"""身份块:**名字**和**称呼**是两件事。

真世界(`night-tide`)里逮到的病:网站没把玩家的名字传进来,引擎于是兜底成
`player-5688afd1`,而身份块紧接着命令她「始终用这个名字认识和称呼对方」——
一条**必然执行不了**的命令。真模型做了唯一合理的事:照着旁边那格身份
(网站填的是中文的「旅人」)编了一个称呼。然后:

    她说「你好,旅人」→ 进转录 → 进会话摘要 → 进 0.8 重要度的长期记忆

四条 `user_conversation` 记忆里玩家从此叫「旅人」,而他叫刘俊康,从没这么说过。
**照跑、报成功、日志一行不错** —— 这个仓库最怕的那类错。

修法不是"把兜底名字换得好看一点",是把两件事分开:

- **称呼**可以有(她总得叫他点什么),没名字时用身份、再不行用「访客」;
- **名字**没有就是没有。他问起自己叫什么,照实说不知道 —— 那才是这个世界里
  真实发生过的事,而且它是可以被下一句话改变的:他一说,她就知道了。

这里盯五件事,每一条都对应上面那条链上的一环。
"""
from __future__ import annotations

from _worldfile import open_world_at

import pytest

from anima_world.chat_service import DEFAULT_ADDRESS, address_for


@pytest.fixture()
def world(tmp_path):
    w = open_world_at(str(tmp_path / "world.db"), force_mock_llm=True)
    w.config_set("chat.intent.enabled", False)
    yield w
    w.close()


def _agent(world) -> str:
    return next(iter(world.state()["agents"]))


def _identity(world, **kwargs) -> str:
    seen = world.debug_prompt(_agent(world), **kwargs)
    blocks = {b["label"]: b["text"] for b in seen["blocks"]}
    assert "identity" in blocks, "身份块永远在场 —— 它不该有缺席这一说"
    return blocks["identity"]


def test_没报名字时不许凭空发明一个名字(world):
    """兜底名字是这条链的源头。`player-3f9a2c` 念不出口,她就会自己造一个。"""
    text = _identity(world, player_id="5688afd1-069f-45e8", role="旅人")

    assert "player-5688afd1" not in text, "把 id 当名字塞进提示词 —— 她念不出来只能编"
    assert "没有告诉过你他叫什么名字" in text
    # 称呼给了,而且是那个人话身份
    assert "「旅人」" in text


def test_称呼不许升格成名字(world):
    """他问「我叫什么」的时候,答案必须是"不知道",不是那个称呼。"""
    text = _identity(world, player_id="p1", role="旅人")

    assert "那是称呼，不是名字" in text
    assert "照实说你还不知道" in text
    assert "不许把「旅人」当成他的名字说出口" in text
    assert "不许自己给他起一个" in text
    # 而且这扇门是开着的:他说了,她就该改口
    assert "他要是告诉了你名字" in text


def test_报了名字就照名字认人(world):
    text = _identity(world, player_id="p1", display_name="刘俊康", role="旅人")

    assert "正在与你交谈的人是 刘俊康" in text
    assert "（身份：旅人）" in text, "名字和身份两格都要有 —— 它们不是一件事"
    assert "没有告诉过你" not in text
    # 称呼那一格交给 overrides:玩家教过别的叫法时,两块不该顶牛
    assert "按他教的来" in text


def test_机器默认的_role_不当身份也不当称呼(world):
    """`role="player"` 是**宿主没说身份**时那个参数的默认值,不是一句人话。

    印成「身份：player」是噪音,当成称呼("你好,player")更糟。
    """
    text = _identity(world, player_id="p1", role="player")

    assert "身份：player" not in text and "身份是player" not in text
    assert f"「{DEFAULT_ADDRESS}」" in text
    assert address_for("player") == DEFAULT_ADDRESS
    assert address_for("旅人") == "旅人"
    assert address_for("") == DEFAULT_ADDRESS


