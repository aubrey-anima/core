"""`World.debug_prompt()`:看一眼她这一刻收到的提示词。

这一层为什么值得有测试盯着:1.3 开发期四个 bug 有三个在提示词里(stance 声明率
2/6、能力一次没用、定时轮次 18 轮 0 动作),而当时唯一的诊断办法是往 chat_service
的私有属性上塞一个假 LLM 去偷看。宿主和世界作者一个都没有。

四件事,第一条是全部价值所在:

1. **它不撒谎** —— 交出来的必须和真聊天送进 LLM 的那一段**逐字相同**。一个会分叉的
   调试视图比没有更坏:你会照着它去改一个不存在的问题。
2. **它解释缺席** —— 少一块几乎总比多一块难查(世界照跑、她照说话,只是从来没提
   那棵树),所以"哪块没出现、为什么"和"哪块出现了"一样是答案。
3. **它不留副作用** —— 静音中的角色也照样交出提示词(而 `chat()` 会当场拒),
   而且不写 `players.last_seen`。看,但不碰。
4. **块顺序是钉住的** —— 位置就是权重(实测:两块从中间移到末尾,声明率 2/6 → 5/6)。
   顺序钉在 `PROMPT_BLOCK_ORDER` 上,新加一块就必须显式决定它排第几。
5. **它说得出这一份是拿谁算的**(`asker`) —— 第 1 条有个前提:同一个函数还不够,
   喂给它的**人**也得是真的。世界不认得的那个 id 会让身份/在场/关系三块整个换一套
   算法,而它渲染得毫无破绽。见文件末尾那一组。
"""
from __future__ import annotations

from _worldfile import open_world_at, run_cli

import json
import subprocess
import sys

import pytest

from anima_world.api import AgentUnavailable, World
from anima_world.chat_service import PROMPT_BLOCK_ORDER


class Spy:
    """记下它收到的 messages,回一句无害的话。"""

    def __init__(self) -> None:
        self.prompts: list[list[dict]] = []

    def _record(self, messages) -> str:
        self.prompts.append([dict(m) for m in messages])
        return "好。"

    async def stream(self, messages):
        yield self._record(messages)

    async def complete(self, messages) -> str:
        return self._record(messages)

    @property
    def system(self) -> str:
        return self.prompts[0][0]["content"]


@pytest.fixture()
def world(tmp_path):
    w = open_world_at(str(tmp_path / "world.db"), force_mock_llm=True)
    w.config_set("chat.intent.enabled", False)  # 分类会多跑一次 LLM,与这里无关
    yield w
    w.close()


@pytest.fixture()
def bare_world(bare_seed, tmp_path):
    """空橱窗的世界:出厂种子自带可见性声明与量,验"缺席"要一个干净的底。"""
    w = open_world_at(str(tmp_path / "bare.db"), seed_path=bare_seed, force_mock_llm=True)
    w.config_set("chat.intent.enabled", False)
    yield w
    w.close()


def _an_agent(world: World) -> str:
    return sorted(world.scheduler.agents)[0]


def test_debug_prompt_is_byte_identical_to_what_the_model_gets(world, tmp_path):
    """全部价值在这一条:调试视图 == 真提示词。

    两条路各写一遍拼装就会分叉 —— 所以它们共用 `ChatService.prompt_blocks`,
    这条测试是那个共用的证据。改拼装而只改了一条路,这里会红。
    """
    agent = _an_agent(world)
    world.config_set("chat.stance.enabled", True)
    world.config_set("chat.tools.enabled", True)
    world.declare_visibility("world", "季节", "public", label="季节")
    world.set_stock("world", "季节", 2)
    world.player_move("p1", world._tool_runtime.agent_location(agent))

    spy = Spy()
    world.chat_service._llm = spy
    world.chat_reply(
        agent,
        [{"role": "user", "content": "在吗"}],
        player_id="p1",
        display_name="阿檀",
    )

    seen = world.debug_prompt(
        agent, player_id="p1", display_name="阿檀", message="在吗"
    )
    assert seen["system"] == spy.system
    assert seen["system_chars"] == len(spy.system)
    # 而且真的拆开了 —— 不是把整段塞进一个块里假装分了块
    assert len(seen["blocks"]) >= 5
    assert "\n\n".join(b["text"] for b in seen["blocks"]) == spy.system


