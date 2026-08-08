"""goals 被按**字**拆开 —— 一份形状完全合法的坏数据。

线上那个世界(897e282865f5)九个角色全中:`["摆","脱","母","亲",…]`。它是
`list[str]`,任何 schema 校验都挑不出毛病,于是它一路进了 Redis、进了投影、
进了 planner 每天排一天日子的提示词。**照跑、日志干净、作者三个月后才发现** ——
这个仓库最怕的那一类。

源头在创作台(`anima_studio/pipeline/concept.py`):模型被要求给一个数组,却回了
一整个字符串 `"摆脱母亲的控制；重新定义自己的人生"`,对它做列表推导就按字符拆开。
那一头已经修了(`_short_lines`),这里钉的是**引擎这一头**:
收进来要认得出、已经写下的要修得回来。
"""
from __future__ import annotations

from _worldfile import open_world_at

from anima_world.beats import coerce_goals

# 线上那九条里的一条,一个字不改
BROKEN = ["摆", "脱", "母", "亲", "的", "控", "制", "；",
          "重", "新", "定", "义", "自", "己", "的", "人", "生"]
FIXED = ["摆脱母亲的控制", "重新定义自己的人生"]


def test_a_char_split_goals_list_is_put_back_together():
    """拼接是**无损**的,所以这不是猜一个答案,是把丢掉的那一步倒回去。"""
    assert coerce_goals(BROKEN) == FIXED


def test_the_repair_is_idempotent():
    assert coerce_goals(coerce_goals(BROKEN)) == FIXED


def test_a_bare_string_is_split_on_the_separators_not_on_characters():
    assert coerce_goals("摆脱母亲的控制；重新定义自己的人生") == FIXED
    assert coerce_goals("守住店") == ["守住店"]


def test_a_separator_inside_one_element_is_split_too():
    """模型常把两个目标写进同一个数组元素 —— 不拆的话分隔符原样进提示词。"""
    assert coerce_goals(["把店开好；撑过雨季"]) == ["把店开好", "撑过雨季"]


def test_good_goals_are_left_exactly_alone():
    """判据要窄到误判需要一份本来就坏的数据 —— 正常的目标一个字都不许动。"""
    for good in (FIXED, ["守住店"], ["活下去", "找到她"], []):
        assert coerce_goals(good) == good


def test_a_short_list_of_short_goals_is_not_mistaken_for_char_split():
    """三条单字的不算(判据要 ≥4 条且**每条**都只有一个字)。"""
    assert coerce_goals(["赢", "输", "逃"]) == ["赢", "输", "逃"]


def test_non_string_elements_survive():
    """丢掉认不出的元素是另一种静默的少装。"""
    assert coerce_goals([{"kind": "x"}, "守住店"]) == [{"kind": "x"}, "守住店"]


# ── 已经写下的那些:修数据的路子 ─────────────────────────────────────────────


def _break_goals(world, agent_id: str) -> None:
    world.scheduler.agents[agent_id].agent.blackboard.write("goals", list(BROKEN))


def test_repair_agent_goals_dry_run_changes_nothing(tmp_path):
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        _break_goals(world, "夏")
        result = world.repair_agent_goals(dry_run=True)

        assert result["repaired"] == 1
        row = result["rows"][0]
        assert row["agent_id"] == "夏"
        assert row["before"] == BROKEN and row["after"] == FIXED
        assert world.scheduler.agents["夏"].agent.blackboard.read("goals") == BROKEN, (
            "--dry-run 动了库"
        )


def test_repair_agent_goals_fixes_the_blackboard_and_the_log(tmp_path):
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        _break_goals(world, "夏")
        result = world.repair_agent_goals()

        assert result["repaired"] == 1
        assert world.scheduler.agents["夏"].agent.blackboard.read("goals") == FIXED
        # 只修黑板不落日志的话,那份坏 spec 还躺在世界里,下一个读它的人又拿到单字。
        updates = [
            e for e in world.events()
            if (e.get("payload") or {}).get("kind") == "persona_update"
            and e.get("who") == "夏"
        ]
        assert updates, "修了黑板却没进事件日志"
        assert updates[-1]["payload"]["spec"]["goals"] == FIXED


def test_repairing_a_healthy_world_is_a_no_op(tmp_path):
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        before = len(world.events())
        result = world.repair_agent_goals()
        assert result["repaired"] == 0 and result["rows"] == []
        assert len(world.events()) == before, "没病也写了一笔"


def test_the_repair_is_idempotent_against_a_real_world(tmp_path):
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        _break_goals(world, "夏")
        world.repair_agent_goals()
        assert world.repair_agent_goals()["repaired"] == 0


def test_a_reboot_alone_is_not_enough_when_the_seed_carries_goals(tmp_path):
    """**别指望重启把它修好** —— 这条测试存在是为了挡住一个很自然的结论。

    开机时 `goals` 确实会过一遍 `coerce_goals`(所以那份坏 spec 折出来是好的),但
    紧接着,手里那份世界文件里写着 goals 的角色会被**种子那份**盖回去。于是"修了
    引擎、重启一次就好"对**有种子的世界**不成立,而发版恰恰就是重启。

    结论写进发布清单:`agent repair-goals` 是必跑的一步,不是可选的补救。
    """
    path = str(tmp_path / "w.db")
    world = open_world_at(path, force_mock_llm=True)
    with world:
        world._record_and_fan({
            "type": "state_change", "who": "夏",
            "payload": {"kind": "persona_update", "spec": {"goals": list(BROKEN)}},
        })
        world.tick(1)
        world.close()

    reopened = open_world_at(path, force_mock_llm=True)
    with reopened:
        after = reopened.scheduler.agents["夏"].agent.blackboard.read("goals")
        assert after != BROKEN, "连开机那道 coerce 都没走"
        # 修好了,但修成的是**种子那份**,不是日志里最后那次 persona_update。
        assert all(len(g) > 1 for g in after), "还是单字"
        # 而 `repair-goals` 对着这个世界是干净的 —— 它已经没有单字目标了。
        assert reopened.repair_agent_goals()["repaired"] == 0