def test_调试视图不许拿默认的_role_盖掉世界里的身份(world):
    """这条是被自己坑过的:`debug_prompt(role=…)` 的默认值曾经是 `"player"`。

    于是同一个世界、同一个玩家,真聊天里她读到「身份是旅人」,调试视图里是
    「身份是player」—— 而调试视图存在的全部理由就是"它和真的一样"。
    """
    agent = _agent(world)
    world.player_move("p1", "cafe", role="旅人")

    text = _identity(world, player_id="p1")
    assert "「旅人」" in text, "不传 role 时要读世界里那份,而不是参数默认值"

    # 位置那几句讲的是"谁在哪",用称呼 —— 没名字时也不许漏出一个空名字
    world.chat_reply(agent, [{"role": "user", "content": "在吗"}], player_id="p1")
    after = _identity(world, player_id="p1")
    assert "和你都在" not in after or "旅人和你都在" in after
    assert "当前在" not in after or "旅人当前在" in after

def test_玩家教过的叫法不许被身份块当场否掉(world):
    """一份提示词里不许有两条顶着的指令,尤其是**两条都是引擎自己写的**。

    实测:玩家说「以后叫我小林」→ 意图分类判成 `style_adjust` → 规则落库 →
    `overrides` 块照着说「怎么称呼玩家:小林」。而紧接着自称"最高优先级事实"
    的身份块还在说「他**没有告诉过你他叫什么名字**……不许自己给他起一个」,
    称呼那一格填的是兜底的「访客」。

    两条指令顶着的下场是模型摇摆(见 CHANGELOG 2.1.0 末尾那条)。而名字和称呼
    照旧是两件事:她照他教的叫,但那仍然不等于她知道他的本名。
    """
    agent = _agent(world)
    world.set_persona_override(agent, "p1", "address_form", "小林")

    text = _identity(world, player_id="p1", role="旅人")

    assert "让你叫他「小林」" in text
    assert "没有告诉过你他叫什么名字" not in text, "身份块正在否掉玩家刚教过的叫法"
    assert "「访客」" not in text and "「旅人」" not in text
    # 教过的叫法 ≠ 本名,这道分界不许一起丢掉
    assert "不一定是他的本名" in text
    assert "别把「小林」说成是他的本名" in text


def test_走一步不许把门口填的身份冲掉(world):
    """真人试玩逮到的,两个世界上各复现一次(阿檀「刚搬来的人」、阿远「旅人」)。

    玩家在门口填了身份,走一步就变回 `player` —— 因为 `player_walk` 的 `role`
    默认值是**字面量** `"player"` 并且无条件写下去,而走路那条路(`player_do_action`
    的 walk 分支、`player_tool`)压根没有 role 可传。于是"我知道他的身份是 player"
    和"我这条路上拿不到身份"变成了同一件事。

    **而这次损坏被占位身份的过滤器藏住了**:`chat_service` 把 `player` 当占位身份
    整段丢掉,于是身份块里那句「（身份：…）」只是安静地消失 —— 线上照跑、日志一行
    不错,只有当面读她的提示词才看得出来。所以这里两头都验:世界里存的那一行、
    以及她真读到的那段字。

    修法在**唯一那道窄口** `_touch_player`:`role=""` 是"这一路不知道他是谁",
    只填缺不覆盖 —— 和读那一侧的 `_interlocutor_for` 逐字同构。挨个给下游入口
    补参数是治不好的,下一个新入口照样会漏。
    """
    world.player_move("p1", "cafe", role="旅人")
    assert "（身份：旅人）" in _identity(world, player_id="p1", display_name="刘俊康")

    # 走路那条真路:玩家点的是工具,工具手上没有 role
    assert world._tool_runtime.player_do_action("p1", "walk", {"location": "workshop"})
    assert (world.presence_store.get("p1") or {}).get("role") == "旅人"
    assert "（身份：旅人）" in _identity(world, player_id="p1", display_name="刘俊康")

    # 同一条纪律在另外两个入口上:`player_tool` 连 role 这个形参都没有,
    # `player_action` 落的是**不可改的历史**,写空了以后谁都补不回来
    world.player_tool("p1", "walk", {"location": "home"})
    world.player_action("p1", "idle_wander", {})
    assert (world.presence_store.get("p1") or {}).get("role") == "旅人"
    walked = [e for e in world.events() if e["type"] == "player_action"]
    assert walked and walked[-1]["role"] == "旅人", walked[-1]


