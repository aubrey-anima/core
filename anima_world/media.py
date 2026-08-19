"""图片:引擎只存一个不透明的 URI,**一个字节都不落地**。

这里不是一个图片子系统 —— 没有存字节、没有下载、没有缩略、不联网。它只有三样
东西:**一道闸**(这个字符串能不能当图用)、**一张键表**(引擎认得哪几格是图)、
**一个扫描器**(这份文件里的图都指向哪儿)。

## 为什么是 URI 而不是字节

`character_card.py` 的模块 docstring 已经把立绘那一次的三条理由写全了(包是分发物、
引擎没有地方放字节、要自足就写 `data:`)。地点的图逐字适用同一条,再加一条产品向的:
图床由网站自己建(`/media/`),世界文件里只留一条绝对外链 —— 于是**换图不用重发世界**,
而世界文件仍然可以 review、可以 diff。

## 为什么 `data:` 不禁,而是给一个上限

禁掉 `data:` 就等于宣布"一个 `.cyberworld` 不可能自足",而自足是这个格式的一半价值
(一份手写的小世界,发出去就能跑,不依赖任何一台还活着的服务器)。所以留着它,
**改成给它一个数**。

## 上限按**读出口**定,所以两个数不必相等

一条 URI 的代价不在存,在**它每次被读出去要过几次网**:

- `portrait` 的读出口是 `roster()` —— 按需拿一次,一次一份名册。
- 地点的图骑在 `state()` 上,而网站每几秒轮询一次 `state()`,**一次带回全部地点**。
  同样一张图放在这里,代价是放在名册里的 (地点数 × 轮询频率) 倍。

于是 `portrait` 给 1 MiB、每张地点图给 256 KiB。数不相等不是随手写的,是这两个出口
本来就不一样贵 —— 让它们相等才需要理由。

## 量的是**这条 URI 在线上有多长**,不是原图有多大

`len(uri.encode("utf-8"))`,一条规则管所有 scheme。两个连带的事实:

- base64 把 N 字节的原图变成约 **4/3 N** 的文本(再加 `data:image/png;base64,`
  这二十来个字节的前缀)。创作台那侧的 `PORTRAIT_MAX_BYTES` 量的是**原图**字节 ——
  同一个 1 MiB 在两边不是同一个东西,它那份 1 MiB 原图到这里是 1.37 MiB,**过不了**。
  要对齐的话创作台的闸该收到 750 KiB 原图上下(768 KiB 是数学上的临界,加上前缀正好越线)。
- 对 `http(s):` 而言这个上限实际上是一条"URL 别长到离谱"的闸(两千字符的链接照过)。
  真正会撑爆读出口的只有 `data:` —— 但**一条规则比两条好记**,而且给 http 单独开一格
  的那天,没人记得它为什么在那儿。

## 闸是错误不是警告,查过真数据才敢这么定

`FOR-STUDIO.md` 里那条纪律是我自己写的:**收紧一道闸之前先去数一遍现有的世界**。
2026-08-19 数过 —— 舰队上两个活着的世界(灯塔湾 2385 条事件、晚潮 145674 条)、
创作台归档的 7 个包、所有种子文件:`data:` **零**处,`portrait` **零**处,包自带的
`demo.cyberworld` 用的是 `https:`。也就是说这两个上限今天**拦不到任何一个已经存在的
世界**,而它们要拦的那种文件(一份把三张 4K 图 base64 进去的世界)还没出生。
拦不到存量的收紧,就该当场报错 —— 留成警告的话,第一个撞上它的人是在线上撞上的。
"""

from __future__ import annotations

import json
from hashlib import blake2b
from typing import Any, Iterable, Iterator
from urllib.parse import urlparse

#: 图片允许的 scheme。`data:` 让一个包自足,`http(s):` 让资产另走图床。
#: 相对路径不在里面 —— 包是分发物,写 `images/a.png` 发出去就是一张断的图,
#: 而且不报错。**`character_card.PORTRAIT_SCHEMES` 就是这个对象**(不是一份拷贝):
#: 两处各写一份 frozenset,迟早只有一处跟着改。
MEDIA_SCHEMES: frozenset[str] = frozenset({"https", "http", "data"})

