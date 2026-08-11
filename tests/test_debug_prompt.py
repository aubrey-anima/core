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