def test_聊天这条路要真的写得进身份(world):
    """同一个 bug 的第三条路,而且它**活过了**上面那条的修法。

    走路那条路修好之后还剩两处字面量 `"player"`:建行时播的那一格,和四个聊天
    入口的 `role` 默认值。合起来的下场是这样 ——

    1. 玩家先点了一下"走"(那条路手上没有身份)→ 建行,行里躺着 `"player"`;
    2. 他再开口聊天,宿主这一轮**给了**身份 → 从前这里是
       `players[pid].setdefault("role", role)`,而那一格永远有值,于是这句
       **永远是空操作**:聊天这条路一次都没写进过身份,而它读起来像写了;
    3. `chat_service` 把 `player` 当占位身份整段丢掉 —— 他在这个世界里从此
       没有身份,聊多少轮都改不回来,一行日志都不会说。

    反过来那一半同样要钉:`chat()` 的 `role` 默认值从前是字面量 `"player"`,
    而 `_interlocutor_for` 里 `role or known["role"]` 让非空的它**压过**世界
    记着的那个 —— 修完写这一侧之后,一个不传 role 的宿主会亲手把玩家在门口
    填的身份冲成 `player`。**是这次修复自己会打开的洞**,所以两头一起验。
    """
    # 1) 建行的常常正是没有身份的那条路 —— 那一格该是空的,不是一个假身份
    world.player_tool("p1", "walk", {"location": "workshop"})
    assert (world.presence_store.get("p1") or {}).get("role") == ""

    # 2) 宿主开口时给了身份 —— 聊天这条路必须真的写进去
    agent = _agent(world)
    world.chat_reply(agent, [{"role": "user", "content": "在吗"}],
                     player_id="p1", role="旅人")
    assert (world.presence_store.get("p1") or {}).get("role") == "旅人"
    assert "（身份：旅人）" in _identity(world, player_id="p1", display_name="刘俊康")

    # 3) 下一轮宿主没给 —— 只填缺不覆盖,不许拿默认值把它冲掉
    world.chat_reply(agent, [{"role": "user", "content": "还在吗"}], player_id="p1")
    assert (world.presence_store.get("p1") or {}).get("role") == "旅人"
    assert "（身份：旅人）" in _identity(world, player_id="p1", display_name="刘俊康")


def test_点名要的昵称压过称呼形式(world):
    """两条规则都在时,`nickname_for_player` 赢 —— 昵称是他点名要的那一个,
    `address_form` 更像口气偏好(「别那么客气」)。"""
    agent = _agent(world)
    world.set_persona_override(agent, "p1", "address_form", "林先生")
    world.set_persona_override(agent, "p1", "nickname_for_player", "小林")

    text = _identity(world, player_id="p1", role="旅人")
    assert "让你叫他「小林」" in text and "林先生" not in text


def test_他报过的名字下一场还在(world):
    """一次真的对局逼出来的,而且**是引擎自己许的诺**:身份块两支结尾都写着
    「他要是告诉了你名字,这一轮之后就照那个名字认他」——而在这之前没有一行代码
    兑现它。玩家打字「我叫林越」,她当场叫得出来(那一轮的原文还在上下文里),
    下一场开局身份块又以"最高优先级事实"的口气说「他没有告诉过你他叫什么名字」,
    而她的长期记忆里明明写着林越。

    落点是 (角色, 玩家) 上的 `player_name`,不是 `display_name` —— 后者是宿主
    认证过的身份(纪律 3),一次正则猜测不许升格成它。所以身份块说的是「他**告诉
    过你**」,不是「他是」。
    """
    agent = _agent(world)
    list(world.chat(agent, [{"role": "user", "content": "你好，我叫林越"}], player_id="p1"))

    kinds = {r["kind"]: r["value"] for r in world.persona_overrides(agent, "p1")}
    assert kinds.get("player_name") == "林越", kinds

    text = _identity(world, player_id="p1", role="旅人")
    assert "告诉过你他叫「林越」" in text
    assert "没有告诉过你他叫什么名字" not in text, "他报过名字了,身份块还在说他没报"
    assert "「访客」" not in text and "「旅人」" not in text


def test_名字和要你怎么叫他可以是两个词(world):
    """「我叫林越，你叫我小林就行」——本名一格、叫法一格,两格都要在,而且不许打架。"""
    agent = _agent(world)
    list(world.chat(agent, [{"role": "user", "content": "我叫林越，你叫我小林就行"}],
                    player_id="p1"))

    kinds = {r["kind"]: r["value"] for r in world.persona_overrides(agent, "p1")}
    assert kinds.get("player_name") == "林越"
    assert kinds.get("address_form") == "小林"

    text = _identity(world, player_id="p1", role="旅人")
    assert "告诉过你他叫「林越」" in text
    assert "叫他「小林」" in text
    assert "不一定是他的本名" not in text, "他报了本名,这句话在否掉它"


