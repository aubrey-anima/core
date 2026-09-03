"""`contract.packs` 那几格必须和**真的那扇门**逐条对得上(3.10.2)。

这道闸是**赔出来的**(验收 B 对 3.10.1):`pack enable` 代码、CLI、REFERENCE
都有了,而 `contract --json` 的 `packs` 段**一个字没提** —— 而运维台那扇
「启用」按钮亮不亮,判据正是 `packs.enable_method` 在不在(**按段探测,
不比版本号**:`anima-world:3.8.0` 这个名字下已经有过两支能力不同的引擎)。

和 `test_host_doc_contract.py` 同一条理由:**一份没人验的键表,和一句没人验的
话是同一种东西** —— 而这一份的读者是另一个仓库。
"""
from __future__ import annotations

import re
from pathlib import Path

from anima_world.__main__ import contract_payload

_REFERENCE = Path(__file__).resolve().parents[1] / "docs" / "REFERENCE.md"


def _pack_subcommands() -> set[str]:
    """`anima-world pack` 真的有哪几个子命令 —— 从 argparse 那棵树上问出来,
    **不是从一份手抄的清单里读**。"""
    from anima_world.__main__ import _build_parser

    parser = _build_parser()
    for action in parser._subparsers._group_actions:      # noqa: SLF001 - 就是要问它
        pack = action.choices.get("pack")
        if pack is None:
            continue
        for sub in pack._subparsers._group_actions:       # noqa: SLF001
            return set(sub.choices)
    raise AssertionError("找不到 `pack` 的子命令表")


def test_契约里那句cli_和真的子命令表逐条相等():
    """🔴 **一句手写的 `cli` 串会烂,而且烂了不报错。**

    3.10.1 加了 `pack enable`,而这句串还停在三个子命令上 —— 照它去敲的人
    (以及照它写运维台按钮的人)不知道有第四个。
    """
    said = contract_payload()["packs"]["cli"]
    named = set(re.findall(r"anima-world pack ([a-z]+)", said))
    real = _pack_subcommands()
    assert named == real, (
        f"契约里那句 cli 提到 {sorted(named)},而真的子命令是 {sorted(real)};"
        f"少的:{sorted(real - named)},多的:{sorted(named - real)}"
    )


def test_每个写世界的子命令_契约里都有一格method():
    """`install` / `disable` / `enable` 各自要有 `method` 那一格 ——
    消费方**按段探测**,而探测不到的门等于不存在。"""
    packs = contract_payload()["packs"]
    for command, key in (("install", "method"), ("disable", "disable_method"),
                         ("enable", "enable_method")):
        assert packs.get(key), f"`pack {command}` 在契约里没有 {key} 那一格"


def _receipt_keys_from_code() -> set[str]:
    """`install_pack` 真交出来的那张回执有哪几格 —— 真装一次包问出来的。"""
    import json
    import fakeredis
    from anima_world.api import World
    from anima_world.world_file import (
        WorldFileManifest, seed_to_author_records, write_world_file,
    )
    import tempfile
    from pathlib import Path as _P

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    with tempfile.TemporaryDirectory() as tmp:
        base = _P(tmp) / "b.cyberworld"
        write_world_file(
            base, WorldFileManifest(world_id="w", engine_min="3.10.0"),
            seed_to_author_records({
                "agents": [{"id": "甲", "name": "甲", "location": "cafe",
                            "personality": "温和"}],
                "locations": [{"id": "cafe", "name": "咖啡店", "kind": "point",
                               "x": 0, "y": 0, "description": "海边"}],
            }), compress=False, checksum=False)
        World.open("w", redis=client, world_file=str(base),
                   force_mock_llm=True).close()
        pack = _P(tmp) / "p.cyberworld"
        write_world_file(
            pack, WorldFileManifest(world_id="w", engine_min="3.10.0"),
            seed_to_author_records({"pack": {"id": "周", "version": "1.0.0"},
                                    "world_setting": "换一段"}),
            compress=False, checksum=False)
        with World.open("w", redis=client, force_mock_llm=True) as world:
            return set(world.install_pack(str(pack)))


