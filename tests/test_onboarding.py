"""`start` / `config` / `doctor` 的引导层:说人话的时钟、LLM 状态、CLI 往返。

world.db 退役后世界是 Redis 上的一个前缀:CLI 经 `_connect_redis` 连接,测试里
把它换成 fakeredis(monkeypatch),`--world-id` 取代了 `--db-path` 的全部角色。
keyfile / Fernet 的整组测试随密钥搬进机器配置而退役(世界里不再有 secret)。
"""
from __future__ import annotations

import pytest

from anima_world import machine_config, onboarding
from anima_world.__main__ import _coerce_config_value, main
from anima_world.config_store import ConfigStore
from anima_world.redis_state import RedisConfigBackend, meta_rows


@pytest.fixture
def world(monkeypatch):
    """一个"存在"的世界(meta 上有一行)+ 它的 ConfigStore + 接管 CLI 的连接。"""
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(
        "anima_world.__main__._connect_redis", lambda url=None: client
    )
    meta_rows(client, "world").put("created", "1")   # 只读命令的存在性守卫认这个
    store = ConfigStore(RedisConfigBackend(client, "world"))
    return client, store


# ── clock, in words ──────────────────────────────────────────────────────────


def test_packaged_default_clock_reads_as_real_time():
    # 1 tick / 300s at 5 world-minutes a tick == wall clock. If this ever stops
    # saying so, the "why does nothing happen" trap is back.
    assert "与现实 1:1" in onboarding.human_tick_rate(1 / 300, 5)
    assert "24 小时" in onboarding.human_tick_rate(1 / 300, 5)


def test_demo_clock_reads_as_minutes_per_day():
    described = onboarding.human_tick_rate(1.0, 5)
    assert "1 tick/秒" in described
    assert "5 分钟走完一个世界日" in described
    assert "300 倍速" in described


def test_stopped_clock_is_not_a_division_by_zero():
    assert onboarding.human_tick_rate(0, 5) == "时钟已停"


# ── LLM status ───────────────────────────────────────────────────────────────


def test_status_unset_carries_the_fix_command(world):
    _, store = world
    status = onboarding.llm_status(store)
    assert status.state == "unset"
    assert status.degraded
    assert "config set llm.api_key" in status.fix


def test_status_ok_masks_the_key(world):
    _, store = world
    # 密钥住机器配置,不进世界 —— ConfigStore.get 的解析顺序会读到它。
    machine_config.set_value("llm.api_key", "sk-abcdefghijklmnop")
    status = onboarding.llm_status(store)
    assert status.state == "ok"
    assert not status.degraded
    assert status.masked_key == "sk-***mnop"
    assert "sk-abcdefghijklmnop" != status.masked_key  # never the raw key


def test_no_config_store_is_its_own_state():
    assert onboarding.llm_status(None).state == "no-store"


# ── config CLI ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,value_type,expected",
    [
        ("1", "int", 1),
        ("0.00333", "float", 0.00333),
        ("true", "bool", True),
        ("off", "bool", False),
        ("LongCat-2.0", "str", "LongCat-2.0"),
    ],
)
def test_config_values_are_coerced_to_the_declared_type(raw, value_type, expected):
    # Storing "0.00333" as a str under a float key poisons the in-memory cache
    # until the next process start — the tick loop would then compare a string.
    assert _coerce_config_value(raw, value_type) == expected


def test_config_rejects_a_value_of_the_wrong_type():
    with pytest.raises(ValueError):
        _coerce_config_value("快点", "float")


def test_config_set_then_get_round_trips_through_the_cli(world, capsys):
    assert main(["config", "set", "scheduler.tick_rate", "0.5"]) == 0
    capsys.readouterr()
    assert main(["config", "get", "scheduler.tick_rate"]) == 0
    assert capsys.readouterr().out.strip() == "0.5"


def test_config_set_refuses_an_unknown_key(world, capsys):
    assert main(["config", "set", "llm.apikey", "x"]) == 2
    assert "config list" in capsys.readouterr().err


def test_config_get_never_prints_a_raw_secret(world, capsys):
    machine_config.set_value("llm.api_key", "sk-supersecretvalue")
    assert main(["config", "get", "llm.api_key"]) == 0
    assert "sk-supersecretvalue" not in capsys.readouterr().out


