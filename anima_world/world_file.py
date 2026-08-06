"""世界文件:`.cyberworld` 的线格式(v3,gzip + JSONL)。

**这个模块存在的理由是消掉一层。** v2 之前世界有三种表示:

    world_seed.json   人写的世界描述(创世的输入)
    Redis 键          活着的世界
    world_state.json  导出时的 dump(藏在一个 zip 里)

第一种和第三种是同一件东西的两种写法 —— 都在回答"这个世界是什么样",只是一个给人写、
一个给机器读。**留着两种,就要维护两套 schema、两套校验、两个跨仓库镜像**,而且它们
描述同一个世界时长得完全不一样(种子写 `duties`,dump 里是编译好的 `bt_nodes`)。

v3 把它们合成一个:**一个文件,两层记录**。

    {"kind":"manifest","version":3,...}              第一行,必须
    {"kind":"author","type":"agent","body":{…}}      作者层:装载时**编译**
    {"kind":"redis","key":"clock","value":…}         状态层:装载时**直接落键**
    {"kind":"event","seq":1,"type":"payment",…}      事件,一行一条
    {"kind":"mysql","table":"memories","row":{…}}    无限增长的另外三张表
    {"kind":"checksum","sha256":…}                   最后一行,可选

**每种记录的载荷都收在一个字段里**(`body` / `value` / `payload` / `row`),不平铺展开。
这条是被一个真的碰撞逼出来的:种子的 `locations` 条目自己带 `kind`(嵌套地图的几何类型
`region`/`point`),平铺进信封就把记录类型覆盖掉了。信封字段和载荷字段共用一个命名空间,
就永远存在"某个世界的某个字段恰好叫 kind"这种事 —— 而它一旦发生是**静默**的。
收进 `body` 让它不可表达,这比约定"别用这几个名字"可靠。

于是:

- **手写的世界** = 只有 `author` 记录
- **跑过的世界导出来** = 只有 `redis` / `event` / `mysql` 记录
- **两者同一个扩展名、同一个装载器、同一道门** —— 创世和还原本来就是同一个动作:
  往一个前缀里装一个世界文件。
- 混着写有确定语义(**按文件顺序应用**),所以"导出一份、手改一行、装回去"天然成立;
  而把一份只含 `author` 记录的文件装进一个**已有**的世界,就是一次编辑 ——
  创作台要的"增删改查创世态"由此免费得到,不必在 API 上再开一排写口。

## 为什么是 JSONL 而不是一整块 JSON

事件是这个文件里最大的一部分,而它天然是一行一条。JSONL 让**导出和导入都能流式** ——
一个跑了两年的世界不必整份塞进内存。(v2 的 `_dump_mysql_section` 是全量 `replay()`
再 `SELECT *`,没有任何上限;那是个会在生产上撞到才发现的洞。)

附带的好处不是修辞:`zcat x.cyberworld | grep '"type":"entity_spawn"'` 真的能用,
两份导出的 `diff` 真的看得懂,而 zip 的成员白名单、解压炸弹比率那六十行校验整个不需要了 ——
一条流,一个上限。

## 编译器还在

`duties` → 行为树、`money` → `payment` 事件、被引用没定义的物品自动补一条 —— 这些是
真实存在的一层,不会因为换了容器就消失。变的只是它不再叫"播种":`author` 记录聚合回
一个 section 字典(`author_records_to_seed`),原样喂给既有的那条编译管线。
**这是有意的最小改动** —— 换掉容器,不动语义。
"""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

# 线格式版本。**改这个 = 改跨仓库契约**(运维台镜像着它)。
WORLD_FILE_VERSION = 3

# 记录类型的闭集。未知类型**当场拒绝**,不静默跳过 —— 一个装载器读不懂的记录,
# 跳过它等于安静地少装一半世界,而文件看上去完全正常。
RECORD_KINDS = frozenset({"manifest", "author", "redis", "event", "mysql", "checksum"})

# `author` 记录的类型 → 它在编译管线里的 section 名。
# 这张表就是"种子的十一个段"原样搬过来的,一个不多一个不少。
AUTHOR_SECTIONS: dict[str, str] = {
    "agent": "agents",
    "location": "locations",
    "kind": "kinds",
    "entity": "entities",
    "rule": "rules",
    "item": "items",
    "stock": "stocks",
    "relation": "relations",
    "memory": "memories",
    "visibility": "stock_visibility",
    "place": "stock_places",
}
# 那两个**不是一列条目而是一个对象**的段,和 `config` 同类:一份而不是一条。
AUTHOR_OBJECT_TYPES: dict[str, str] = {
    "config": "config",
    "world_setting": "world_setting",
    "mock_narration": "mock_narration",
}
AUTHOR_CONFIG_TYPE = "config"    # 兼容旧名字;它现在只是 AUTHOR_OBJECT_TYPES 之一