def test_文档里那张回执键表_和真回执逐格一致():
    """🔴 **同一张回执,三处三个数**(验收 B 对 3.10.1 量的):
    代码 14 格 · REFERENCE §3 那一行 12 格 · §4.11 那一段 10 格 ·
    而 `api.py` 的 docstring 还写着 10 格。**下游抄的就是这几处。**

    这道闸只钉一件事:**REFERENCE 里那张键表 == 真回执的键**。
    """
    real = _receipt_keys_from_code()
    text = _REFERENCE.read_text(encoding="utf-8")
    marker = "回执键表(`install_pack`):"
    assert marker in text, (
        f"REFERENCE 里找不到那张唯一的回执键表(应以「{marker}」打头)")
    # ⚠️ **读到空行为止,不是读一行** —— 十四个键换行排版是常态,
    # 而只读一行的闸会把「换了行」当成「少了键」(第一版就是这样,当场红)。
    # ⚠️ **只读那一串键本身,读到破折号为止** —— 后面那段散文里会提到
    # 「只在真装进去了什么的时候才多出来」的那几个段名,把它们算进表里
    # 就是让闸去咬一句解释(第一版就是这样,当场红)。
    tail = text.split(marker, 1)[1]
    block = tail.split("\n——", 1)[0].split("\n\n", 1)[0]
    named = set(re.findall(r"`([a-z_]+)`", block))
    assert named == real, (
        f"REFERENCE 那张回执键表:{sorted(named)}\n真回执:{sorted(real)}\n"
        f"少的:{sorted(real - named)},多的:{sorted(named - real)}"
    )
    # 🔴 **连那个数字词一起比**(3.11.0,验收 B 对 3.10.2:上一版只比键名,
    # 于是键补齐了而旁边那句「十四格」还留着 —— **自称权威的那一行反而是旧的**)。
    digits = {10: "十", 12: "十二", 14: "十四", 16: "十六", 17: "十七", 18: "十八"}
    right = f"{digits[len(real)]}格"
    # ⚠️ **只在那张表自己那一段里比,不扫整份文档**(第一版扫全文,当场红在
    # §4.9 抹除那张**另一份**回执的「十四格」上 —— **闸咬到了一句跟它无关的话**。
    # 「一条规矩的例子和它禁止的那件事是同一段字节」这个坑,这是第三次了,
    # 形状每次都一样:**闸的作用域比它要钉的那件事大**。)
    scope = tail.split("\n\n\n", 1)[0][:1200]
    # 🔴 **同一道闸扫两处**(3.11.0,验收 A):`api.py` 的 docstring 里也有一份
    # 同样的十七格表,而它上一版**没上闸** —— REFERENCE 那张有闸、它没有,
    # 于是"哪一份先烂"完全看运气。**一道只覆盖我记得去扫的地方的闸**,
    # 这个仓库记过两次了。
    from anima_world.api import World

    doc = World.install_pack.__doc__ or ""
    marker_doc = "返回一张回执"
    assert marker_doc in doc, "`install_pack` 的 docstring 里没有那张回执表"
    doc_scope = doc.split(marker_doc, 1)[1].split("\n\n", 2)
    doc_named = set(re.findall(r"[a-z_]{3,}", "\n".join(doc_scope[:2])))
    missing_in_doc = real - doc_named
    assert not missing_in_doc, (
        f"`api.py` 的 docstring 那张回执表少了 {sorted(missing_in_doc)} —— "
        f"REFERENCE 那张有闸而它没有,「哪一份先烂」就成了运气")
    assert right in scope, (
        f"那张回执键表旁边找不到「{right}」—— 真回执是 {len(real)} 格。"
        f"⚠️ 自称权威的那一行反而是旧的,正是验收 B 逮到的那一种")
    for n, word in digits.items():
        if n == len(real):
            continue
        assert f"{word}格" not in scope, (
            f"那张回执键表旁边还写着「{word}格」,而真回执是 {len(real)} 格")


def test_install_pack的docstring_不许再说那三件不做():
    """`api.py` 的 docstring 是**权威**,而它还写着「2a-① 明确不做:改在册的人的
    人设、给在册的人补记忆、停用一个包」—— 同一棵树上三件都做了。
    **权威与镜像分叉时,先烂的是照它抄的那一个**(创作台的错话很可能照它抄的)。
    """
    from anima_world.api import World

    said = World.install_pack.__doc__ or ""
    # ⚠️ **正面断言,不是"别出现这几个字"**(第一版就是后者,而它当场红了 ——
    # 红在**我解释这个 bug 的那句话**上:那段说明里原样引了那句过时的话,
    # 而闸看不出「在陈述」和「在引用」的差别。
    # **一条规矩的例子,和它禁止的那件事,是同一段字节** —— 这个仓库记过一次,
    # 这是第二次。)
    for door in ("enable_pack", "disable_pack", "compare-and-set"):
        assert door in said, (
            f"`install_pack` 的 docstring 里没提 {door!r} —— 那三件都有出口了,"
            "而这段 docstring 是权威;权威与镜像分叉时,先烂的是照它抄的那一个")