def test_block_order_follows_the_pinned_order(world):
    """实际顺序必须是 `PROMPT_BLOCK_ORDER` 的子序列。

    钉住它是为了让"往末尾加块"变成一个显式决定:末尾只有一个,抢的人多了就不值钱。
    新加一块而没想清楚它属于"事实"还是"要照做的",这条会红。
    """
    agent = _an_agent(world)
    world.config_set("chat.stance.enabled", True)
    world.config_set("chat.tools.enabled", True)
    world.declare_visibility("world", "季节", "public")
    world.set_stock("world", "季节", 2)
    world.set_persona_override(agent, "p1", "address_form", "叫我阿檀")

    order = world.debug_prompt(agent, player_id="p1", display_name="阿檀")["order"]
    assert order, "一块都没有,拼装坏了"
    assert len(order) == len(set(order)), f"有块重复出现:{order}"
    positions = [PROMPT_BLOCK_ORDER.index(label) for label in order]
    assert positions == sorted(positions), (
        f"实际顺序 {order} 不符合 PROMPT_BLOCK_ORDER —— "
        "要么改回来,要么改 PROMPT_BLOCK_ORDER 并说明为什么"
    )
    # 末尾那两块定的是"她要不要做点什么",必须是她最后读到的(位置就是权重)
    assert order[-2:] == ["stance", "tools"]


def test_absence_is_explained_not_just_omitted(bare_world):
    """少一块要说清为什么 —— 照着那句话就能让它出现。

    用 `bare_seed` 起世界:**出厂世界自己就声明了可见性**(橱窗要展示感知层),
    所以在默认世界里 perception 永远在场,验不到"从没声明过"这条路。
    """
    world = bare_world
    agent = _an_agent(world)
    world.config_set("chat.stance.enabled", False)
    world.config_set("chat.tools.enabled", False)

    absent = world.debug_prompt(agent, player_id="p1")["absent"]
    assert "chat.stance.enabled" in absent["stance"]
    assert "chat.tools.enabled" in absent["tools"]
    # 没声明可见性时,要指向 declare_visibility —— 这是最容易"照跑但少一块"的一处:
    # 量都写好了、规律也在算,她就是感知不到,而默认 hidden 是故意的
    assert "declare_visibility" in absent["perception"]
    # identity 不该出现在 absent 里:没传 display_name 时真聊天也会兜底成
    # `player-xxxx`,所以它永远在场 —— 给它写缺席理由就是死代码假装解释
    assert "identity" not in absent

    # 打开之后,这几块就不该再出现在 absent 里
    world.config_set("chat.stance.enabled", True)
    world.config_set("chat.tools.enabled", True)
    world.declare_visibility("world", "季节", "public")
    world.set_stock("world", "季节", 2)
    after = world.debug_prompt(agent, player_id="p1", display_name="阿檀")
    assert "stance" not in after["absent"]
    assert "tools" not in after["absent"]
    assert "perception" not in after["absent"]


def test_declared_but_unreachable_says_so(bare_world):
    """声明过、却一个都感知不到 —— 和"从没声明过"是两种病,不能报同一句话。"""
    world = bare_world
    agent = _an_agent(world)
    world.declare_visibility("mine", "储量", "hidden", label="矿脉储量")
    world.set_stock("mine:north", "储量", 800)

    why = world.debug_prompt(agent, player_id="p1")["absent"]["perception"]
    assert "declare_visibility" not in why
    assert "1 条" in why


def test_looking_does_not_touch(world):
    """看,但不碰:不写 last_seen、静音中的角色也照样交出提示词。

    静音那条尤其要紧 —— 她不理人的时候恰恰是你最想知道"她收到了什么"的时候,
    而 `chat()` 这时会抛 AgentUnavailable。调试入口跟着一起拒就等于没有。
    """
    agent = _an_agent(world)
    before = dict(world.players.get("p1") or {})

    world.chat_state.set_quiet(agent, "p1", kind="mute", minutes=60)
    with pytest.raises(AgentUnavailable):
        world.chat_reply(agent, [{"role": "user", "content": "在吗"}], player_id="p1")

    seen = world.debug_prompt(agent, player_id="p1", display_name="阿檀")
    assert seen["blocks"], "静音中就交不出提示词 = 最需要它的时候它不在"
    assert dict(world.players.get("p1") or {}) == before, "看一眼却改了玩家状态"


