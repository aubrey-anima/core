"""`docs/REFERENCE.md` 与 `docs/FOR-STUDIO.md` 说的话必须是真的。

为什么值得一个测试文件:REFERENCE 是**宿主照着写代码**的那份东西(CLAUDE.md 把它
和 README 一起列为"改架构前先读"),而它和代码之间此前没有任何机械联系。一次人工
对账查出四条:

- `world.declare_visibility(kind, …)` —— 真实形参是 `owner_kind`,照文档写关键字
  参数直接 TypeError(而种子里的字段**确实**叫 `kind`,所以这个错特别容易犯)
- `world.close_conversation(id)` / `conversation_messages(id)` —— 真实是 `conversation_id`
- `world.history` / `fast_forward` / `report` —— 三个真实公开 API,REFERENCE 里
  **零次出现**

一次性改完没有用:下次加个方法照样飘。所以把对账钉成闸门 —— 加公开方法就必须写文档,
改形参名就必须改文档。API 面本来就是"只加不改"的跨仓库契约,这道闸和那条纪律同源。

## 2026-08-20:这道闸的射程比它读起来窄得多,补上了三块

第八轮的验收把它的三块**结构性**盲区一次列全 —— 三块都不是"漏了一个名字",是
"这一整类名字它从来没看过":

1. **只读 `REFERENCE.md`**,`FOR-STUDIO.md`(给创作台看的那份)一个字不读。
   于是 `World.set_rules`(排给 1.4.0、到 3.6.0 还没做)在那份文档里躺了很久。
2. **只认带左括号的 `` `World.名字( ``**。不带括号写的 `` `World.give_item` ``
   —— 出身 `01f0f1b`(2.3.0 时代),真身是 `_ToolRuntime.give_item` —— 从没被逮到。
   `f8e5186` 那次(`World._colocation_gate`)会红,**纯属它当时碰巧带了括号**。
3. **只拿 `dir(World)` 去对**,于是 REFERENCE 里所有**非 `World` 类**的名字
   (`Scheduler.*` / `Perception.*` / `Director.*` / `_ToolRuntime.*` …)一个都没查过。
   而"这个方法挂在哪个类上"恰恰是没人会去复核的那一格 —— 写的人复核的是"它做不做
   那件事",不是"它姓什么"。

补法在下面三个 `test_*_exists` 里。**第三块是静态的**(`ast` 扫 `anima_world/`,
不 import),因为要对的是一百多个类,而 import 一遍等于把引擎的副作用搬进闸门。

⚠️ **两个已知的假阳性,是有意放过的,不是漏网**:`JudgeResult.axes_*` 是通配写法
(真字段 `axes_a_to_b` / `axes_b_to_a`),`MemoryStore` 是这个仓库的既有泛称
(真身 `RedisMemoryStore` / `MySQLMemoryStore`,那两句话的内容全对)。前者按
"下一个字符是 `*` 就跳过"放行,后者因为索引里根本没有这个类而自动跳过。

⚠️ **`test_every_public_method_is_documented`(覆盖率那一半)有意仍然只读 REFERENCE。**
把 FOR-STUDIO 也算进"写过了",等于让一个只在创作台说明书里出现过的方法通过
API 面的文档闸 —— 那是**放宽**,不是补射程。加公开方法的落点只有 REFERENCE 一处。
"""
from __future__ import annotations

import ast
import collections
import inspect
import re
from pathlib import Path

import pytest

from anima_world.api import World

_DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
REFERENCE = _DOCS_DIR / "REFERENCE.md"
FOR_STUDIO = _DOCS_DIR / "FOR-STUDIO.md"
# 存在性那几道闸读两份;覆盖率那一道只读 REFERENCE(理由见模块 docstring)。
DOCS = (REFERENCE, FOR_STUDIO)
_PACKAGE = Path(__file__).resolve().parent.parent / "anima_world"

