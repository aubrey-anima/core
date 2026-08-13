"""ChatStateStore:一轮聊天要读写的"此刻"状态(chat-agent,1.3.0)。

纯存储 —— 不发事件、不调 LLM,和 `ChatStore` 同一条纪律。六样都是**当前值**,
不是历史:

- stance          她此刻对某人的关系性意图(#18)
- mutes           软静音 / "等会儿再说"(#15)
- refused_topics  她拒绝谈的话题(#15)
- followups       到点回来找你的约(#15 的 delay_reply 兑现)
- overrides       玩家教给她的对话规则,按 (角色, 玩家) 永久(#16)
- 消息行上的观测量  一轮的 stance / intent / tool_call,给观测用

这一层是**纯逻辑基类**:存储原语全部归子类
(`redis_state.RedisChatStateStore`),这里只放不碰存储的业务逻辑
(`topics_hit_by`、转录转发)与契约(方法签名、时间语义、白名单)。
子类自备 `self._lock` 与 `self._transcript`。

两条时间语义,哪个后端都不能想当然:

- **静音用墙钟,不用 tick。** 玩家那一侧的"五分钟"是真的五分钟,而世界时钟
  可能是演示速度(1 tick/秒)。用 tick 会让"五分钟别理我"在演示速度下变成
  不到一分钟。
- **回头找你用 tick。** 那是世界内部的约定(到第几 tick 回来敲门),和墙钟无关。

**为什么不发每轮事件。** issue #18/#16 里写的是"每轮发一个 agent_stance /
user_intent 事件"。CLAUDE.md 的不变量是「聊天子系统与事件核解耦:整场会话只在关闭
时发一个事件」—— 每轮一个事件等于把聊天转录搬进世界的历史。这里的取法是:观测量
落在这些当前值和消息行上(运维台照样能逐轮显示 tag),**后果**照旧走事件日志(她真的
走开 = travel/location_join 事件,广播 = 一条事件),而整场会话的 stance/intent
分布在关闭时随那一个 `conversation` 事件一起出去。事件日志仍然只记世界里发生的事。
"""

from __future__ import annotations

import time
from typing import Any

# 玩家能教的规则种类。白名单是有意的:开放键位等于让分类器往库里写任意字段,
# 而提示词那一段会照着念 —— 一个错分类就能把角色的身份改掉。
OVERRIDE_KINDS: dict[str, str] = {
    "address_form": "怎么称呼玩家",
    "description_style": "动作与神态怎么写",
    "tone_preference": "口气偏好",
    "forbidden_topics": "不要提的东西",
    "nickname_for_player": "玩家的昵称",
    # **名字和称呼不是一件事**,所以它单独占一格。`address_form` 是"当面怎么叫他",
    # 而这一个是"他自己说他叫什么" —— 身份块里那句「他要是告诉了你名字,这一轮之后
    # 就照那个名字认他」要兑现,得有个地方存。存不下的下场实测过:玩家报了名字、
    # 她当场叫得出来(那一轮的原文还在上下文里),下一场开局身份块又以"最高优先级
    # 事实"的口气说「他没有告诉过你他叫什么名字」—— 而她的长期记忆里明明写着。
    "player_name": "他说他叫什么",
}

_NOT_IMPLEMENTED = "存储归子类(redis_state.RedisChatStateStore)"