def test_config_set_routes_a_machine_key_to_machine_config(world, capsys):
    """密钥属于这台机器:`config set llm.api_key` 落进 ~/.anima-world,不进世界。"""
    assert main(["config", "set", "llm.api_key", "sk-routed"]) == 0
    found = machine_config.resolve("llm.api_key")
    assert found is not None and found[0] == "sk-routed"
    client, _ = world
    raw = client.hgetall("anima:world:config") or {}
    assert not any("sk-routed" in v for v in raw.values()), "密钥不许落进世界"


# ── doctor ───────────────────────────────────────────────────────────────────


def test_doctor_on_a_missing_world_points_at_start(world, capsys):
    assert main(["doctor", "--world-id", "nope"]) == 1
    assert "anima-world start" in capsys.readouterr().out


def test_doctor_flags_a_degraded_world_without_calling_out(world, capsys):
    assert main(["doctor", "--skip-probe"]) == 1
    out = capsys.readouterr().out
    assert "LLM 未配置" in out
    assert "与现实 1:1" in out  # the clock is reported in words, always


def test_doctor_names_the_cheap_work_running_on_the_expensive_model(world, capsys):
    """背景槽空着不是坏配置,是白花的钱 —— 而产物上一点看不出来。

    意图分类走的是背景槽,而空的背景槽退回主模型。它每轮跑一次、**串在回复前面**,
    所以玩家等的是两次生成而不是一次。她照样回话,所以这条永远不会自己暴露。
    """
    _, store = world
    store.set("chat.intent.enabled", True)
    machine_config.set_value("llm.api_key", "sk-x")
    machine_config.set_value("llm.model", "big-model")

    main(["doctor", "--skip-probe"])
    out = capsys.readouterr().out
    assert "背景槽没配" in out
    assert "意图分类" in out
    assert "llm.background.model" in out


def test_doctor_stays_quiet_when_nothing_cheap_is_running(world, capsys):
    """开关都关着就别唠叨 —— 一个没人会读的建议和没有建议一样。"""
    _, store = world
    for key in ("chat.intent.enabled", "autonomy.enabled", "chat.loop.enabled"):
        store.set(key, False)

    main(["doctor", "--skip-probe"])
    assert "背景槽没配" not in capsys.readouterr().out


# ── guided setup ─────────────────────────────────────────────────────────────


def test_empty_key_is_a_supported_answer_not_an_error(world):
    _, store = world
    configured = onboarding.configure_llm_interactively(
        store, input_fn=lambda _: "", secret_input_fn=lambda _: ""
    )
    assert configured is False
    assert onboarding.llm_status(store).state == "unset"


def test_guided_setup_stores_key_url_and_model(world, capsys):
    _, store = world
    answers = iter(["https://api.longcat.chat/openai/v1", "LongCat-2.0"])
    configured = onboarding.configure_llm_interactively(
        store, input_fn=lambda _: next(answers), secret_input_fn=lambda _: "sk-typed-in"
    )
    assert configured is True
    assert store.get("llm.api_key") == "sk-typed-in"
    assert store.get("llm.model") == "LongCat-2.0"
    assert store.get("llm.base_url") == "https://api.longcat.chat/openai/v1"
    assert "sk-typed-in" not in capsys.readouterr().out  # echoed masked only
    # 写进的是机器配置,不是世界。
    found = machine_config.resolve("llm.api_key")
    assert found is not None and found[0] == "sk-typed-in"


def test_guided_setup_accepts_defaults_on_bare_enter(world):
    _, store = world
    onboarding.configure_llm_interactively(
        store, input_fn=lambda _: "", secret_input_fn=lambda _: "sk-typed-in"
    )
    assert store.get("llm.base_url") == onboarding.DEFAULT_BASE_URL
    assert store.get("llm.model") == onboarding.DEFAULT_MODEL


def test_fix_command_names_the_world_it_applies_to(world):
    """回归:非默认名字的世界,修复提示必须带上 --world-id。

    不带的话,照抄提示的人会把密钥配置指向默认世界,自己的世界依旧降级 ——
    再跑 doctor 还是同一句提示,原地打转。
    """
    _, store = world
    fix = onboarding.llm_status(store, "mine").fix
    assert "--world-id mine" in fix