# Redis 值的类型闭集(和 `type` 命令的回答一致)。
REDIS_TYPES = frozenset({"hash", "list", "string", "set"})

# 无限增长的那三张行表(events 单独有自己的记录类型,因为它有 seq 语义)。
MYSQL_ROW_TABLES = ("memories", "conversations", "messages")

# ── 上限 ────────────────────────────────────────────────────────────────────
#
# gzip 和 zip 一样能做成解压炸弹,所以上限一条都不能少。但形状简单得多:
# **一条流,三个数** —— 而不是 v2 那套(成员数 × 单成员大小 × 压缩比)。
DEFAULT_MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024      # 解压后总字节
DEFAULT_MAX_RECORDS = 5_000_000                          # 记录条数
DEFAULT_MAX_LINE_BYTES = 32 * 1024 * 1024                # 单行字节(一个巨型 hash)


class WorldFileError(ValueError):
    """这个文件不能当世界用 —— 逐条列出,一次性报完。

    和 `OntologyError` / `RuleError` 同一条纪律:坏文件不许流到运行期。
    """

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__(
            "invalid world file:\n" + "\n".join(f"- {e}" for e in self.errors)
        )


@dataclass
class WorldFileManifest:
    """第一行。**必须是第一行** —— 装载器要先知道版本才敢读后面的东西。"""

    version: int = WORLD_FILE_VERSION
    world_id: str = ""
    name: str = ""
    engine_min: str = ""
    engine_max_exclusive: str = ""
    source_engine_version: str = ""
    created_at: str = ""
    summary: str = ""
    genre: str = ""
    setting: str = ""
    theme: str = "default"
    extra: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        row: dict[str, Any] = {"kind": "manifest", "version": int(self.version)}
        for name in ("world_id", "name", "engine_min", "engine_max_exclusive",
                     "source_engine_version", "created_at", "summary", "genre",
                     "setting", "theme"):
            value = getattr(self, name)
            if value:
                row[name] = value
        row.update(self.extra)
        return row

    @classmethod
    def from_record(cls, record: Any) -> WorldFileManifest:
        if not isinstance(record, dict) or record.get("kind") != "manifest":
            raise WorldFileError([
                "第一行必须是 manifest 记录 —— 装载器要先知道版本才敢读后面的东西"
            ])
        try:
            version = int(record.get("version"))
        except (TypeError, ValueError):
            raise WorldFileError(["manifest 少了 version,或者它不是整数"]) from None
        known = {
            "kind", "version", "world_id", "name", "engine_min", "engine_max_exclusive",
            "source_engine_version", "created_at", "summary", "genre", "setting", "theme",
        }
        return cls(
            version=version,
            world_id=str(record.get("world_id") or ""),
            name=str(record.get("name") or ""),
            engine_min=str(record.get("engine_min") or ""),
            engine_max_exclusive=str(record.get("engine_max_exclusive") or ""),
            source_engine_version=str(record.get("source_engine_version") or ""),
            created_at=str(record.get("created_at") or ""),
            summary=str(record.get("summary") or ""),
            genre=str(record.get("genre") or ""),
            setting=str(record.get("setting") or ""),
            theme=str(record.get("theme") or "default"),
            # 不认识的顶层字段**留着,不丢** —— manifest 是要跨仓库读的,
            # 而"加一个可选字段"不该是破坏性变更(种子那边一直是这条,继承过来)。
            extra={k: v for k, v in record.items() if k not in known},
        )


# ── 写 ──────────────────────────────────────────────────────────────────────


def _line(record: dict[str, Any]) -> bytes:
    # `ensure_ascii=False` 是有意的:世界里全是中文,转义成 \uXXXX 会让"能 grep、
    # 能 diff、能读"这三条一起失效 —— 而那正是换成文本格式的全部理由。
    return (json.dumps(record, ensure_ascii=False, sort_keys=False) + "\n").encode("utf-8")