# 文档里**当成方法调用**写的地方(带括号)。只认带括号的,是因为 `world.db` 是
# 文件名、`world.setting` / `world.minutes_per_tick` 是配置键 —— 它们和 `World`
# 的成员同名同前缀,不加这一条会把配置表整个误报成幽灵方法。
_CALL = re.compile(r"`(?:world|World)\.([a-z_][a-z0-9_]*)\(")
# 覆盖率检查放宽到"被提到过"就算:属性(如 `world.paused`)写不出括号。
_MENTION = re.compile(r"`(?:world|World)\.([a-z_][a-z0-9_]*)`")
# 不带括号、**大写 `World.`** 打头的裸名。有意只认大写那一半:小写的 `world.` 同时是
# 实例名、配置键前缀(`world.minutes_per_tick`)和文件名(`world.db` / `world.cyberworld`),
# 三种东西挤在一个前缀上,拿它当"成员"判会把配置表整个误报;大写的 `World.` 只可能是类。
_BARE = re.compile(r"`World\.([a-z_][a-z0-9_]*)`")
# 反引号里的**非 `World` 类**成员:`Scheduler.tick` / `intent.Director._colocation_refusal`
# / `_ToolRuntime._colocation_gate` 都是这个形状。带不带括号都认。
_SPAN = re.compile(r"`([^`\n]*?)`")
_CLASS_MEMBER = re.compile(
    r"(?:^|[.\s(\[])(_?[A-Z][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)"
)

# 有意不写进 REFERENCE 的公开名字。**每一条都要有理由** —— 这个清单是给
# "为什么它不在文档里"留的位置,不是垃圾桶。
UNDOCUMENTED_ON_PURPOSE = {
    # 底层对象,REFERENCE 的"持久化与底层"一节整体交代过(绕过它们直写 db 违反纪律 1)
    "scheduler", "chat_store", "chat_state", "chat_service", "players",
    # async 版本:参数与同步版逐字相同,文档在同步版那一行里点名
    "achat", "achat_burst", "achat_reply",
    # 上下文管理器协议
    "open", "close",
}


def _documented_calls(path: Path = REFERENCE) -> set[str]:
    """文档里当成方法调用写的名字 —— 这些必须真的存在、形参必须对得上。"""
    return set(_CALL.findall(path.read_text(encoding="utf-8")))


def _documented_any() -> set[str]:
    """被提到过就算写过(属性没有括号)。**只看 REFERENCE**,见模块 docstring。"""
    text = REFERENCE.read_text(encoding="utf-8")
    return _documented_calls() | set(_MENTION.findall(text))


def _public() -> set[str]:
    return {name for name in dir(World) if not name.startswith("_")}


def _members() -> set[str]:
    """`World` 身上**到底有没有这个名字** —— `dir()` 加上 `self.X = …` 的实例属性。

    两处比 `_public()` 宽,各有各的理由:
    - **带下划线的也算**。文档会正当地点名私有方法(`World._colocation_error` 那一段
      整节都在讲两扇门为什么猜错),把它判成幽灵,下一个人的处理办法就是把闸关掉。
    - **实例属性也算**。`dir()` 拿的是类,而 `World.players` / `World.chat_store`
      是 `__init__` 里才长出来的 —— 只拿 `dir()` 去对,一句真话会被判成幽灵。

    **只用在裸名那道闸上**:带括号的调用仍然对 `_public()`,那一格不许放宽
    (`World.players` 是真的,`World.players()` 不是)。
    """
    source = inspect.getsource(World)
    assigned = set(re.findall(r"^\s+self\.([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=", source, re.M))
    return set(dir(World)) | assigned


