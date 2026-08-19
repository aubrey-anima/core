"""角色卡:主次、一句话、立绘。**作者写给玩家看的,不是写给她看的。**

诉求来自创作台(`../tool/docs/引擎接口诉求-角色卡.md`),依据是一次真人试玩:线上
那个世界 21 个角色,4 个是作者写了几周的主角、17 个是背景 NPC,而玩家的通讯录里
这 21 个人长得一模一样。作者写得进、校验放行、世界跑得动、包也导得出 —— **就是
到不了玩家眼前,而全程零报错**。这正是这个仓库最怕的那一类。

三条设计判断,写在这里而不是散在调用点上:

**一、`billing` 是枚举不是布尔**(`lead` / `supporting`(缺省) / `hidden`)。
恋爱陪伴产品里"解锁后才出现的人"是真需求,而布尔长不出第三档 —— 等要的时候再改
就是一次跨仓库破坏。缺省是 `supporting` 而不是"未知":老世界一个都没标,它们的
正确解读是"这个世界没做过主次",不是"这个世界坏了"。

**二、立绘走 URI,引擎一个字节都不存。** 创作台给了三条路,选的是"引擎只存一个
不透明的字符串",但**加了一道闸**:必须是带 scheme 的绝对 URI(`https` / `http` /
`data`)。理由三条 ——

- `.cyberworld` 是**分发物**。写 `portraits/a1.png` 而图留在作者笔记本上,包发出去
  就是一张断的图,而且不报错。所以相对路径**开不了机**。
- 引擎没有地方放字节。立绘永远不进提示词,所以 Redis 那条"有界才进"的理由推不动它;
  它也不是"她的"东西(不是记忆、不是事件),MySQL 那四样也不收。
- 想让包自足就写 `data:` URI —— gzip JSONL 装得下,而且**不必新增一种记录类型**。
  新增 `{"kind": "asset"}` 会当场破掉跨仓库线格式契约(运维台 `lib/worldPackage.js`
  镜像着 `RECORD_KINDS`,不认识的记录类型是硬错误),还会让 `demo.cyberworld` ——
  仓库里那份**能 review 的纯文本世界文件** —— 变成一块没人看得懂的 base64。

**三、引擎不理解它,只原样带过去。** 尤其 `tagline` **绝不进提示词**:那是写给玩家
看的广告词,混进人设她就会照着念。所以角色卡**不写黑板**,只走事件日志的
`agent_join.payload.spec.card`,读回来走投影。
"""

from __future__ import annotations

from typing import Any

from anima_world.media import (
    MEDIA_SCHEMES,
    PORTRAIT_MAX_BYTES as _PORTRAIT_MAX_BYTES,
    media_uri_errors,
)

#: 主次。顺序即优先级(第一屏怎么排由宿主定,引擎只交出声明)。
CARD_BILLINGS: tuple[str, ...] = ("lead", "supporting", "hidden")

#: 没标 = 背景角色。**不是"未知"** —— 老世界一个都没标,那不是坏。
DEFAULT_BILLING = "supporting"

#: 一句话的上限(字符数)。有上限是因为它进的是通讯录那一行 —— 一段三百字的
#: "一句话"在任何界面上都只会被截断,而截断在哪儿由界面随手决定,作者看不见。
TAGLINE_MAX_CHARS = 80

#: 立绘允许的 scheme。`data:` 让一个包自足,`http(s):` 让资产另走 CDN。
#: 相对路径不在里面 —— 理由见模块 docstring。
#: **它就是 `media.MEDIA_SCHEMES` 这个对象**(不是一份拷贝):地点的两格图也
#: 走同一道闸,两处各写一份 frozenset 的话,迟早只有一处跟着改。
PORTRAIT_SCHEMES: frozenset[str] = MEDIA_SCHEMES

#: 立绘这条 URI 的字节上限。**这一格此前引擎一个字节都没管过** ——
#: 于是一张 8 MB 的图 base64 进世界文件,加载放行、导出带着走、`roster()` 每次
#: 都把它整个吐出来,而没有任何一处会说一句话。数怎么定的、量的是什么,见
#: `media.py` 的模块 docstring(一句话:量这条 URI 本身,上限按读出口定)。
PORTRAIT_MAX_BYTES = _PORTRAIT_MAX_BYTES

#: 引擎认得的三格。**不认识的键原样带过去**(创作台已经预告了第四样:声线、
#: 主题色、CV)—— 引擎本来就不理解这几格,拦下来只会让新版创作台配不了老引擎。
#: 但会在 warnings 里点名,免得一个拼错的 `taglien` 静静地什么也不做。
CARD_KEYS: frozenset[str] = frozenset({"billing", "tagline", "portrait"})