def write_world_file(
    path: str | Path,
    manifest: WorldFileManifest,
    records: Iterable[dict[str, Any]],
    *,
    checksum: bool = True,
    compress: bool = True,
) -> str:
    """把记录流写成一个 `.cyberworld`。返回内容的 sha256。

    **流式写** —— `records` 可以是个生成器,一个跑了两年的世界不必先攒进内存。
    校验和覆盖 checksum 行**之前**的全部字节,写在最后一行。
    """
    digest = hashlib.sha256()
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # `mtime=0`:同样的世界写两次要得到同样的字节。gzip 默认把当前时间写进头里,
    # 于是"这两个包一样吗"永远答不了 —— 而这个格式的卖点之一就是可 diff。
    opener = (
        (lambda: gzip.GzipFile(filename="", mode="wb", fileobj=open(target, "wb"), mtime=0))
        if compress else (lambda: open(target, "wb"))
    )
    with opener() as raw:
        head = _line(manifest.to_record())
        digest.update(head)
        raw.write(head)
        for record in records:
            kind = record.get("kind")
            if kind not in RECORD_KINDS or kind in ("manifest", "checksum"):
                raise WorldFileError([
                    f"写不出这条记录:kind={kind!r} —— manifest 只能是第一行,"
                    f"checksum 只能由这个函数生成"
                ])
            blob = _line(record)
            digest.update(blob)
            raw.write(blob)
        if checksum:
            raw.write(_line({"kind": "checksum", "sha256": digest.hexdigest()}))
    return digest.hexdigest()


# ── 读 ──────────────────────────────────────────────────────────────────────


def read_world_file(
    path: str | Path,
    *,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_records: int = DEFAULT_MAX_RECORDS,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
) -> tuple[WorldFileManifest, Iterator[dict[str, Any]]]:
    """读一个世界文件:先拿到 manifest,再流式拿记录。

    返回 `(manifest, 记录迭代器)`。**manifest 当场解析并校验**(调用方常常只想问
    "这个包要哪个引擎",不该为此把整份读完 —— `world inspect` 就是那个调用方);
    其余记录**边读边给**,内存占用与世界大小无关。

    校验和(如果文件里有)在迭代**走到最后一条**时才验得完,所以它不会在这里抛 ——
    迭代器耗尽时校验不上会抛 `WorldFileError`。半途丢弃迭代器 = 不验校验和,
    这是**有意的**:`inspect` 那类只读头的调用方不该被迫读完一个 5 GB 的世界。
    """
    target = Path(path)
    if not target.exists():
        raise WorldFileError([f"文件不存在:{target}"])

    # **压缩与否只看头两个字节,不看扩展名。** 写出去的永远是 gzip,但读进来的
    # 允许是裸 JSONL —— 手写一个世界不该被逼着先 gzip 一下,而内置的演示世界
    # 因此能以可 diff 的纯文本进仓库(一个 review 不了的二进制块不该是这个格式
    # 给新用户看的第一眼)。压缩是传输层的事,格式本身是那些行。
    with open(target, "rb") as probe:
        packed = probe.read(2) == b"\x1f\x8b"
    handle = gzip.open(target, "rb") if packed else open(target, "rb")
    try:
        first = handle.readline(max_line_bytes + 1)
    except (OSError, EOFError) as exc:
        handle.close()
        raise WorldFileError([f"读不动这个文件:{exc}"]) from None
    if not first:
        handle.close()
        raise WorldFileError(["文件是空的"])
    manifest = WorldFileManifest.from_record(_parse_line(first, 1))
    if manifest.version > WORLD_FILE_VERSION:
        handle.close()
        raise WorldFileError([
            f"这个世界文件是格式 v{manifest.version} 写的,而这个引擎只认到 "
            f"v{WORLD_FILE_VERSION} —— 装一个新些的引擎,别指望旧引擎猜新格式"
        ])

    def _records() -> Iterator[dict[str, Any]]:
        digest = hashlib.sha256()
        digest.update(first)
        seen = 0
        total = len(first)
        stated: str | None = None
        try:
            for lineno, blob in enumerate(handle, start=2):
                total += len(blob)
                if total > max_uncompressed_bytes:
                    raise WorldFileError([
                        f"解压后超过 {max_uncompressed_bytes} 字节 —— 拒绝继续读"
                    ])
                if len(blob) > max_line_bytes:
                    raise WorldFileError([f"第 {lineno} 行超过 {max_line_bytes} 字节"])
                if not blob.strip():
                    continue
                record = _parse_line(blob, lineno)
                kind = record.get("kind")
                if kind not in RECORD_KINDS:
                    raise WorldFileError([
                        f"第 {lineno} 行:不认识的记录类型 {kind!r} —— 只认 "
                        f"{sorted(RECORD_KINDS)}。跳过它等于安静地少装一半世界"
                    ])
                if kind == "manifest":
                    raise WorldFileError([f"第 {lineno} 行:manifest 只能是第一行"])
                if kind == "checksum":
                    stated = str(record.get("sha256") or "")
                    break            # checksum 之后不该再有东西
                seen += 1
                if seen > max_records:
                    raise WorldFileError([f"记录超过 {max_records} 条 —— 拒绝继续读"])
                digest.update(blob)
                yield record
            if stated and stated != digest.hexdigest():
                raise WorldFileError([
                    "校验和对不上 —— 这个文件在路上被改过或者截断了"
                ])
        finally:
            handle.close()

    return manifest, _records()