def _class_index() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """静态扫 `anima_world/`:类名 → 它身上有哪些名字,外加它继承了谁。

    **不 import**:要对的是一百多个类,散在几十个模块里,import 一遍等于把引擎的
    副作用(线程池、Redis 客户端、包数据读盘)搬进一道纯文本闸门。`ast` 够用,
    因为要答的问题只是"这个名字在这个类的类体里出现过没有"。
    """
    members: dict[str, set[str]] = collections.defaultdict(set)
    bases: dict[str, set[str]] = collections.defaultdict(set)
    for path in sorted(_PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                if isinstance(base, ast.Name):
                    bases[node.name].add(base.id)
                elif isinstance(base, ast.Attribute):
                    bases[node.name].add(base.attr)
            names: set[str] = set()
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(child.name)
                    for sub in ast.walk(child):
                        if (
                            isinstance(sub, ast.Attribute)
                            and isinstance(sub.value, ast.Name)
                            and sub.value.id == "self"
                            and isinstance(sub.ctx, ast.Store)
                        ):
                            names.add(sub.attr)
                elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                    names.add(child.target.id)
                elif isinstance(child, ast.Assign):
                    for target in child.targets:
                        if isinstance(target, ast.Name):
                            names.add(target.id)
            members[node.name] |= names
    return members, bases


def _class_members(name: str, index: dict[str, set[str]], bases: dict[str, set[str]],
                   seen: set[str] | None = None) -> set[str]:
    seen = seen if seen is not None else set()
    if name in seen or name not in index:
        return set()
    seen.add(name)
    found = set(index[name])
    for base in bases.get(name, ()):
        found |= _class_members(base, index, bases, seen)
    return found


def _class_member_ghosts(text: str) -> list[str]:
    """反引号里写的 `某个类.某个名字`,那个类是本包定义的,而那个名字它身上没有。

    索引里**没有**的类名一律跳过 —— 文档里合法地出现别的仓库的类(platform 的
    `CoreRegistry`)、JS 的 `Object.keys`、以及这个仓库的既有泛称 `MemoryStore`
    (真身 `RedisMemoryStore` / `MySQLMemoryStore`)。跳过它们是**有意的**:
    一道会对着自己不认识的东西喊的闸,下一个人的处理办法是把它关掉。
    """
    index, bases = _class_index()
    ghosts: list[str] = []
    for span in _SPAN.finditer(text):
        inner = span.group(1)
        for hit in _CLASS_MEMBER.finditer(inner):
            cls, attr = hit.group(1), hit.group(2)
            if cls == "World" or cls not in index:
                continue
            if inner[hit.end():hit.end() + 1] == "*":
                continue  # `JudgeResult.axes_*` 这类通配写法
            if attr not in _class_members(cls, index, bases):
                ghosts.append(f"{cls}.{attr}")
    return ghosts


def test_every_documented_method_exists():
    """文档里写了、World 上没有 —— 宿主照着写会 AttributeError。

    2026-08-20 起 **FOR-STUDIO 也读**:创作台照着它写代码,而"库里有而对方看不见"
    的对偶是"对方看得见而库里没有",后者更贵。
    """
    problems: list[str] = []
    for path in DOCS:
        ghosts = sorted(_documented_calls(path) - _public())
        if ghosts:
            problems.append(f"{path.name}:{ghosts}")
    assert not problems, (
        f"文档提到了不存在的成员:{problems}。"
        "要么改文档,要么它是被删掉的 API —— 而 API 面是只加不改的。"
    )


def test_every_documented_bare_name_exists():
    """不带括号写的 `` `World.名字` `` 也得真的存在。

    这一格漏了很久:`` `World.give_item` ``(真身 `_ToolRuntime.give_item`)在
    REFERENCE 里从 2.3.0 躺到 3.6.0,`` `World.set_rules` ``(压根没做)在
    FOR-STUDIO 里躺着 —— 两条都只差一个左括号就会被上面那道闸逮住。
    **一句话是不是真的,不该取决于它写没写括号。**
    """
    known = _members()
    problems: list[str] = []
    for path in DOCS:
        ghosts = sorted(set(_BARE.findall(path.read_text(encoding="utf-8"))) - known)
        if ghosts:
            problems.append(f"{path.name}:{ghosts}")
    assert not problems, (
        f"文档里 `World.x` 这么写、而 World 上没有 x:{problems}。"
        "改成真身所在的类(比如 `_ToolRuntime.give_item`),"
        "或者写明它还没实现 —— 排期不是现状。"
    )


def test_every_documented_class_member_exists():
    """非 `World` 的类也得对得上:名字挂在哪个类上,是没人会复核的那一格。

    `f8e5186` 把 `_ToolRuntime._colocation_gate` 写成了 `World._colocation_gate`,
    上面那道闸逮住它**纯属它当时带了括号**;同一段里第二个名字
    (`agent_location()`)裸着写,一路绿着落地。这道闸把"它姓什么"变成一次字符串比对。
    """
    problems: list[str] = []
    for path in DOCS:
        ghosts = sorted(set(_class_member_ghosts(path.read_text(encoding="utf-8"))))
        if ghosts:
            problems.append(f"{path.name}:{ghosts}")
    assert not problems, (
        f"文档把这些名字挂在了它们不在的类上:{problems}。"
        "写之前把类名一起核了 —— `git grep -n 'def <名字>' anima_world/` 只答方法在哪个文件,"
        "答不了它在哪个类上。"
    )


def test_these_gates_still_bite():
    """回归语料:上面三道闸真的咬得动它们号称咬得动的东西。

    **一道加宽的闸必须自证它咬得住** —— 否则"补了射程"和"写了一句补了射程"在
    屏幕上长得一模一样(全绿)。这里喂进去的四条,三条是真实历史(它们当年一条都
    没红过),外加两条**不许**咬的(已知的假阳性)。
    """
    # ① `World.give_item` —— 真身 `_ToolRuntime.give_item`,2.3.0 起躺在 REFERENCE 里
    assert set(_BARE.findall("`World.give_item` 写")) - _members() == {"give_item"}
    # ② `World.set_rules` —— 全仓不存在,躺在 FOR-STUDIO 的"已知的洞"表里
    assert set(_BARE.findall("(`World.set_rules` + CLI)")) - _members() == {"set_rules"}
    # ③ 挂错类:`f8e5186` 那次的形状,这一次不带括号也逮得住
    assert _class_member_ghosts("`Director._colocation_gate` 问了在途") == [
        "Director._colocation_gate"
    ]
    assert _class_member_ghosts("`Scheduler.no_such_method()`") == [
        "Scheduler.no_such_method"
    ]
    # ④ 不许咬的两条:通配写法,以及索引里没有这个类的泛称
    assert _class_member_ghosts("`JudgeResult.axes_*` 是可选的") == []
    assert _class_member_ghosts("`MemoryStore.rebuild` 有行就一动不动") == []
    # ⑤ 真话不许咬
    assert _class_member_ghosts("`_ToolRuntime._colocation_gate`") == []


def test_every_public_method_is_documented():
    """加了公开方法就必须写进 REFERENCE。

    API 面是宿主依赖的跨仓库契约。一个没人知道的方法等于没加,而一个**半知道**的
    方法更糟:宿主从 `dir()` 里翻出来用,而它的语义从来没被写下来过。
    """
    missing = sorted(_public() - _documented_any() - UNDOCUMENTED_ON_PURPOSE)
    assert not missing, (
        f"这些公开方法在 REFERENCE 里一次都没出现:{missing}。"
        "写进 §3 的 API 表,或者加进 UNDOCUMENTED_ON_PURPOSE **并说明理由**。"
    )


def test_documented_parameter_names_match_the_real_signature():
    """文档写的形参名必须真的存在。

    `declare_visibility(kind, …)` 就是这么飘的:种子里那个字段叫 `kind`,文档跟着
    写了 `kind`,而形参是 `owner_kind`。位置调用照样能用,所以永远不会有人发现 ——
    直到有人写了关键字参数。**FOR-STUDIO 同办**(2026-08-20 起):创作台也是照着
    关键字参数写代码的那一方。
    """
    problems: list[str] = []
    for path in DOCS:
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"`(?:world|World)\.([a-z_][a-z0-9_]*)\(([^`]*)\)`", text):
            name, written = match.group(1), match.group(2).strip()
            if not written or name not in _public():
                continue
            attribute = getattr(World, name)
            try:
                signature = inspect.signature(attribute)
            except (TypeError, ValueError):
                continue  # property 之类,没有签名可比
            real = {p for p in signature.parameters if p != "self"}
            if any(p.kind is p.VAR_KEYWORD for p in signature.parameters.values()):
                continue  # 收 **kwargs 的,写什么都算数
            for chunk in re.split(r",(?![^{\[(]*[}\])])", written):
                chunk = chunk.strip().lstrip("*").split("=")[0].split(":")[0].strip()
                if re.fullmatch(r"[a-z_][a-z0-9_]*", chunk) and chunk not in real:
                    problems.append(
                        f"{path.name} 的 world.{name}:文档写了 `{chunk}`,"
                        f"真实签名是 ({', '.join(sorted(real))})"
                    )
    assert not problems, "文档的形参名和代码对不上:\n  " + "\n  ".join(problems)


