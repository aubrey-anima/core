"""@tool 注册表:声明在代码里,调用在聊天里,执行在引擎进程内(issue #15)。

`bt_actions` 那套能力早就是数据化的(声明在 db、实现在包里,形状和 OpenAI function
calling / MCP tool definition 对齐),但**聊天时完全没人读它** —— LLM 看不到"我可以
选择走开",只能用词把话接下去。这个包补的就是那一半:她真有可以选择的行动。

三条边界:

- **执行在引擎进程内。** 跨进程 / 字面 MCP server 留给未来(比如接一个 TTS 让她哼
  一段),v1 全内建。
- **v1 所有角色共用一套 tool。** 按性格分工是 v2 —— 先让"她能走开"这件事成立。
- **工具改的是世界,不是提示词。** `walk_away` 真的发起一次行程,`mute` 真的让下一条
  消息被拒。声明了却没人兑现的能力,比没有更坏。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)


class ToolCallError(RuntimeError):
    """工具调用本身是坏的(未知 id、参数不合法、运行时不支持)。"""


@dataclass
class ToolResult:
    """一次工具执行的结果。

    `text` 是给玩家看的一句(可空 —— 静音通常什么也不说);`end_conversation`
    与 `stop_loop` 是这次调用对本轮对话的处置。
    """

    ok: bool = True
    text: str = ""
    end_conversation: bool = False
    stop_loop: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self, tool_id: str, params: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool": tool_id,
            "params": dict(params),
            "ok": self.ok,
        }
        if self.text:
            payload["text"] = self.text
        if self.detail:
            payload["detail"] = dict(self.detail)
        if self.error:
            payload["error"] = self.error
        if self.end_conversation:
            payload["end_conversation"] = True
        return payload


class ToolRuntime(Protocol):
    """工具能碰到的世界。由 `World` 实现并注入 —— 聊天子系统本身不认识调度器。"""

    def tick(self) -> int: ...

    def now(self) -> int: ...

    def ticks_for_minutes(self, minutes: float) -> int: ...

    def config(self, key: str, default: Any = None) -> Any: ...

    @property
    def state(self) -> Any:
        """`ChatStateStore`。"""
        ...

    def emit(self, event: dict[str, Any]) -> None: ...

    def agent_ids(self) -> list[str]: ...

    def agent_names(self) -> dict[str, str]: ...

    def present_player_ids(self, agent_id: str | None = None) -> list[str]: ...

    def player_name(self, player_id: str) -> str: ...

    def agent_location(self, agent_id: str) -> str: ...

    def player_location(self, player_id: str) -> str: ...

    def face_to_face(self, agent_id: str, player_id: str) -> bool: ...

    def claim_hail(self, agent_id: str, player_id: str) -> str: ...
    """她这会儿能不能主动搭话。空串 = 能,并且当场记下水位;非空 = 一句人话的
    理由。查与记合成一个调用,是为了让工具这条路和调度器闲着时那条路共用同一个
    水位 —— 分开就是玩家连挨两次搭话。"""

    def point_ids(self) -> list[str]: ...

    def point_names(self) -> dict[str, str]: ...

    def move_agent(self, agent_id: str, location: str) -> dict[str, Any]: ...

    def do_action(self, agent_id: str, kind: str, params: dict[str, Any]) -> bool: ...

    def player_do_action(
        self, player_id: str, kind: str, params: dict[str, Any]
    ) -> bool: ...
    """玩家版的 `do_action`。返回 `False` 同样是"世界这会儿不接",不是异常。

    分开一条而不是给 `do_action` 加个开关:玩家没有行为树、没有黑板、没有需求带,
    `emit_action` 那条路上的每一步都假设施动者是一个 `Agent`。共用的是**动词和
    它的后果**,不是执行器。"""

    def give_item(self, player_id: str, agent_id: str, wanted: str) -> dict[str, Any]: ...

    def entity_names(self) -> dict[str, str]: ...

    def interact_with(
        self, actor_id: str, target: str, verb: str,
        participants: list[str] | None = None, player_id: str = "",
    ) -> dict[str, Any]: ...
    """施动者是**角色 id 或 `player:<id>`** —— 见 `ToolContext.world_actor_id`。

    `player_id` 是另一回事:它说的是"这一轮跟她说话的人是谁"(用来查静音、
    姿态、以及"跟我一起"里那个「我」),一次角色调用照样有它。"""

    def close_conversation(self, agent_id: str, player_id: str) -> bool: ...


AGENT_ACTOR = "agent"
PLAYER_ACTOR = "player"


def resolve_location(ctx: "ToolContext", where: str) -> str:
    """把她写的地名换成世界认得的 id。**收人话**,拒绝时说得出有哪些。

    她读到的世界里写着「江堤」,而 `move_agent` / `point_ids()` 只认 `levee` ——
    这一轮反复撞见的那条缝(用一种写法印给她,却按另一种写法验她的回答),在走路
    这件事上有两个落点:`walk` 的 `location` 和 `walk_away` 的 `to_location`。
    两处都只认 id,于是她按世界里读到的名字写下来,第一次调用必然失败。

    合成一个函数是因为**拒绝的那句话必须一模一样**:同一个玩家在同一个聊天窗里
    问路,不该因为她挑了哪个动词而读到两种写法。`walk_away` 那半此前连清单都不给,
    只有一句光秃秃的「没有 江堤 这个地方」—— 那等于告诉她"再猜一次"。

    ⚠️ **清单说人话,不印 id。** 合成一个函数的时候这里写的是 `places_menu(points)`
    (默认带 id),于是玩家点一次"走去哈尔滨",收到的是二十个拉丁字母 id 铺在一个中文
    世界里 —— 而那正是上一版刚给 `places_menu` 加 `with_ids` 要分开的两个读者。
    合并两个调用点时把读者也合并了:模型那份此前需要 id 是因为 `walk` 只收
    `point_ids()`,而这个函数存在的**全部理由**就是它现在收人话了。重名的那几个
    照旧带 id(`小院、小院;说准一点` 是一句没法照着做的回执)。
    """
    from anima_world.intent import places_menu, resolve_place

    points = ctx.runtime.point_names()
    resolved, candidates = resolve_place(where, points)
    if resolved is not None:
        return resolved
    if candidates:
        # 对得上好几个就**别猜**:猜错了她真的会走过去,而世界里一行日志都不报错。
        readable = places_menu(
            {pid: points.get(pid) or pid for pid in candidates}, with_ids=True
        )
        raise ToolCallError(f"{where} 对得上好几个地方:{readable};说准一点")
    raise ToolCallError(
        f"没有 {where} 这个地方;有的是 {places_menu(points, with_ids=False)}")


@dataclass
class ToolContext:
    """一次调用的现场:谁在调、对谁调、以及那个世界。

    `actor` 是**这一下是谁做的**,而不是这一下发生在谁身上。两个 id 字段都还在
    (一次玩家调用照样要知道对面那个角色是谁,`talk_to` 的 target 就是他),变的
    只是施动者。默认 `agent` —— 在这个字段出现之前的每一次调用都是她做的,
    默认值让那些调用点一行都不用改。
    """

    agent_id: str
    player_id: str
    runtime: ToolRuntime
    agent_name: str = ""
    actor: str = AGENT_ACTOR

    @property
    def state(self) -> Any:
        return self.runtime.state

    @property
    def is_player(self) -> bool:
        return self.actor == PLAYER_ACTOR

    @property
    def actor_id(self) -> str:
        """施动者的 id,**裸的**。宿主署名用它 —— 玩家的动作不该记在角色名下。"""
        return self.player_id if self.is_player else self.agent_id

    @property
    def world_actor_id(self) -> str:
        """施动者在**世界那一侧**的 id —— 玩家带 `player:` 前缀。

        和上面那个不是同一件事,而且两个都对。世界里"人"分好几种(角色、玩家、
        货架、金库),所以库存的 holder、量的 owner、事件的 `who` 一律带前缀;
        宿主那一侧只有玩家,前缀是噪音。传错的样子是安静的:一个叫 `p1` 的
        施动者在世界看来根本不存在,于是他的体力扣在一个空账户上。
        """
        return f"player:{self.player_id}" if self.is_player else self.agent_id


ToolHandler = Callable[[ToolContext, dict[str, Any]], ToolResult]


# 能力露在哪个面上。**不是装饰**:自主轮次里没有"对方"这个人,`walk_away` /
# `end_conversation` / `delay_reply` / `wait_for_user` 在那儿一律没有意义 —— 一个
# 在无人对话时也能被选中的 `end_conversation` 只会写出一堆关掉空会话的动作。
CHAT = "chat"            # 玩家跟她说话的那一轮
AUTONOMY = "autonomy"    # 定时轮次:没人跟她说话,她自己决定要不要做点什么
BODY = "body"            # 过日子的动作:走、吃、干活、睡 —— 谁都能做,不挑场合
PLAYER = "player"        # 玩家在网页上点得动的那些 —— 见下
SURFACES = (CHAT, AUTONOMY, BODY, PLAYER)
"""`PLAYER` 是"人和 NPC 共用一套行动"的那一半。