#: 地点的两格图,**顺序即"从远到近"**。为什么是两格而不是一格:它们是两个用途,
#: 而一张图同时满足不了 —— 地图上那一格要小、要一眼认得出(缩略图的活),
#: 走进去铺开的那张要大、要留白、压暗了还得压得住字(背景图的活)。
#: 合成一格的话,作者只能在"地图上糊成一团"和"正文压在人脸上"之间挑一个。
LOCATION_IMAGE_KEYS: tuple[str, ...] = ("map_image", "scene_image")

#: 每一格是干什么用的 —— **这句话进 `contract --json`**。创作台的铁律是"问引擎,
#: 不读文档",所以"哪个是哪个"必须能从契约里读出来,不能只写在 FOR-STUDIO 上。
LOCATION_IMAGE_GLOSS: dict[str, str] = {
    "map_image": "地图上那一格的缩略图:小、方形、一眼认得出是哪儿",
    "scene_image": "走进这个地点之后铺开的那张大图:要留白,压暗了还压得住正文",
}

#: 引擎认得的图字段全集 —— 扫描器只认这几个键名。
#: 拿它当"值看着像不像 URL"去猜的话,`llm.base_url` 会被当成一张图。
MEDIA_KEYS: frozenset[str] = frozenset({"portrait", *LOCATION_IMAGE_KEYS})

#: 立绘这条 URI 的字节上限。读出口是 `roster()`(按需一次)。
PORTRAIT_MAX_BYTES = 1024 * 1024

#: 地点每一格图的字节上限。读出口是 `state()`,而网站每几秒问一次、一次带回
#: 全部地点 —— 同样一张图在这里贵得多,理由见模块 docstring。
LOCATION_IMAGE_MAX_BYTES = 256 * 1024

#: 每个字段的上限查表(扫描器与闸共用,免得第三处再抄一遍数)。
MEDIA_MAX_BYTES: dict[str, int] = {
    "portrait": PORTRAIT_MAX_BYTES,
    **{key: LOCATION_IMAGE_MAX_BYTES for key in LOCATION_IMAGE_KEYS},
}

#: 我自己发出去过、后来被两格取代的那个名字。`FOR-STUDIO.md §3.22` 曾经公布过
#: 单数的 `image` —— 照着那一版写的人会写出一个**引擎不认识、装载时安静丢掉**的键。
#: 拦不下(不认识的键原样带过去是这一层的规矩),但必须点名。
_SUPERSEDED_LOCATION_IMAGE_KEYS: dict[str, str] = {
    "image": "地点的图现在是两格:" + " / ".join(LOCATION_IMAGE_KEYS),
}


def media_uri_errors(value: Any, *, label: str, field: str, max_bytes: int) -> list[str]:
    """这一格图哪里写错了,一行一条(空 = 没问题)。**一次列全。**

    两道闸,理由各不相同,所以两条都报:scheme(相对路径发出去就是一张断的图)、
    字节数(它每几秒就要从读出口过一次)。
    """
    if value is None:
        return []
    if not isinstance(value, str):
        return [f"{label}: {field} 必须是一个 URI 字符串,收到 {type(value).__name__}"]
    text = value.strip()
    if not text:
        return [f"{label}: {field} 是空的 —— 不要这一格就别写它"]

    errors: list[str] = []
    scheme = urlparse(text).scheme.lower()
    if scheme not in MEDIA_SCHEMES:
        errors.append(
            f"{label}: {field} {_clip(text)!r} 不是绝对 URI"
            f"(只认 {', '.join(sorted(MEDIA_SCHEMES))}:)。"
            "包是分发物 —— 相对路径发出去就是一张断的图,而且不报错;"
            "要让包自足就写 data: URI"
        )
    size = len(text.encode("utf-8"))
    if size > max_bytes:
        errors.append(
            f"{label}: {field} 这条 URI 有 {size} 字节,上限 {max_bytes}"
            f"({max_bytes // 1024} KiB)。量的是这条字符串本身 —— base64 之后约是"
            "原图的 4/3,所以想内嵌的话原图要更小;"
            "把图放到图床上写 https: 链接则完全不占这个额度"
        )
    return errors


def _clip(text: str, limit: int = 120) -> str:
    """报错里不许原样回吐一条 300 KB 的 `data:` URI —— 那会把终端刷没。"""
    return text if len(text) <= limit else text[:limit] + "…"