def test_the_contract_tool_catalog_says_which_surface_each_tool_is_on():
    """`contract --json` 的 `chat_tools` **不只有 chat 面**,所以每条得带 `surfaces`。

    名字是历史包袱(运维台镜像已经在读 `chat_tools`,改名等于跨仓库破坏),但内容是
    全目录:`reach_out` 只在定时轮次里出现,聊天里永远调不到。照着字段名做一个
    "聊天能力"列表就会把它也列进去 —— 而那是一个用户永远等不到的按钮。
    """
    import json
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-m", "anima_world", "contract", "--json"],
        capture_output=True, text=True,
    )
    assert done.returncode == 0, done.stderr
    catalog = json.loads(done.stdout)["chat_tools"]
    assert catalog, "能力目录是空的"
    for entry in catalog:
        assert entry.get("surfaces"), f"{entry['id']} 没说自己在哪个面上"

    by_id = {entry["id"]: entry["surfaces"] for entry in catalog}
    assert "chat" not in by_id["reach_out"], (
        "reach_out 被标成了聊天能力 —— 它只在定时轮次里出现"
    )
    assert "chat" in by_id["walk_away"]


def test_the_contract_storage_section_matches_the_code():
    """跨仓库的存储契约由 `contract --json` 传出去,运维台镜像读的就是它。"""
    import json
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-m", "anima_world", "contract", "--json"],
        capture_output=True, text=True,
    )
    payload = json.loads(done.stdout)["storage"]
    assert payload["backend"] == "redis"
    assert payload["key_prefix"] == "anima:{world_id}:"