def test_unknown_agent_is_a_keyerror(world):
    with pytest.raises(KeyError):
        world.debug_prompt("并不存在的人")


# ---- 人用的那道门 ---------------------------------------------------------


def _cli(*argv: str) -> subprocess.CompletedProcess:
    return run_cli(*argv)


def _a_world(tmp_path) -> str:
    db = tmp_path / "cli.db"
    done = _cli("simulate", "--world-id", "w", "--ticks", "0", "--llm", "mock")
    assert done.returncode == 0, done.stderr
    return str(db)


def test_cli_prompt_shows_blocks_and_absences(tmp_path):
    """`anima-world prompt` 是这道门的意义:改模板的人不一定写 Python。

    世界作者动的是 `prompt_templates` 里的模板,而"改完她到底收到了什么"过去只有
    写 Python 塞假 LLM 才看得到。
    """
    db = _a_world(tmp_path)
    done = _cli("prompt", "--world-id", "w", "--agent", "夏", "--name", "阿檀")
    assert done.returncode == 0, done.stderr
    assert "persona" in done.stdout and "identity" in done.stdout
    assert "没出现的块" in done.stdout
    # 摘要模式不许倒正文 —— 默认就吐几千字等于又看不见了,那是 `--full` 的活。
    # 验的是正文行的那个前缀(和 `--full` 那条测试互为反面)
    assert "      │ " not in done.stdout


def test_cli_prompt_full_and_json(tmp_path):
    db = _a_world(tmp_path)
    full = _cli("prompt", "--world-id", "w", "--agent", "夏", "--full")
    assert full.returncode == 0, full.stderr
    assert "      │ " in full.stdout, "--full 没有把正文打出来"

    as_json = _cli("prompt", "--world-id", "w", "--agent", "夏", "--json")
    assert as_json.returncode == 0, as_json.stderr
    payload = json.loads(as_json.stdout)
    assert payload["system"] == "\n\n".join(b["text"] for b in payload["blocks"])


def test_cli_prompt_without_agent_lists_the_roster(tmp_path):
    """不给 --agent 就列名册,和 `chat` 一个规矩 —— 不猜她是谁。"""
    db = _a_world(tmp_path)
    done = _cli("prompt", "--world-id", "w")
    assert done.returncode == 0, done.stderr
    assert "夏" in done.stdout

    missing = _cli("prompt", "--world-id", "w", "--agent", "并不存在的人")
    assert missing.returncode == 2
    assert "没有" in missing.stderr


# ---- 一段提示词只读一次世界 -----------------------------------------------


def test_one_prompt_reads_the_world_exactly_once(world):
    """在场块和身份声明必须用**同一份**世界快照。

    各读一次的后果不是慢(两次一共 1.5ms),是**同一段提示词自相矛盾**:两次读之间
    时钟线程推进了一格,于是提示词里同时出现"你在咖啡店,同在这里的还有:没有别人"
    和"你当前在建筑工作室,因此对话媒介是手机私聊" —— 还顺手禁止她描写站在面前的
    玩家。LLM 会挑一边编,而且**无声**。`in_transit` 那道闸修的是同一种病的另一扇门。
    """
    agent = _an_agent(world)
    world.player_move("p1", world._tool_runtime.agent_location(agent))

    real = world.chat_service._world_provider
    reads: list[str] = []

    def counting(agent_id, interlocutor_id):
        reads.append(agent_id)
        return real(agent_id, interlocutor_id)

    world.chat_service._world_provider = counting
    world.chat_service._llm = Spy()
    world.chat_reply(
        agent, [{"role": "user", "content": "在吗"}], player_id="p1", display_name="阿檀"
    )
    assert len(reads) == 1, f"一轮聊天读了 {len(reads)} 次世界 —— 块之间会互相打脸"

    reads.clear()
    world.debug_prompt(agent, player_id="p1", display_name="阿檀")
    assert len(reads) == 1, f"debug_prompt 读了 {len(reads)} 次"