#: 那几件**做完了、而文档里可能还写着"没做"**的事。一格一个短语,
#: 命中即红 —— 措辞换了不要紧,这张表钉的是**那句话的形状**。
_DONE_BUT_MAYBE_STALE = (
    "仍然是排期,不是现状",
    "这一轮明确没做的",
    "还没有出口(周更批 2a-② 会开)",
    "❌ **还没做**(2a-②)",
    "仍不做:停用一个包",
)


def test_两份文档里不许再写那几件已经做完的事还没做():
    """🔴 **一句过期的排期,比没有排期更坏** —— 照它写的人会去绕一条根本不必
    绕的路(3.10.2,验收 C ③)。

    2a-② 与 3.10.1 交付之后,「改在册的人的人设 / 给在册的人补记忆 / 停用一个包
    / 再启用回来 / 主持人的『回来』时刻」五件都有出口,而两份文档里有四处还写着
    「没做」。**排期不是现状**,这个仓库为这句话红过一次(`World.` 那道闸)。

    ⚠️ 这道闸**只钉那几个具体短语**,不扫「没有排期」那一类 —— 那些说的是
    真的没做的东西(`set_rules`、规律层扇入、`hidden` 的八卦链),**它们该留着**。
    """
    docs = {
        "REFERENCE.md": _REFERENCE,
        "FOR-STUDIO.md": _REFERENCE.parent / "FOR-STUDIO.md",
    }
    hits: list[str] = []
    for name, path in docs.items():
        text = path.read_text(encoding="utf-8")
        for phrase in _DONE_BUT_MAYBE_STALE:
            if phrase in text:
                hits.append(f"{name}:「{phrase}」")
    assert not hits, (
        "这几处还写着那几件已经做完的事没做 —— 而创作台照它抄:\n  "
        + "\n  ".join(hits)
    )


def test_回执里报了落地的段_都进得了sections():
    """🔴 **`declared - sections` 是消费方判「有东西没装上」的那把尺**
    (FOR-STUDIO §3.62(m) 就是这么教的),所以**回执说装了什么,`sections`
    就必须有那一格** —— 少一格就是指着一样装得好好的东西说它没装进去。

    这个坑踩过两次,**形状一模一样、只是少的格子不同**:
    A② 少了 kinds/entities/plugins/rules/items;C① 少了 personality/memories。
    病根都是**那张表是手抄的**。这道闸让"又漏了一格"有人喊。
    """
    import tempfile
    from pathlib import Path as _P
    import fakeredis
    from anima_world.api import World
    from anima_world.world_file import (
        WorldFileManifest, seed_to_author_records, write_world_file,
    )

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    with tempfile.TemporaryDirectory() as tmp:
        base = _P(tmp) / "b.cyberworld"
        write_world_file(
            base, WorldFileManifest(world_id="w", engine_min="3.10.0"),
            seed_to_author_records({
                "agents": [{"id": "夏", "name": "夏", "location": "cafe",
                            "personality": "温和"}],
                "locations": [{"id": "cafe", "name": "咖啡店", "kind": "point",
                               "x": 0, "y": 0, "description": "海边"}],
            }), compress=False, checksum=False)
        World.open("w", redis=client, world_file=str(base),
                   force_mock_llm=True).close()
        # 一份**只改人设 + 补记忆**的包 —— C① 就是在它身上量到 `sections: {}` 的
        pack = _P(tmp) / "p.cyberworld"
        write_world_file(
            pack, WorldFileManifest(world_id="w", engine_min="3.10.0"),
            seed_to_author_records({
                "pack": {"id": "第二周", "version": "1.0.0"},
                "agents": [{"id": "夏", "name": "夏", "location": "cafe",
                            "personality": "这一周她话多了些"}],
                "memories": [{"agent_id": "夏", "kind": "seed",
                              "summary": "第二周开头下了场雨"}],
            }), compress=False, checksum=False)
        with World.open("w", redis=client, force_mock_llm=True) as world:
            # `--force`:这一句是创世写的、不是这份包上一版写的,CAS 照规矩拒 ——
            # 而这条用例要测的是**装进去之后回执与 sections 对不对得上**。
            receipt = world.install_pack(str(pack), force=True)
            assert receipt["forced_personality"] == ["夏"], receipt
            sections = world.packs()[0]["sections"]
            assert receipt["personality"] == ["夏"], receipt
            assert receipt["memories"] == 1, receipt
            assert sections.get("personality") == ["夏"], sections
            assert sections.get("memories"), sections
            # 而 `declared - sections` 不该再指着它们说"没装进去"
            declared = world.packs()[0].get("declared") or {}
            ghost = set(declared.get("agents") or ()) - set(
                (sections.get("agents") or []) + (sections.get("personality") or []))
            assert not ghost, f"这几样装进去了却被报成没装:{ghost}"