# 配置键的家默认是 REFERENCE 里**那张表上的一行**。少数几个键的家在正文,逐个列在
# 这里并写清理由 —— 一份列得出名字的例外表比一道谁都满足得了的松闸诚实。
_CONFIG_KEYS_DOCUMENTED_IN_PROSE = {
    "economy.player_allowance":
        "「玩家兜里的第一笔钱由世界给」那一段有整段 ⚠️ 说明(§经济),"
        "塞进表里会把那段话拆散",
}


def test_every_declared_config_key_is_documented():
    r"""声明了一个配置键,就必须在 REFERENCE 里说得出它是什么。

    这道闸是被 `scheduler.max_agents` 逼出来的:它 **2.0 就交付了**(`3254f36`),
    默认 100、`0 = 不限`、报错里带着 `config set` 解法 —— 而到 2026-08-25 为止,
    `docs/REFERENCE.md` 和 `docs/FOR-STUDIO.md` 里**一个字都没有**,CHANGELOG 也没记。
    整整三周,一个下游按文档判"这个引擎有没有人数上限"的人,得到的答案是"没有"。

    ⚠️ **这正是"交付要回执"那条纪律没有闸的样子。** 公开方法有
    `test_every_public_method_is_documented` 盯着,配置键此前一条都没有 ——
    而配置键恰恰是**运营与作者**唯一够得着的那一面。

    ## 2026-08-25 同日修:第一版的判据答的不是它自己以为的那句话

    第一版取"在 REFERENCE 里出现过"(`` `([A-Za-z][\w.]*)` `` 全文抓一遍),理由写着
    "有几个键的家在正文,不该逼它们进表"。**那个理由今天仍然成立**,可那个抓法把
    配置键和**提示词模板名**装进了同一个点号命名空间。2026-08-25 数了一遍:
    REFERENCE 里反引号 token **1017** 个,带点的 **239** 个,其中只有 66 个是配置键
    —— 剩下 **173** 个带点的 token 里,`chat.intent` / `contact.compose` /
    `autonomy.decide` 这类**提示词模板名**占了 28 个
    (`prompt_store._SAMPLE_VARS` 36 个模板里,有 28 个在 REFERENCE 里被反引号提过;
    剩下 8 个是 `narrative.mock.<动作种类>`,文档里写的是那个尖括号占位)。
    实测:往 `_DEFAULTS` 里塞一个**很像会有**的开关 `chat.intent_classifier`,
    这道闸答**绿** —— 而满足它的那个反引号在提示词模板那一节,说的是一条模板的名字,
    和"这个世界有没有这个开关"一个字都不相干。
    **又一次:证据成立,而它证的不是你以为的那句话。**

    现在的判据分两档,把两个约束一起满足:

    - **默认档是"表上有它自己那一行"**(行首 `| ` + 反引号包着的键 + ` |`)。
      提示词模板名占不了表格首格(2026-08-25 实测:36 个模板名一个都没占),
      于是上面那个洞当场关死;而 66 个键里 65 个本来就有自己那一行,**一条假红都不多**。
    - **例外档是 `_CONFIG_KEYS_DOCUMENTED_IN_PROSE`**,留给家在正文的那几个
      (今天只有 `economy.player_allowance` 一个)。它**不是免检**:名字必须是真配置键
      (一份比代码老的例外表会替一个不存在的键放行),而且那个键得**真的在 REFERENCE
      里出现过** —— 例外表放行的是"不进表",不是"不写"。

    这道闸要挡的仍然是**一个字都没有**;换掉的只是"什么样的一句话算数"。
    有牙判据见 `test_the_config_key_gate_is_not_satisfied_by_a_prompt_template_name`。
    """
    from anima_world.config_store import _DEFAULTS

    text = REFERENCE.read_text(encoding="utf-8")
    mentioned = set(re.findall(r"`([A-Za-z][\w.]*)`", text))
    row_keys = set()
    for line in text.split("\n"):
        row = re.match(r"^\|\s*`([A-Za-z][\w.]*)`\s*\|", line)
        if row:
            row_keys.add(row.group(1))

    stale = sorted(set(_CONFIG_KEYS_DOCUMENTED_IN_PROSE) - set(_DEFAULTS))
    assert not stale, (
        f"例外表里这些名字已经不是配置键了:{stale}。"
        "一份比代码老的例外表会替一个不存在的键放行,删掉它们。"
    )

    missing = sorted(
        key for key in _DEFAULTS
        if key not in row_keys and key not in _CONFIG_KEYS_DOCUMENTED_IN_PROSE
    )
    assert not missing, (
        f"这些配置键在 REFERENCE 的配置表里没有自己那一行:{missing}。"
        "运营和作者只能从文档知道一个键存不存在 —— 声明了却不写,等于没交付。"
        "家确实在正文的,写进 _CONFIG_KEYS_DOCUMENTED_IN_PROSE 并说清理由;"
        "⚠️ **在别处被反引号提过一次不算** —— 提示词模板名和配置键同一个命名空间。"
    )

    silent = sorted(key for key in _CONFIG_KEYS_DOCUMENTED_IN_PROSE if key not in mentioned)
    assert not silent, (
        f"这些键挂在「家在正文」的例外表上,而正文里一次都没出现:{silent}。"
        "例外表放行的是「不进表」,不是「不写」。"
    )