class ChatStateStore:
    """聊天当前值存储的纯逻辑基类。存储原语归子类,派生逻辑在这里。

    子类要自备:`self._lock`(进程内线程锁)、`self._transcript`(转录存储,
    `transcript` 属性经它转发消息行上的观测量)。没有 `__init__`:这一层
    不持有任何存储状态。
    """

    @staticmethod
    def _now() -> int:
        return int(time.time())

    # ── stance(#18) ─────────────────────────────────────────────────────────

    def stance(self, agent_id: str, target_id: str) -> dict[str, Any] | None:
        """她对 target 的当前 stance:`{stance, declared, updated_tick,
        updated_at}`,没有则 None。存储归子类。"""
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def set_stance(
        self, agent_id: str, target_id: str, stance: str, *,
        declared: bool = True, tick: int = 0,
    ) -> None:
        """按 (agent, target) 覆盖写。子类实现必须先经 `stance_mod.is_stance`
        校验(未知枚举 ValueError)。存储归子类。"""
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def stances(self, agent_id: str) -> list[dict[str, Any]]:
        """她对所有人的 stance,按 target 排序。存储归子类。"""
        raise NotImplementedError(_NOT_IMPLEMENTED)

    # ── 静音 / 等会儿再说(#15) ──────────────────────────────────────────────

    def set_quiet(
        self, agent_id: str, player_id: str, *, kind: str = "mute",
        minutes: float, reason: str | None = None, now: int | None = None,
    ) -> int:
        """静音到某个墙钟时刻,返回过期时间戳。kind 只认 "mute" / "delay"
        (其余 ValueError)。**墙钟而不是 tick** —— 见模块 docstring。
        存储归子类。"""
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def quiet_until(
        self, agent_id: str, player_id: str, *, now: int | None = None
    ) -> dict[str, Any] | None:
        """她这会儿不理这个玩家吗?返回最晚的那条(`{kind, expires_at, reason,
        seconds_left}`),过期的顺手清掉 —— 读到的就是此刻还成立的。存储归子类。"""
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def clear_quiet(self, agent_id: str, player_id: str, *, kind: str | None = None) -> None:
        """清掉静音;kind=None 清全部。存储归子类。"""
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def mutes(self, agent_id: str | None = None, *, now: int | None = None) -> list[dict[str, Any]]:
        """未过期的静音,按 expires_at 排序;agent_id=None 看全世界。存储归子类。"""
        raise NotImplementedError(_NOT_IMPLEMENTED)

    # ── 拒谈话题(#15) ──────────────────────────────────────────────────────

    def refuse_topic(
        self, agent_id: str, keyword: str, *, minutes: float | None = None,
        now: int | None = None,
    ) -> None:
        """按 (agent, keyword) 覆盖写;minutes=None 永久。空 keyword ValueError。
        存储归子类。"""
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def refused_topics(self, agent_id: str, *, now: int | None = None) -> list[dict[str, Any]]:
        """她还在拒谈的话题(`{keyword, expires_at}`),过期的顺手清掉。存储归子类。"""
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def topics_hit_by(self, agent_id: str, text: str, *, now: int | None = None) -> list[str]:
        """这条消息又踩到了哪些她拒绝谈的话题。(纯派生:只依赖 `refused_topics`。)"""
        lowered = (text or "").lower()
        return [
            row["keyword"] for row in self.refused_topics(agent_id, now=now)
            if row["keyword"].lower() in lowered
        ]

    # ── 回头找你的约(#15 delay_reply) ──────────────────────────────────────

    def add_followup(
        self, agent_id: str, player_id: str, *, due_tick: int,
        kind: str = "delayed_reply", reason: str | None = None, created_tick: int = 0,
    ) -> int:
        """记一条"到第 due_tick 回来敲门",返回自增 id。**tick 而不是墙钟** ——
        见模块 docstring。存储归子类。"""
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def due_followups(self, tick: int) -> list[dict[str, Any]]:
        """到点且未兑现的约,按 (due_tick, id) 排序。存储归子类。"""
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def mark_followup_fired(self, followup_id: int, tick: int) -> None:
        """标记一条约已兑现(记下 fired_tick)。存储归子类。"""
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def pending_followups(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        """未兑现的约,按 (due_tick, id) 排序;agent_id=None 看全部。存储归子类。"""
        raise NotImplementedError(_NOT_IMPLEMENTED)

    # ── persona override(#16) ───────────────────────────────────────────────

    def set_override(self, agent_id: str, player_id: str, kind: str, value: str) -> None:
        """后到覆盖(同 kind 唯一):玩家改主意就是改主意。子类实现必须先按
        `OVERRIDE_KINDS` 白名单校验 kind、拒绝空 value(ValueError)。存储归子类。"""
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def overrides(self, agent_id: str, player_id: str) -> list[dict[str, Any]]:
        """玩家教过她的规则(`{kind, value, updated_at}`),按 kind 排序。存储归子类。"""
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def clear_override(self, agent_id: str, player_id: str, kind: str) -> bool:
        """删一条规则,返回是否真的删了。存储归子类。"""
        raise NotImplementedError(_NOT_IMPLEMENTED)

    # ── 一个人离开了这个世界 ────────────────────────────────────────────────

    def forget_player(self, player_id: str) -> int:
        """这个玩家走了 —— 清掉这几张表里**朝前看**的行,返回清了几行。

        `World.forget_player` 那条路上的一环。这一层只清"她还准备对他做什么"
        (stance / 静音 / 回头找你 / 他教过的规则);历史(转录、消息行上的
        观测量)一个字不改 —— 那是他真的说过的话。存储归子类。
        """
        raise NotImplementedError(_NOT_IMPLEMENTED)

    # ── 一轮的观测量:转发给转录存储 ────────────────────────────────────────
    #
    # 这两个碰的是转录(`messages`),不是这一层的当前值。SQL 住在转录的主人那儿
    # (`MySQLChatStore` / `RedisChatStore` —— **表在谁那儿,写它的 SQL 就在谁
    # 那儿**),这里只转发,于是任何后端都不必重写、也不会继承到一条死路。

    @property
    def transcript(self) -> Any:
        """转录存储。子类构造时注入(Redis 世界的转录在 MySQL 或 Redis 上);
        没注入还来调,说明接线漏了 —— 当场报错,别让它静默吞掉观测量。"""
        store = getattr(self, "_transcript", None)
        if store is None:
            raise NotImplementedError(
                "transcript store 未注入 —— 存储归子类,构造时传 transcript"
            )
        return store

    def annotate_message(
        self, message_id: int, *, stance: str | None = None, intent: str | None = None,
        intent_confidence: float | None = None, tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        """把一轮的观测量写到消息行上。给出什么写什么,None 一律不动。"""
        self.transcript.annotate_message(
            message_id, stance=stance, intent=intent,
            intent_confidence=intent_confidence, tool_calls=tool_calls,
        )

    def conversation_meta(self, conversation_id: int) -> dict[str, Any]:
        """一场会话的 stance / intent 分布 + 调过的工具 —— 关闭事件带的那份。"""
        return self.transcript.conversation_meta(conversation_id)