def card_errors(card: Any, *, label: str) -> list[str]:
    """这张卡哪里写错了,一行一条(空 = 没问题)。**一次列全**。

    加载期严格:坏声明当场开不了机,而且把问题一次说完 —— 作者改一条重试一次
    是这个仓库明确拒绝的体验(`--ticks 0` 当校验器用的前提就是这个)。
    """
    if card is None:
        return []
    if not isinstance(card, dict):
        return [f"{label}: card 必须是一个对象,收到 {type(card).__name__}"]

    errors: list[str] = []

    billing = card.get("billing")
    if billing is not None:
        if not isinstance(billing, str) or billing not in CARD_BILLINGS:
            errors.append(
                f"{label}: card.billing 是 {billing!r},只认 "
                f"{', '.join(repr(b) for b in CARD_BILLINGS)}"
                f"(不写 = {DEFAULT_BILLING!r})"
            )

    tagline = card.get("tagline")
    if tagline is not None:
        if not isinstance(tagline, str):
            errors.append(
                f"{label}: card.tagline 必须是一行文本,收到 {type(tagline).__name__}"
            )
        elif len(tagline) > TAGLINE_MAX_CHARS:
            errors.append(
                f"{label}: card.tagline 有 {len(tagline)} 个字,上限 "
                f"{TAGLINE_MAX_CHARS} —— 它进的是通讯录里名字底下那一行"
            )
        elif "\n" in tagline:
            errors.append(f"{label}: card.tagline 是**一行**,不许换行")

    # 立绘和地点的两格图走**同一道闸**(`media.media_uri_errors`):scheme 那半条
    # 理由逐字相同,字节那半条只是上限的数不一样(按各自的读出口定)。两处各写
    # 一遍的话,下一次收紧只会收紧其中一处 —— 而另一处不报错,它只是继续放行。
    if "portrait" in card:
        errors.extend(
            media_uri_errors(
                card.get("portrait"), label=label, field="card.portrait",
                max_bytes=PORTRAIT_MAX_BYTES,
            )
        )
    return errors


def card_warnings(card: Any, *, label: str) -> list[str]:
    """作者八成写错了、但不拦的地方。**只警告,绝不拒绝。**"""
    if not isinstance(card, dict):
        return []
    unknown = sorted(set(card) - CARD_KEYS)
    if not unknown:
        return []
    return [
        f"{label}: card 里有引擎不认识的键 {', '.join(repr(k) for k in unknown)}"
        "(原样带给宿主,不丢 —— 但如果这是拼错的,它什么也不会做)"
    ]


def normalize_card(card: Any) -> dict[str, Any] | None:
    """把一张(已经验过的)卡收成规范形状。没有卡就是 `None`。

    **缺席 = 这个世界没做过角色卡,不是错误**,所以这里绝不凭空造一张出来:
    造出来的话每个老世界都会突然多出 21 张"背景角色"卡,而宿主分不出"作者说他是
    背景"和"作者什么也没说"。`billing` 的缺省在**读**的那一侧补(`billing_of`)。
    """
    if not isinstance(card, dict) or not card:
        return None
    out = dict(card)
    if isinstance(out.get("tagline"), str):
        out["tagline"] = out["tagline"].strip()
    if isinstance(out.get("portrait"), str):
        out["portrait"] = out["portrait"].strip()
    return {k: v for k, v in out.items() if v not in (None, "")} or None


def billing_of(card: Any) -> str:
    """这个角色的主次。没卡 / 没写 = `supporting`。"""
    if isinstance(card, dict):
        value = card.get("billing")
        if isinstance(value, str) and value in CARD_BILLINGS:
            return value
    return DEFAULT_BILLING


def card_of_seed_agent(agent_id: str, world_seed: dict[str, Any] | None) -> dict[str, Any] | None:
    """作者在世界文件里给这个人写的卡(创世时用)。没写就是 `None`。"""
    if not world_seed:
        return None
    entries = world_seed.get("agents")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("id") or "") == agent_id:
            return normalize_card(entry.get("card"))
    return None


def _agent_label(entry: Any, index: int) -> str:
    name = entry.get("id") or entry.get("name") if isinstance(entry, dict) else None
    return f"agents[{index}] ({name!r})" if name else f"agents[{index}]"


def world_card_errors(world_seed: Any) -> list[str]:
    """一份作者层里所有角色卡的错,一行一条。**一次列全。**

    和 `visibility_band_errors` 同一个位置:它**不进** `world_seed_errors`。
    那个函数的裁决是跨仓库镜像(`is_valid_world_seed`),而它的纪律写在
    `world_seed.py` 的模块 docstring 里 —— 裁决只看必填键,可选字段一律不改裁决面。
    卡的闸挂在它旁边,由**同一批调用点**(加载期 `_load_world_file` 与
    `validate world`)一起调,于是"validate 说没问题、开机却失败"这种事不会发生。
    """
    if not isinstance(world_seed, dict):
        return []
    entries = world_seed.get("agents")
    if not isinstance(entries, list):
        return []
    errors: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or "card" not in entry:
            continue
        errors.extend(card_errors(entry.get("card"), label=_agent_label(entry, index)))
    return errors


def world_card_warnings(world_seed: Any) -> list[str]:
    """作者八成写错了、但不拦的地方(拼错的键)。**只警告,绝不拒绝。**"""
    if not isinstance(world_seed, dict):
        return []
    entries = world_seed.get("agents")
    if not isinstance(entries, list):
        return []
    warnings: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or "card" not in entry:
            continue
        warnings.extend(card_warnings(entry.get("card"), label=_agent_label(entry, index)))
    return warnings