def test_the_config_key_gate_is_not_satisfied_by_a_prompt_template_name(monkeypatch):
    """判据自己也得站得住:一个**提示词模板名**不许替一个配置键说"有文档"。

    这条钉的是上面那道闸 2026-08-25 当天被验收探针捅穿的那个洞 —— 那次探针往
    `_DEFAULTS` 里加了一个 `chat.intent_classifier`,闸门 **1 passed**。

    前提两条先自证(那个名字真的是模板名、它真的在 REFERENCE 里被反引号包着出现过),
    然后塞一个同名的配置键进去:**老判据答绿,新判据必须答红**。

    ⚠️ **这条测的不是 `chat.intent_classifier` 这个名字**,是"配置键和提示词模板
    共用一个点号命名空间"这件事不再骗得过闸门。哪天那条模板改了名,把 victim 换成
    `prompt_store._SAMPLE_VARS` 里任意一个在 REFERENCE 出现过的名字即可 ——
    前提那两条 assert 会当场告诉你该换。
    """
    from anima_world import config_store, prompt_store

    victim = "chat.intent_classifier"
    assert victim in prompt_store._SAMPLE_VARS, (
        f"前提没成立:{victim} 已经不是提示词模板名了,换一个再钉"
    )
    assert f"`{victim}`" in REFERENCE.read_text(encoding="utf-8"), (
        f"前提没成立:REFERENCE 里没有 `{victim}` 这个 token,这条测不出那个洞"
    )
    assert victim not in config_store._DEFAULTS, "前提没成立:它已经是真配置键了"

    monkeypatch.setitem(config_store._DEFAULTS, victim, True)
    with pytest.raises(AssertionError, match=re.escape(victim)):
        test_every_declared_config_key_is_documented()


def test_配置表里那一列默认值_和_DEFAULTS对得上():
    """🔴 **3.11.3(验收 B+C ⑤)**:`director.enabled` 3.11.2 翻成了 `true`,
    而 REFERENCE 的配置表照旧写着 `false` —— **文档是运营和作者唯一的入口**,
    一个写错的默认值比没写更坏:照它去"打开"一个本来就开着的开关不会报错,
    照它以为"默认关着"而不去关的世界会开着跑。

    ⚠️ 上一条闸只查**这个键在不在表里**,查不出**那一格写的是什么** ——
    一张齐全而说谎的表,和一张缺行的表是两种病。
    """
    import re

    from anima_world.config_store import _DEFAULTS

    text = REFERENCE.read_text(encoding="utf-8")
    rows = dict(re.findall(r"^\| `([a-z0-9_.]+)` \| \w+ \| ([^|]+?) \|", text, re.M))
    wrong = []
    for key, spec in _DEFAULTS.items():
        said = rows.get(key)
        if said is None:
            continue                      # 缺行由上面那条闸管
        want = spec[0]
        got = said.strip().strip("*").strip()
        if isinstance(want, bool):
            if got.lower().strip("*") != str(want).lower():
                wrong.append(f"{key}: 表里写 {got!r},而 _DEFAULTS 是 {want}")
    assert not wrong, "配置表那一列在说谎:\n" + "\n".join(wrong)