在这之前玩家只有两个入口:`player_move`(真改位置)和 `player_action` ——
而后者是个**空壳**,签名是一个自由字符串,进去只落一条日志事件,不校验、不产生
任何后果。于是同一个世界里有两套行为学:她走路要花时间、吃饭要付钱、
一起做事要在同一个地方;而人点一下"走到哈尔滨",世界里只多一行字。

这个面把玩家接到**同一份 `ToolSpec`** 上:同一套参数校验、同一条执行路径、
同一批副作用。区别只剩调用者是谁 —— LLM 挑,还是人在网页上点。

⚠️ **进这个面的门槛是"在世界里真发生了什么",不是"这个动作听起来人也能做"。**
第一批只有两个:

- `walk` —— 真在途、发 `travel`、到点补 `location_join`
- `broadcast` —— 在场的角色真的留下 `memory_seed`

`work` / `eat` / `sleep` / `wander` / `seek_company` / `talk_to` **暂不进**:
玩家侧它们最终落到 `World.player_action`,而那条只写一行事件日志、**不投递给
任何角色的感知**,更没有需求带和账本可扣。开了它们等于把上面那句"同一批副作用"
变成一句假话 —— 而菜单上的说明还是照角色写的(`eat` 那条写着"付钱是副作用"),
宿主照着画出来的按钮点下去世界里什么也没发生、**还不报错**。那正是本文件开头
骂 `player_action` 的形状,不该在同一次改动里又犯一遍。