def _parse_line(blob: bytes, lineno: int) -> dict[str, Any]:
    try:
        record = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorldFileError([f"第 {lineno} 行不是合法 JSON:{exc}"]) from None
    if not isinstance(record, dict):
        raise WorldFileError([
            f"第 {lineno} 行必须是一个对象,收到 {type(record).__name__}"
        ])
    return record


# ── 作者层 ↔ 编译管线 ───────────────────────────────────────────────────────


def author_records_to_seed(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """把 `author` 记录聚合成编译管线吃的那个 section 字典。

    这是"换容器不动语义"的落点:`{"kind":"author","type":"agent",...}` 聚合回
    `{"agents": [...]}`,于是既有的那十八个编译步骤一行都不用改。

    非 `author` 的记录**直接忽略** —— 调用方拿同一个流分别喂给这里和落键那一路。
    """
    seed: dict[str, Any] = {}
    errors: list[str] = []
    for index, record in enumerate(records):
        if record.get("kind") != "author":
            continue
        kind = str(record.get("type") or "").strip()
        body = record.get("body")
        if not isinstance(body, dict):
            errors.append(
                f"author[{index}] {kind or '?'}:少了 body,或者它不是一个对象"
                f"(收到 {type(body).__name__}) —— 载荷收在 body 里,不平铺"
            )
            continue
        merged = AUTHOR_OBJECT_TYPES.get(kind)
        if merged is not None:
            # 一份而不是一条:两条同类记录**合并**,不是追加。
            seed.setdefault(merged, {}).update(body)
            continue
        section = AUTHOR_SECTIONS.get(kind)
        if section is None:
            errors.append(
                f"author[{index}]:不认识的 type {kind!r} —— 只认 "
                f"{sorted([*AUTHOR_SECTIONS, *AUTHOR_OBJECT_TYPES])}"
            )
            continue
        seed.setdefault(section, []).append(body)
    if errors:
        raise WorldFileError(errors)
    return seed


def seed_to_author_records(seed: dict[str, Any]) -> list[dict[str, Any]]:
    """反过来:一份 section 字典 → `author` 记录。

    留着它有两个正当用处,都不是"兼容旧格式":把内置演示世界一次性转成新格式,
    以及让测试用最短的写法造夹具。**产品代码里没有第二条读 section 字典的路。**
    """
    out: list[dict[str, Any]] = []
    reverse = {section: kind for kind, section in AUTHOR_SECTIONS.items()}
    reverse_object = {section: kind for kind, section in AUTHOR_OBJECT_TYPES.items()}
    unknown: list[str] = []
    for section, value in seed.items():
        kind = reverse_object.get(section)
        if kind is not None:
            if isinstance(value, dict) and value:
                out.append({"kind": "author", "type": kind, "body": dict(value)})
            continue
        kind = reverse.get(section)
        if kind is None:
            # **不认识的段是报错,不是跳过。** 静默丢掉一段的后果:世界照样建得起来,
            # 只是少了一整层东西(`mock_narration` 丢了 = 世界改说另一种语言),
            # 而没有任何地方会说一句。这条是被那次真的丢段逼出来的。
            unknown.append(section)
            continue
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, dict):
                    out.append({"kind": "author", "type": kind, "body": entry})
    if unknown:
        raise WorldFileError([
            f"不认识的段 {sorted(unknown)} —— 认得的是 "
            f"{sorted([*AUTHOR_SECTIONS.values(), *AUTHOR_OBJECT_TYPES.values()])}。"
            f"加一个新段要同时在 AUTHOR_SECTIONS / AUTHOR_OBJECT_TYPES 里登记,"
            f"不然它会安静地从世界里消失"
        ])
    return out


def has_author_records(records: Iterable[dict[str, Any]]) -> bool:
    return any(record.get("kind") == "author" for record in records)