def test_presence_and_identity_never_contradict(world):
    """世界在两块之间变了,提示词也不许自相矛盾 —— 因为只读一次,后来的变化看不到。"""
    agent = _an_agent(world)
    here = world._tool_runtime.agent_location(agent)
    world.player_move("p1", here)

    real = world.chat_service._world_provider
    calls = {"n": 0}

    def drifting(agent_id, interlocutor_id):
        """第二次读就说她已经走了 —— 修好之后第二次读根本不该发生。"""
        ctx = dict(real(agent_id, interlocutor_id) or {})
        calls["n"] += 1
        if calls["n"] >= 2:
            ctx["presence"] = {
                **(ctx.get("presence") or {}),
                "location_id": "studio",
                "location": "建筑工作室",
            }
        return ctx

    world.chat_service._world_provider = drifting
    blocks = {
        b["label"]: b["text"]
        for b in world.debug_prompt(agent, player_id="p1", display_name="阿檀")["blocks"]
    }
    face_to_face = "面对面" in blocks["identity"]
    same_place = "手机" not in blocks["identity"]
    assert face_to_face and same_place, (
        "身份声明说不在一起,而在场块说在一起 —— 两块用了不同的快照:\n"
        f"presence: {blocks['presence']}\nidentity: {blocks['identity']}"
    )


def test_the_place_she_is_walking_to_is_named_in_chinese(world):
    """进提示词的地名一律是人话,一处都不能漏。

    实测漏的是行程的目的地:她读到「你在建筑工作室，正在去cafe的路上」——
    同一句话里一个地方用人话、另一个用 id,而她会照着把 `cafe` 念出口。
    这类错不报错、测试不红,只是出戏。
    """
    agent = _an_agent(world)
    here = world._tool_runtime.agent_location(agent)
    destination = next(
        pid for pid in world._tool_runtime.point_ids() if pid != here
    )
    world.player_move("p1", here)
    world.scheduler._transit[agent] = {
        "from": here, "to": destination,
        "arrive_at": world.scheduler.clock + 10,
    }

    blocks = {
        b["label"]: b["text"]
        for b in world.debug_prompt(agent, player_id="p1")["blocks"]
    }
    presence = blocks["presence"]
    expected = world._tool_runtime.point_names()[destination]
    assert destination not in presence, f"她读到的是地点 id:{presence}"
    assert expected in presence, presence


def test_是她走开的_这件事得说出口(world):
    """一场面对面的对话说到一半,排班可以把她挪走 —— 而此前这件事**对谁都不留
    一个字**。

    世界的时钟不等人,所以"挪走"本身是对的;错的是没人被告知。玩家读到的是
    「走吧，从这儿过去十来分钟。你跟紧点」,然后她就没影了(线上现场,两次)。
    她自己读到的只有一句静静翻过去的「因此对话媒介是手机文字私聊」—— 于是她照着
    转录的惯性接着写在原地做事:苏念人在潮汐里 3 号,却在擦咖啡车的台子、把磨豆机
    收进架子。世界的事实进提示词、话由她说,是这个引擎一贯的做法;而这一条事实
    从来没进过提示词。

    只在她**刚好是从他站的那个地方**走开时说 —— 那时这句话必定成立,也正是
    "他被落下了"的那一种。她一早从家里出来的那种与这场对话无关。
    """
    agent = _an_agent(world)
    here = world._tool_runtime.agent_location(agent)
    world.player_move("p1", here)
    destination = next(
        pid for pid in world._tool_runtime.point_ids() if pid != here
    )
    # 她起身走了,而他还在原地 —— 走到落地为止
    world._tool_runtime.move_agent(agent, destination)
    for _ in range(400):
        world.tick(1)
        if agent not in world.scheduler._transit:
            break
    assert world._tool_runtime.agent_location(agent) == destination, "夹具前提没成立"

    blocks = {
        b["label"]: b["text"]
        for b in world.debug_prompt(agent, player_id="p1")["blocks"]
    }
    identity = blocks["identity"]
    here_name = world._tool_runtime.point_names()[here]
    there_name = world._tool_runtime.point_names()[destination]
    assert "是你走开的" in identity, f"她读不出这场话说到一半她走了:{identity}"
    assert here_name in identity and there_name in identity, identity