def _location_label(entry: Any, index: int) -> str:
    name = entry.get("id") or entry.get("name") if isinstance(entry, dict) else None
    return f"locations[{index}] ({name!r})" if name else f"locations[{index}]"


def location_media_errors(entry: Any, *, label: str) -> list[str]:
    """一个地点条目上那两格图的错。"""
    if not isinstance(entry, dict):
        return []
    errors: list[str] = []
    for key in LOCATION_IMAGE_KEYS:
        if key in entry:
            errors.extend(
                media_uri_errors(
                    entry.get(key), label=label, field=key,
                    max_bytes=LOCATION_IMAGE_MAX_BYTES,
                )
            )
    return errors


def world_location_media_errors(world_seed: Any) -> list[str]:
    """一份作者层里所有地点图的错,一行一条。**一次列全。**

    位置和 `world_card_errors` / `visibility_band_errors` 一模一样:**它不进
    `world_seed_errors`**。那个函数的裁决面是跨仓库镜像(`is_valid_world_seed`),
    而它的纪律是"裁决只看必填键,可选字段一律不改裁决面" —— 图是可选的,
    一个没有图的世界不是坏世界。这道闸挂在 `authored_layer_errors` 里,由**同一批
    调用点**(开机 / `validate world` / `world check`)一起调,于是"validate 说没问题、
    开机却失败"这种事不会发生。
    """
    entries = (world_seed or {}).get("locations") if isinstance(world_seed, dict) else None
    if not isinstance(entries, list):
        return []
    errors: list[str] = []
    for index, entry in enumerate(entries):
        errors.extend(location_media_errors(entry, label=_location_label(entry, index)))
    return errors


def world_location_media_warnings(world_seed: Any) -> list[str]:
    """作者八成是照着**上一版文档**写的。**只警告,绝不拒绝。**

    只点名一个键:`image`。它不是随便挑的 —— 那是 `FOR-STUDIO.md §3.22` 里我自己
    公布过、后来改成两格的名字。公布出去的名字收不回来,而照着它写的人得到的是
    "装载时安静丢掉":世界照跑、日志干净、图一张都不出现。
    """
    entries = (world_seed or {}).get("locations") if isinstance(world_seed, dict) else None
    if not isinstance(entries, list):
        return []
    warnings: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        for key, hint in _SUPERSEDED_LOCATION_IMAGE_KEYS.items():
            if key in entry:
                warnings.append(
                    f"{_location_label(entry, index)}: {key!r} 引擎不认识,"
                    f"装载时会被丢掉 —— {hint}"
                )
    return warnings


def _looks_like_json(text: str) -> bool:
    """这个字符串**装的是不是一行 JSON**。

    ⚠️ 这一条是被自己的第一版当场打脸打出来的:Redis 的哈希里,一个地点是一串
    **JSON 文本**,不是一个嵌套对象(`{"locations": {"码头": "{\\"id\\": …}"}}`)。
    不拆开的话,一个跑过的世界导出来会得到一句"外链 0 条" —— 而它的地图上明明
    挂着两条链接。**一个安静的错答案比一句"没数"坏得多**,而这一段的全部用途
    就是不让外链的代价隐形。

    只看头一个字符,所以一条 `data:` URI 不会被当成 JSON 去解(它以 `d` 开头)。
    """
    head = text.lstrip()[:1]
    return head in ("{", "[")


def _as_json_object(text: str) -> Any:
    """把一行 JSON 文本解开;解不动就当它不是(不报错 —— 数图不该拦住任何事)。"""
    if not _looks_like_json(text):
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