def test_fix_command_stays_bare_on_the_default_world(world):
    """反过来:世界就叫默认名字时,提示不该塞进冗余的 --world-id。"""
    _, store = world
    fix = onboarding.llm_status(store, onboarding.DEFAULT_WORLD_ID).fix
    assert "--world-id" not in fix


def test_config_accepts_world_id_trailing_like_every_other_command(world, capsys):
    """回归:`config set k v --world-id world` 必须和前置写法等价。

    这组参数曾只挂在 config 的组解析器上,尾置会撞一句顶层 usage 错(exit 2),
    连正确位置都不提示。SUPPRESS 的叶子副本就是修这个的。
    """
    assert main(["config", "set", "scheduler.tick_rate", "0.25", "--world-id", "world"]) == 0
    capsys.readouterr()
    assert main(["config", "get", "scheduler.tick_rate", "--world-id", "world"]) == 0
    assert capsys.readouterr().out.strip() == "0.25"
    assert main(["config", "list", "--world-id", "world"]) == 0
    assert "scheduler.tick_rate" in capsys.readouterr().out


def test_config_leaf_world_id_wins_over_the_group_copy(world, capsys):
    """两处都给时,以叶子(用户最后打的那个)为准 —— 不能各写各的。"""
    assert main(["config", "set", "scheduler.tick_rate", "0.75"]) == 0
    capsys.readouterr()
    assert main(["config", "--world-id", "ghost-world", "get",
                 "scheduler.tick_rate", "--world-id", "world"]) == 0
    assert capsys.readouterr().out.strip() == "0.75"  # 读到了真实世界


# ── 屏幕上的字是给人读的,不是 markdown ──────────────────────────────────────


def test_屏幕上不许出现裸markdown星号():
    """`**总账**` 在终端上就是四个星号。

    这条纪律本来只写在"忙着那句话"那一处(反引号是 markdown,玩家屏幕上就是两个
    撇号),而 `doctor --help` 自己的说明里躺着 `退出码是**总账**` —— **一条只在
    一个地方被执行的纪律,等于没有这条纪律**。所以这里按 AST 把 `print()` 的实参
    与 `help=` / `description=` / `epilog=` 整个扫一遍,加新话时它会当场拦下来。

    强调改用「」(和 `Scheduler._named` 同一个记号:它在终端、在提示词、在玩家
    屏幕上都长得一样)。

    ⚠️ **上一句话它自己违反过一次**(3.6.0 第六轮 2026-08-20 修):写下"一条只在
    一个地方被执行的纪律等于没有这条纪律"的**同一个 commit** 里,这条测试写死了
    `__main__.py` 一个文件。扫描面扩到 `anima_world/` 整棵树,当场逮出
    `tools/body.py` 两条 —— 而它们用的正是这里要扫的那个 `description=` 关键字,
    只是换了个文件。`walk` 那条尤其贵:`surfaces=(CHAT, BODY, PLAYER)`,
    `**要花时间**` 会**原样印在玩家的按钮说明上**(`World.player_tools()`)。

    ⚠️ **它看不见什么** —— 别把它读成"屏幕上再也没有星号了"。字符串先落进一个
    变量、再拼进 `print()`(或者走 `logger`、或者攒进一个 `warnings` 列表由别处印)
    的那几条路,这个扫描一条都过不去。同一轮手工查出三条这样的,已经改掉:
    `presence` 那句 `tail`、`--edit` 的图警告(`_edit_location_media_warnings`)、
    装载那条 `logger.info`。要拦住它们得做数据流分析,而**说清楚它守不住什么,
    比假装它守得住便宜** —— 上一版正是死在"读的人以为它守住了全部"。
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "anima_world"
    bad: list[tuple[str, int, str]] = []

    for src in sorted(root.rglob("*.py")):
        rel = src.relative_to(root.parent).as_posix()
        tree = ast.parse(src.read_text(encoding="utf-8"))

        def scan(node, rel=rel):
            for n in ast.walk(node):
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and "**" in n.value:
                    bad.append((rel, n.lineno, n.value[:60]))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == "print":
                for arg in node.args:
                    scan(arg)
            for kw in node.keywords:
                if kw.arg in {"help", "description", "epilog"}:
                    scan(kw.value)

    assert not bad, f"这些字会原样印到终端 / 玩家屏幕上:{bad}"