def test_她本来就不在他那儿时不说这句(world):
    """这句话只有"从他站的地方走开"那一种才成立。

    她一早从别处走到别处,和这场对话没有关系 —— 说了就是往提示词里塞噪音,
    而噪音多了她连真的那句也一起不读了。
    """
    agent = _an_agent(world)
    here = world._tool_runtime.agent_location(agent)
    others = [pid for pid in world._tool_runtime.point_ids() if pid != here]
    world.player_move("p1", others[0])          # 他在别处,而且不是她的出发地
    world._tool_runtime.move_agent(agent, others[1])
    for _ in range(400):
        world.tick(1)
        if agent not in world.scheduler._transit:
            break

    blocks = {
        b["label"]: b["text"]
        for b in world.debug_prompt(agent, player_id="p1")["blocks"]
    }
    assert "是你走开的" not in blocks["identity"], blocks["identity"]


def test_she_is_told_which_places_this_world_has(world):
    """**她去得了哪儿,得由世界告诉她。**

    `walk` 的 `location` 是必填,而在这一行出现之前整份提示词里没有一处列过这个
    世界有哪些地方 —— 她于是只能编一个(线上现场:「回声后面有个小阁楼」,而世界
    里没有这个地方),然后整件事退回散文,连一次被拒绝的记录都不留。给了必填参数
    却不给取值范围,是这一轮反复撞见的那条缝的又一处。

    ⚠️ **先把 `world.setting` 换掉再问。** 这个洞被引擎自带的那份世界观盖了很久:
    它手写着"街区只有三个地方——咖啡店(cafe)、建筑工作室(workshop)、以及一间用来
    画画的家(home)",于是演示世界和整套测试都读得到清单。作者一换掉那段话(真世界
    都会换)清单就没了 —— 不换掉它,这条测试把修法抹掉照样绿。
    """
    agent = _an_agent(world)
    world.prompt_set("world.setting", "江边的小镇,梅雨下个不停。")

    blocks = {
        b["label"]: b["text"]
        for b in world.debug_prompt(agent, player_id="p1")["blocks"]
    }
    assert "cafe" not in blocks.get("world.setting", ""), "前提没成立:世界观里还留着清单"

    presence = blocks["presence"]
    points = world._tool_runtime.point_names()
    assert points, "前提没成立:这个世界一个地方都没有"
    for pid, name in points.items():
        # **她读到的清单和 `walk` 认的那份必须是同一份**:各写一遍就迟早只有一半
        # 跟着代码走。所以逐个 id 查它的名字有没有印出来,而不是"印了几个就算"。
        assert name in presence, f"{name}({pid}) 没进她的提示词:{presence}"
        assert pid not in presence, f"她读到的是地点 id:{presence}"


# ── 拿谁作答:同一个函数不够,喂给它的人也得是真的 ─────────────────────────
#
# 真人试玩时撞的:`anima-world prompt --agent tie` 交出一份**渲染毫无破绽**的提示词,
# 而它和真的那一轮差着三块 —— 她被告知对方没报过名字、不在她跟前、这是手机私聊,
# 真玩家反倒被列成「只是同场角色,不是正在和你说话的人」。原因是默认那个 player_id
# 世界不认得:`self.players.get()` 交回空 dict,身份/在场/关系整个换了一套算法。
# 这一层原本有两条测试盯着"同一段提示词不许自相矛盾"(见上面读一次那两条),
# 它们堵的是**快照**那扇门;这是同一种病的另一扇门。


def _ghost_and_real(world):
    agent = _an_agent(world)
    world.player_move("p1", world._tool_runtime.agent_location(agent))
    return agent, world.debug_prompt(agent, player_id="没这个人"), world.debug_prompt(
        agent, player_id="p1", display_name="阿檀"
    )


def test_the_view_says_whose_turn_it_computed(world):
    """世界不认得的人身上算出来的那一份,必须自己承认。"""
    _, ghost, real = _ghost_and_real(world)
    assert ghost["asker"]["known"] is False, "拿一个世界不认得的人算的,却说认得"
    assert ghost["asker"]["player_id"] == "没这个人"
    assert real["asker"]["known"] is True
    assert real["asker"]["display_name"] == "阿檀"