class MediaScan:
    """一份世界文件里的图**指向哪儿** —— 按 host 数外链,按字节数内嵌。

    **不联网探活。** 这一段的用途不是"这些链接还活着吗"(那是运维的活,而且答案
    每分钟都在变),是**把外链这条产品选择的代价摆到台面上**:这个包依赖哪几台
    还必须活着的服务器、以及它自己带了多少字节。一个只会说"有 12 条外链"的报告
    帮不了任何人;按 host 分组之后,"这个包挂在一台已经不归我们的 CDN 上"这句话
    是一眼看得出来的。

    走法是**显式栈的迭代**,而且只挑 `MEDIA_KEYS` 里那几个键名的值 —— 按"值看着
    像不像一条 URL"猜的话,`llm.base_url` 会被算成一张图。
    """

    def __init__(self) -> None:
        self._hosts: dict[tuple[str, str], dict[str, Any]] = {}
        self._seen: set[bytes] = set()
        self.external_count = 0
        self.inline_count = 0
        self.inline_bytes = 0
        self.inline_largest = 0

    def feed(self, obj: Any) -> None:
        """把任意一段 JSON 喂进来(作者层的一节、一条事件的载荷、一行 Redis)。"""
        stack: list[Any] = [obj]
        while stack:
            node = stack.pop()
            if isinstance(node, str):
                node = _as_json_object(node)
                if node is None:
                    continue
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in MEDIA_KEYS and isinstance(value, str):
                        self.record(str(key), value)
                    elif isinstance(value, (dict, list)):
                        stack.append(value)
                    elif isinstance(value, str) and _looks_like_json(value):
                        stack.append(value)
            elif isinstance(node, list):
                for value in node:
                    if isinstance(value, (dict, list)):
                        stack.append(value)
                    elif isinstance(value, str) and _looks_like_json(value):
                        stack.append(value)

    def record(self, field: str, uri: str) -> None:
        """记一条。空串与写坏了的值不算 —— 那些归闸报,不归这里数。

        **同一条 URI 只数一次。** 数的是"这个世界有多少张图、指向哪儿",不是
        "这个字符串在文件里出现了几次" —— 后者是导出格式的实现细节:出生证明
        (`:meta.world_seed`)里抄着一份作者层,`agent_join` 的载荷里又是一份,
        于是一个只有一张立绘的新世界会被报成三张。**同一件事被数成三件,和数漏了
        一样是个错答案。**

        去重靠摘要而不是留一份原串:一条 `data:` URI 可以有几百 KB,把它们整个
        攒在内存里等于为了数数把这个包又装了一遍。
        """
        text = uri.strip()
        if not text:
            return
        digest = blake2b(text.encode("utf-8"), digest_size=16).digest()
        if digest in self._seen:
            return
        self._seen.add(digest)
        parsed = urlparse(text)
        scheme = parsed.scheme.lower()
        if scheme == "data":
            size = len(text.encode("utf-8"))
            self.inline_count += 1
            self.inline_bytes += size
            self.inline_largest = max(self.inline_largest, size)
            return
        if scheme not in MEDIA_SCHEMES:
            return          # 相对路径不是外链,是一条错(闸会说)
        host = parsed.netloc or "(没有 host)"
        row = self._hosts.setdefault(
            (host, scheme), {"host": host, "scheme": scheme, "count": 0, "fields": set()}
        )
        row["count"] += 1
        row["fields"].add(field)
        self.external_count += 1

    def external_media(self) -> list[dict[str, Any]]:
        """按 host 列外链。**不联网探活。**"""
        return [
            {
                "host": row["host"],
                "scheme": row["scheme"],
                "count": row["count"],
                "fields": sorted(row["fields"]),
            }
            for row in sorted(
                self._hosts.values(), key=lambda r: (r["host"], r["scheme"])
            )
        ]

    def inline_media_bytes(self) -> dict[str, int]:
        """内嵌的 `data:` 一共占了多少 —— 也就是这个包为"自足"付的那笔钱。

        `total` 是**去重之后**的字节数(同一张图抄在三处只算一份):要回答的是
        "这个世界带了多少张图、多大",而不是"gzip 之前这个文件有多长"。
        """
        return {
            "count": self.inline_count,
            "total": self.inline_bytes,
            "largest": self.inline_largest,
        }


def tee_media(records: Iterable[Any], scan: MediaScan) -> Iterator[Any]:
    """把记录流原样放过去,顺手喂给扫描器。**单趟遍历。**

    `world check` 已经是流式读一遍(灯塔湾那份包十几万条状态记录,攒一份全量列表
    出来纯属白背)。为了数图再读第二遍等于把那条纪律作废,所以在同一条流上分叉。

    **作者记录和状态记录都数**:一个跑过的世界导出来只有状态记录,而她的立绘就在
    `agent_join` 的载荷里、地点的图就在那几行 Redis 行上。只数作者层的话,这种包会
    得到一句"外链 0 条" —— 一个安静的错误答案,正是这个仓库最怕的那一类。
    """
    for record in records:
        if isinstance(record, dict):
            for key in ("body", "value", "payload", "row"):
                if key in record:
                    scan.feed(record[key])
        yield record
