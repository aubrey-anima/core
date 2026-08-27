"""`anima-world contract --json`:引擎自报它的线格式。

这个仓库是跨语言契约的**权威**,别人持有镜像(运维台的 `lib/worldPackage.js` /
`lib/worldSeed.js`)。今天镜像端要知道"我对齐的是哪一版"只有一个办法:读 Python 源码。
于是镜像会悄悄落后 —— 而落后的镜像不会报错,它只是对新格式给出旧答案。

这条命令是那份数据的机器可读出口:不用装世界、不用有 db,`--json` 一把梭。
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

import anima_world
from anima_world.api import _PLAYER_TTL_SECONDS
from anima_world.beats import OP_REQUIRED_FIELDS, VALID_OPS
from anima_world.tools import tools_for
from anima_world.sim_report import REPORT_FORMAT_VERSION
from anima_world.world_package import PACKAGE_FORMAT_VERSION
from anima_world.world_seed import WORLD_SEED_AGENT_KEYS, WORLD_SEED_LOCATION_KEYS


def _contract(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "anima_world", "contract", *args],
        capture_output=True, text=True,
    )


def test_contract_needs_no_world_and_no_database():
    """跑不了世界也要能回答 —— 与 `world inspect` 同一类只读命令。"""
    result = _contract("--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["engine_version"] == anima_world.__version__


def test_contract_reports_every_wire_format_version():
    payload = json.loads(_contract("--json").stdout)
    # world.db 退役(2.0 改造):`db` 段没有了,镜像端读到 `storage` 段才算对齐。
    assert "db" not in payload
    assert payload["storage"] == {
        "backend": "redis",
        "key_prefix": "anima:{world_id}:",
        "mysql_tables": ["events", "memories", "conversations", "messages"],
        "mysql_table_prefix": "{world_id}_",
        # 3.2.0:在场玩家搬进 Redis。**打包时必须跳过 `volatile_keys`** —— 镜像端
        # (运维台 `lib/worldPackage.js`)照这一格对齐,漏了的下场是导出的世界带着
        # 别人的玩家此刻在哪儿,装回去还成了一份永不过期的假在场(JSON 存不了 TTL)。
        # 3.5.0 多了 `erasure:{player_id}`:一趟没做完的法务抹除的进度,而它记着的
        # 正是**要被抹掉的那些名字**。它进包比不抹还糟,所以和 lock / 在场同一类。
        "volatile_keys": [
            "lock", "players", "player:{player_id}", "erasure:{player_id}",
        ],
        "presence": {
            "index_key": "anima:{world_id}:players",
            "row_key": "anima:{world_id}:player:{player_id}",
            "ttl_seconds": _PLAYER_TTL_SECONDS,
            "in_package": False,
        },
    }
    assert payload["package"]["format_version"] == PACKAGE_FORMAT_VERSION
    assert payload["report"]["format_version"] == REPORT_FORMAT_VERSION


def test_contract_reports_the_chat_tools_a_host_will_see_events_from():
    """她在聊天里能调的能力。宿主要显示"她走开了",得先知道哪些能力会产生它。"""
    payload = json.loads(_contract("--json").stdout)
    reported = {entry["id"] for entry in payload["chat_tools"]}
    assert reported == {spec.id for spec in tools_for("*")}
    assert {"mute", "walk_away", "wait_for_user"} <= reported


def test_contract_reports_the_seed_and_beat_shapes_a_mirror_must_match():
    """种子 schema 与节拍脚本没有版本号 —— 那就报形状,让镜像能 diff。"""
    payload = json.loads(_contract("--json").stdout)

    assert payload["seed"]["agent_keys"] == sorted(WORLD_SEED_AGENT_KEYS)
    assert payload["seed"]["location_keys"] == sorted(WORLD_SEED_LOCATION_KEYS)
    assert payload["beats"]["ops"] == sorted(VALID_OPS)
    assert payload["beats"]["op_required_fields"] == {
        op: sorted(fields) for op, fields in sorted(OP_REQUIRED_FIELDS.items())
    }


def test_the_op_table_and_its_required_fields_cannot_drift_apart():
    """VALID_OPS 里有、_OP_REQUIRED_FIELDS 里没有的 op,校验会放行任何字段。"""
    undocumented = sorted(set(VALID_OPS) - set(OP_REQUIRED_FIELDS))
    assert not undocumented, (
        f"这些 op 没有必填字段表,写错字段名不会被拒:{undocumented}"
    )


def test_contract_is_readable_by_a_human_too():
    result = _contract()
    assert result.returncode == 0, result.stderr
    assert anima_world.__version__ in result.stdout
    assert "redis" in result.stdout.lower()


# ── 配置键那一段(3.8.0)────────────────────────────────────────────────────
#
# 🔴 **这一段是「这一版引擎声明过什么」,不是「某个世界现在是什么」。**
# 后者是 `config list` 的合并视图(环境变量 → 机器配置 → 世界 → `_DEFAULTS`)。
# 混成一段就是 1.4.0 拆「创世播默认值」时治的那个病:播下去的是**创世那天的快照**,
# 引擎把 `chat.recall_k` 从 3 改成 99,已有的世界一个都吃不到,而 `config list`
# 看上去一模一样。


def test_配置段和_DEFAULTS_逐键对得上():
    """**逐键对账,不是数个数。** 只断条数的话,加一个键同时漏掉另一个仍然是绿的。"""
    from anima_world.config_store import _DEFAULTS

    payload = json.loads(_contract("--json").stdout)
    section = payload["config"]
    assert set(section) == set(_DEFAULTS), (
        f"契约里多出来的:{sorted(set(section) - set(_DEFAULTS))};"
        f"漏掉的:{sorted(set(_DEFAULTS) - set(section))}"
    )
    for key, (default, value_type, category, is_secret, description) in _DEFAULTS.items():
        row = section[key]
        assert row["value_type"] == value_type, key
        assert row["category"] == category, key
        assert row["is_secret"] == is_secret, key
        assert row["description"] == description, key
        # 密文键**一格值都不报**;别的键报引擎声明的那个默认值。
        assert row["default"] == (None if is_secret else default), key


def test_密文键只报元数据_不报值():
    """`llm.api_key` 的 `default` **永远是 `null`** —— 不是"这个世界没设",
    是**这一段根本不报值**。世界里一个 secret 都没有,所以这里也不该有一个能放它
    的位置。"""
    section = json.loads(_contract("--json").stdout)["config"]
    secrets = [k for k, v in section.items() if v["is_secret"]]
    assert secrets, "一个密文键都没有?那 `is_secret` 这一格就没有判据了"
    for key in secrets:
        assert section[key]["default"] is None, key
        # 元数据照给 —— 缺席和 null 是两件事,消费方要分得出"这支引擎有这个键"。
        assert section[key]["value_type"] and section[key]["category"]


def test_配置段不碰任何世界():
    """`contract` 整条命令都不连 Redis,所以这一段答的是引擎,不是任何一个世界。

    判据敲得动:不给 `--redis`、不给 `--world-id`,它照样答得出 66 个键。
    """
    result = _contract("--json")
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)["config"]) >= 60


def test_人看的那一份只印数与分类_不刷全表():
    """**那个数现算,别写死。** 写死一个会随每次加键而烂的数字,烂了也没有红字。"""
    from anima_world.config_store import _DEFAULTS

    out = _contract().stdout
    assert "配置键" in out and f"{len(_DEFAULTS)} 个" in out
    assert "引擎声明过什么" in out, "没把那条边界印给人看"
    # 逐键那份走 --json:人这儿不该出现某个具体键的描述文本。
    assert "Closed-session summaries recalled into the prompt" not in out


def test_可见性五档进了契约_而且和引擎读的是同一份常量():
    """🆕 2026-08-27(创作台修镜像漂移时立的诉求,🟡)。

    量声明里的 `visibility` 只认五个词,而**整份契约此前一格都没列过它们** ——
    于是创作台只能照 FOR-STUDIO 手抄一份。那正是 `kind_keys` 当初那条假红的形状:
    抄漏一个词,一份**完全合法**的声明被判红,而引擎这侧一声不吭。

    这条是**防漂移闸**,判据不是"有这一格",是**这一格和引擎真正读的那份常量
    逐位相等**:写死一份平行清单只是把漂移搬个家 —— 契约会和 `perception.py`
    分叉,而分叉那天两边都不报错(照 `plugins.rule_selectors` 的先例)。
    """
    from anima_world.perception import VISIBILITIES

    payload = json.loads(_contract("--json").stdout)
    listed = payload["seed"]["visibilities"]
    assert listed == list(VISIBILITIES), (
        f"契约报的可见档和 `perception.VISIBILITIES` 对不上:{listed} vs "
        f"{list(VISIBILITIES)} —— 这一格的全部意义就是消费方不必手抄"
    )
    # **次序是承重的**(从窄到宽),所以钉的是列表不是集合;顺带钉住"就是这五个",
    # 免得哪天悄悄多一档而消费方的选择器少一项。
    assert listed == ["self", "connected", "here", "public", "hidden"], listed
    # ⚠️ **「没声明 = 感知不到」不在这张表里** —— 它是缺席的语义,不是第六档。
    assert "none" not in listed and "" not in listed, listed