def test_报名字不吃掉他这一轮的话(world):
    """记下来之后**必须放行**。判成 `style_adjust` 的版本会回一句
    「(记下了:…)」的系统回执,把他刚说的话整个吞掉 —— 玩家自报家门收到一张回单,
    比不记还伤。"""
    agent = _agent(world)
    reply = "".join(world.chat(agent, [{"role": "user", "content": "我叫林越"}],
                               player_id="p1"))
    assert reply.strip(), "这一轮没有回话 —— 他的话被吞了"
    assert "记下了" not in reply


def test_宿主报过一次的名字第二轮不传也还认得他(world):
    """真人对局逼出来的:宿主第一轮传了 `display_name="林越"`,**世界已经被告知过**,
    第二轮它没再传(CLI 不给 `--name` 时传的就是空串),她当场又不认识他了。

    这一条和「不许兜底一个假名字」不冲突:回落取的是世界自己记着的那一格,而那一格
    只有一个来源 —— 上一次宿主亲口传进来的 `display_name`。出处仍然是宿主,纪律 3
    没有松;松的是"世界明明记得却装作不知道"。
    """
    agent = _agent(world)
    list(world.chat(agent, [{"role": "user", "content": "在吗"}],
                    player_id="p1", display_name="林越", role="旅人"))

    # 空串 = 宿主这一轮没说,不是"他没有名字"
    text = _identity(world, player_id="p1", display_name="", role="旅人")
    assert "正在与你交谈的人是 林越" in text
    assert "没有告诉过你他叫什么名字" not in text, "世界记着他叫林越,身份块却说不认识"

    # None 走同一支 —— 两个"宿主没说"的写法不许给出两种身份
    assert _identity(world, player_id="p1", display_name=None, role="旅人") == text


def test_宿主不传名字时不许把世界记着的名字冲掉(world):
    """比"这一轮不认识"更坏的那一半:空串会顺着 `_touch_player(name=…)` **写回去**,
    把上一轮宿主告诉过的名字抹成空。于是第一轮认得他、从第二轮起永远不认识,
    而日志一行不错 —— 这个仓库最怕的那类错。
    """
    agent = _agent(world)
    list(world.chat(agent, [{"role": "user", "content": "在吗"}],
                    player_id="p1", display_name="林越", role="旅人"))
    list(world.chat(agent, [{"role": "user", "content": "还在吗"}],
                   player_id="p1", display_name="", role="旅人"))

    assert (world.players["p1"] or {}).get("name") == "林越", "宿主没说话,世界却把名字忘了"
    assert "正在与你交谈的人是 林越" in _identity(world, player_id="p1", role="旅人")


def test_宿主从没说过名字就绝不许回落出任何名字(world):
    """上一条的**反向闸**:回落只准落到"宿主上次告诉过的那一格",空的时候就是空的。

    放松成"随便找个词填上"就是 `night-tide` 那条链的源头(见本文件开头)——
    所以这里连玩家 id 都不许漏进提示词。
    """
    agent = _agent(world)
    list(world.chat(agent, [{"role": "user", "content": "在吗"}],
                    player_id="5688afd1-069f-45e8", display_name="", role="旅人"))

    assert not (world.players["5688afd1-069f-45e8"] or {}).get("name"), "凭空记下了一个名字"
    text = _identity(world, player_id="5688afd1-069f-45e8", display_name="", role="旅人")
    assert "没有告诉过你他叫什么名字" in text
    assert "5688afd1" not in text, "把 id 当名字塞进提示词 —— 她念不出来只能编"
    assert "「旅人」" in text


def test_他改口要走明路不被正则悄悄改掉(world):
    """只填空,不覆盖:改称呼是 `style_adjust` 那条明路的事。一句偶然命中的正则
    不该把作者/玩家已经定下的规则换掉。"""
    agent = _agent(world)
    world.set_persona_override(agent, "p1", "player_name", "林越")
    list(world.chat(agent, [{"role": "user", "content": "我叫王二"}], player_id="p1"))

    kinds = {r["kind"]: r["value"] for r in world.persona_overrides(agent, "p1")}
    assert kinds["player_name"] == "林越"