要开它们,先补玩家侧的那份账(需求带 / 钱包 / 物品消耗),和 `interact` 门口
挂的是同一条理由。

`wait_for_user`(对人没有意义)、`reach_out`(人本来就是主动方)则永远不进。"""


@dataclass(frozen=True)
class ToolSpec:
    """一个能力的声明 + 实现。形状对齐 OpenAI function calling 的 tool 定义。"""

    id: str
    kind: str
    description: str
    params_schema: dict[str, Any]
    handler: ToolHandler
    surfaces: tuple[str, ...] = (CHAT,)
    player_description: str = ""
    """**同一份声明有两个读者。** 这一格是写给人的那一半,空着就回落 `description`。

    存在的理由是一次真人试玩:宿主问引擎"玩家能做什么",拿回来的 `interact` 说明
    写着「东西的 id 和它能被怎么做,都写在你感觉到的那几行里」—— 而那是**她的提示词
    里的一个块**,玩家那一侧根本没有那个东西;`target` 的例子是 `tree:harbor_oak`,
    橱窗世界的 id,换个世界就是一句错的话。于是宿主只剩一条路:画一个文本框,
    让人自己猜一个 id 和一个动词打进去。

    ⚠️ 这一格**只管文案**。"这儿有什么、能被怎么做、这会儿点不点得动"是数据,
    走 `World.player_options()` —— 把它写进说明里等于把某一个世界的样子刻进引擎,
    而刻错了不报错。
    """
    requires_colocation: bool = False
    """**这个能力要玩家真的在她跟前。**

    在这之前玩家是个幽灵:不管角色在哈尔滨还是三亚,他都能面对面说话、给东西、
    一起做事 —— **位置这个维度等于白设计了**。而引擎里位置是真的(她走路要花
    时间、同地才看得见对方身上的量、`reach_out` 老早就拒绝不在场的人),
    只有玩家这一侧一直没人管。

    判据是**施动者是谁**,不是"这件事重不重要":

    - 玩家**亲手**做的(把围巾递过去、跟她一起坐下来)—— 要当面。隔着三亚递不了
      一条围巾,这一条不需要论证。
    - 玩家**开口**让她做的(「你去睡觉」「你去雕那座冰雕」)—— 不要当面。
      那是一句话,而一句话打电话也说得出来。把它一起挡掉,等于宣称"异地就不能
      跟她说话",而那正是这一层想保住的那一半。

    ⚠️ **默认不生效。** 当初不敢开的理由是引擎侧收紧会当场打断线上世界
    (`player_move` 是宿主可选调用,而 **3.2.0、2026-08 那会儿线上一个宿主都没调**,
    于是"异地"是默认值)。所以这道闸挂在 `presence.enforce_colocation` 上、默认关,
    关着时行为与从前**逐位相同**。
    ⚠️ **括号里那句实况已经过期**:站点 2026-08-13 前后接上了 `player_move`,
    要开这个开关之前重新量一次在场覆盖率(`anima-world presence`),别再引用它。
    迁移说明在 REFERENCE §2.9.7。
    """
    requires_target_entity: bool = False
    """**这个能力要她这儿真有一样能被做点什么的东西。**

    和上面那条是同一件事的另一半:一个是"跟前得有人",一个是"手边得有东西"。
    两条都只回答**这一刻它有没有可能成**,而那正是"要不要把它摆上菜单"的判据。

    存在的理由是线上量出来的:一轮真世界的自主决定,63 次问、0 次动作、5 次失败,
    五次全是同一句 —— 提示词刚说完「这会儿你身边没有别人」,菜单还照样摆着
    `reach_out`。给了菜单却必然执行不了,是这个仓库骂过的那个形状(#15 的原话:
    给了能力却不给许可;这是它的镜像 —— 给了必然被拒的许可)。
    """
    writes: tuple[str, ...] = ()
    """**它把世界改在哪儿。** 每一项是一张表名,或 `events:<类型>`。

    这个字段存在的理由是账面上的:这一版靠人肉找出了八处"声明了却没兑现" ——
    `broadcast` 告诉她"世界里的人都能看到"而只写了一行日志、`walk_away` 隔着手机是
    空动作、规律写 `world_x` 落到别人名下而仪表报成功。**每一个都是"能跑、不报错、
    给错东西"**,而每一个都是玩到了才发现的。

    声明之后这条就成了机器能验的:`tests/test_verb_writes.py` 逐个动词调一遍,
    比对声明的地方到底变没变。CLAUDE.md 里"**她的选择必须在世界里兑现**"那条不变量,
    从一句人得自己记住的话,变成一条会红的测试。

    留空是**故意的空**,不是忘了填 —— 有测试盯着不许留空。
    """
    writes_late: tuple[str, ...] = ()
    """`writes` 里**不在这次调用栈上落地**的那几项(必须是 `writes` 的子集)。

    "改在哪儿"不够,还得说"**什么时候**"。`talk_to` 是这条的由来:它当场发
    `agent_action`,而记忆是关系判定的产物,判定按硬不变量"**LLM 永不在 tick 线程
    调用**"跑在判定线程池上 —— `act()` 返回时那两处**还没变**(实测 40 次里有 10 次
    如此)。只写 `writes` 的话,这份声明读起来是"调用返回时这些地方都已经变了",
    而那是一句会在四分之一的时候变成假话的话。

    对外面的进程,这一格就是"**别在这一瞬间去查**":照着 `writes` 读完立刻查库的
    宿主,会随机地看见一个"她说了话但谁也没记住"的世界,而且不报错 —— 正是这个
    字段当初要堵的那类。

    ⚠️ **它不是"可以不兑现"的许可证。** 声明成 late 的落点照样必须落,只是晚一点;
    `tests/test_verb_writes.py` 对它照等不误,等不到一样红。
    """

    def prompt_line(self) -> str:
        params = ", ".join(
            f"{name}"
            + (":必填" if isinstance(meta, dict) and meta.get("required") else "")
            for name, meta in self.params_schema.items()
        )
        suffix = f" 参数:{params}" if params else " 无参数"
        return f"- {self.id}:{self.description}{suffix}"


_REGISTRY: dict[str, ToolSpec] = {}


def tool(
    *, id: str, kind: str, description: str,  # noqa: A002 - id 是这份契约里的字段名
    params: dict[str, Any] | None = None,
    player_description: str = "",
    surfaces: tuple[str, ...] = (CHAT,),
    writes: tuple[str, ...] = (),
    writes_late: tuple[str, ...] = (),
    requires_colocation: bool = False,
    requires_target_entity: bool = False,
) -> Callable[[ToolHandler], ToolHandler]:
    """把一个函数登记成能力。重复登记同一个 id 是错,不是覆盖。"""

    def decorate(handler: ToolHandler) -> ToolHandler:
        if id in _REGISTRY:
            raise ToolCallError(f"tool {id!r} 已经登记过了")
        unknown = [surface for surface in surfaces if surface not in SURFACES]
        if unknown:
            raise ToolCallError(f"tool {id!r} 声明了不存在的面:{unknown}")
        # `writes_late` 说的是 `writes` 里某几项**什么时候**落地,不是另一份落点表。
        # 不是子集的话,那一项就只在"什么时候"里出现过、从没在"改哪儿"里出现过 ——
        # 一个照着 `writes` 办事的宿主永远看不见它。
        stray = [place for place in writes_late if place not in writes]
        if stray:
            raise ToolCallError(
                f"tool {id!r} 的 writes_late 有 {stray} 不在 writes 里 —— "
                "它说的是 writes 里哪几项是稍后落的,不是第二份落点表"
            )
        if player_description and PLAYER not in surfaces:
            # 写给一个不在这个面上的读者。留着不报的话,那句话永远没人读到,
            # 而作者会以为自己已经把玩家那一侧说清楚了。
            raise ToolCallError(
                f"tool {id!r} 写了 player_description 却不在 PLAYER 面上"
            )
        _REGISTRY[id] = ToolSpec(
            id=id, kind=kind, description=description, writes=tuple(writes),
            player_description=player_description,
            writes_late=tuple(writes_late),
            params_schema=dict(params or {}), handler=handler,
            surfaces=tuple(surfaces), requires_colocation=bool(requires_colocation),
            requires_target_entity=bool(requires_target_entity),
        )
        return handler

    return decorate


def get(tool_id: str) -> ToolSpec:
    spec = _REGISTRY.get(tool_id)
    if spec is None:
        raise ToolCallError(f"没有 {tool_id!r} 这个能力")
    return spec


def tools_for(agent_id: str, surface: str | None = None) -> list[ToolSpec]:
    """这个角色在某个面上能用的能力。

    v1 所有角色同一套(按性格分工是 v2),但**面是分的**:`surface=None` 给全部
    (目录、`contract`、`World.tools()` 用),给了面就只给那个面上的。
    """
    specs = [_REGISTRY[key] for key in sorted(_REGISTRY)]
    if surface is None:
        return specs
    return [spec for spec in specs if surface in spec.surfaces]


def capability_payloads() -> list[dict[str, Any]]:
    """创世时写进能力目录的那份(`capability_registered` 事件的 payload)。"""
    return [
        {
            "id": spec.id,
            "kind": spec.kind,
            "description": spec.description,
            "params_schema": spec.params_schema,
            "surface": ",".join(spec.surfaces),
            # 目录里也要有它:宿主(和运维台的镜像)照这份画界面,而"这个按钮
            # 要玩家走到她跟前才点得动"是界面上必须先知道的事 —— 点下去才发现
            # 的话,那是一次没有任何人预告过的失败。
            "requires_colocation": spec.requires_colocation,
            "requires_target_entity": spec.requires_target_entity,
        }
        for spec in tools_for("*")
    ]


def call(ctx: ToolContext, tool_id: str, params: dict[str, Any]) -> ToolResult:
    """执行一次调用。工具自己的失败降级成一个 `ok=False` 的结果 —— 一次坏调用
    不该掀翻整轮聊天,但**必须留下痕迹**(结果会随观测量落到消息行上)。"""
    spec = _REGISTRY.get(tool_id)
    if spec is None:
        logger.warning("角色 %s 调了一个不存在的能力 %r", ctx.agent_id, tool_id)
        return ToolResult(ok=False, error=f"unknown tool {tool_id}")
    try:
        return spec.handler(ctx, dict(params or {}))
    except ToolCallError as exc:
        logger.warning("能力 %s 拒绝了这次调用:%s", tool_id, exc)
        return ToolResult(ok=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - 工具坏了不该让她哑掉
        logger.warning("能力 %s 执行失败", tool_id, exc_info=True)
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