def test_a_stranger_really_does_get_a_different_prompt(world):
    """`known` 那一格不是装饰 —— 两份提示词是真的不一样,而且差在最要紧的几块。

    这一条要是绿得太容易,说明前提没成立:先确认真玩家那一份说的是"面对面"。
    """
    _, ghost, real = _ghost_and_real(world)
    g = {b["label"]: b["text"] for b in ghost["blocks"]}
    r = {b["label"]: b["text"] for b in real["blocks"]}

    assert "面对面" in r["identity"], "前提没成立:真玩家那一份就没说面对面"
    assert "手机" in g["identity"], "陌生人那一份居然也算成了面对面"
    # 反过来那一半:陌生人那一份还**明令禁止**她描写站在跟前的人 —— 而真的那一轮里
    # 玩家就站在她面前。照着这一份去调模板,会把对的行为当成 bug 去改。
    assert "不得臆造看见、触碰对方" in g["identity"]
    assert "不得臆造看见、触碰对方" not in r["identity"]
    assert "阿檀" in r["identity"] and "阿檀" not in g["identity"]


def test_the_absence_reasons_belong_to_whoever_asked(world):
    """缺席理由是真话,而它解释的是一个不存在的人 —— 两件事要连着读。"""
    _, ghost, _real = _ghost_and_real(world)
    why = ghost["absent"].get("relation", "")
    assert why, "陌生人那一份连关系块缺席都不解释"
    assert ghost["asker"]["known"] is False, (
        "这条理由说得通,只是说的是一个世界不认得的人 —— `asker` 不报出来,"
        "读的人会当成真玩家身上的结论"
    )


def test_debug_prompt_never_swaps_in_someone_else(world):
    """**绝不悄悄换人顶上。** 换了就是第二种撒谎:你问的是甲,给的是乙。"""
    agent = _an_agent(world)
    world.player_move("p1", world._tool_runtime.agent_location(agent))
    seen = world.debug_prompt(agent, player_id="没这个人")
    assert seen["player_id"] == "没这个人"
    assert seen["asker"]["player_id"] == "没这个人"


def _land(player_id: str, agent_id: str = "夏") -> None:
    """在 CLI 那个世界里给一个玩家落个脚 —— CLI 与它共用 `current_client()`。"""
    from _worldfile import current_client

    w = World.open("w", redis=current_client(), force_mock_llm=True)
    try:
        w.player_move(player_id, w._tool_runtime.agent_location(agent_id))
    finally:
        w.close()


def test_cli_prompt_picks_a_real_player_and_says_who(tmp_path):
    """不给 --player-id 时去世界里找一个真的,并且把挑了谁印在抬头上。

    挑了谁不说,是另一种撒谎:同一条命令在两个世界上会以不同的人作答。
    """
    _a_world(tmp_path)
    _land("玩家甲")
    done = _cli("prompt", "--world-id", "w", "--agent", "夏")
    assert done.returncode == 0, done.stderr
    assert "玩家甲" in done.stdout, f"没说这一份是拿谁算的:{done.stdout[:400]}"
    assert "这个世界不认得" not in done.stdout, "世界里有真玩家,却还是拿幽灵算的"
    # 真玩家那一轮才有的两块 —— 幽灵那一份里它们是缺席的
    assert "relation" in done.stdout


def test_cli_prompt_owns_up_when_nobody_real_is_around(tmp_path):
    """世界里一个玩家都没有时照样交出提示词(诊断门不该因此告吹),但要说清成色。"""
    _a_world(tmp_path)
    done = _cli("prompt", "--world-id", "w", "--agent", "夏")
    assert done.returncode == 0, done.stderr
    assert "这个世界不认得" in done.stdout, (
        "拿一个世界不认得的人算出了一份提示词,却一个字都不说 —— "
        "调提示词的人会照着它去改一个不存在的问题"
    )
    assert "--player-id" in done.stdout, "没告诉人下一步该怎么看到真的那一份"
    assert done.stdout.count("块"), "光顾着警告,提示词没交出来"


def test_cli_prompt_flags_a_typo_in_the_player_id(tmp_path):
    """id 抄错了和"世界里没人"是两回事,而两种都不许静悄悄地作答。"""
    _a_world(tmp_path)
    _land("玩家甲")
    done = _cli("prompt", "--world-id", "w", "--agent", "夏", "--player-id", "玩家鉒")
    assert done.returncode == 0, done.stderr
    assert "这个世界不认得" in done.stdout and "玩家鉒" in done.stdout
    assert "抄错" in done.stdout
