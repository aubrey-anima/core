"""文档里那些指着**仓库外**的相对路径,必须真的指得到东西。

🔴 **这道闸是赔出来的**(3.10.1,2026-09-02 验收 C ⑲):`docs/FOR-STUDIO.md`
里两条 `../../docs/…` 少退了一级 —— 从 `src/core/docs/` 出发,`../../` 到的是
`src/`,而总图那份 `docs/` 在 `Anima正式版/` 底下,要 `../../../`。
同一份仓库里 `docs/ARCHITECTURE.md` 写的就是 `../../../`,**两处不一致而没有
一处会红**。

**一条指不到东西的路径,和一句没人验的话是同一种东西** —— 而这一份的读者是
另一个仓库(创作台照着它去找任务单)。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
#: 只查指着仓库**外面**的那些(`../../` 打头)—— 仓库内的相对链接由别处管,
#: 而跨仓库那几条正是最容易少退一级、又最没人会去点的。
#: `../../…` 打头的,和 **`docs/…` 打头**的(3.11.0,验收 C ⑥:两条死链就长这样
#: —— 从 `src/core/docs/` 出发写 `docs/x.md`,指的是 `src/core/docs/docs/x.md`,
#: 一个不存在的地方。**它们看上去比 `../../` 还像对的**,所以更该上闸)。
_OUTWARD = re.compile(
    r"(?<![\w/.:])((?:\.\./(?:\.\./)+|docs/)[^\s)\]`\"'|]+)")


def _docs():
    yield _ROOT / "CHANGELOG.md"
    yield _ROOT / "README.md"
    for path in sorted((_ROOT / "docs").glob("*.md")):
        yield path


#: 总图那份 `docs/` 在**父目录**里,而这个仓库是可以单独克隆的
#: (CI 上就是单独克隆的)。它不在手上时这道闸**答不出来**。
_SIBLING_DOCS = _ROOT.parent.parent / "docs"


def test_指着仓库外的相对路径_都指得到东西():
    # 🔴 **答不出来就说答不出来,别红。**
    #
    # 第一版没有这一句,于是它在 `git archive` 出来的隔离树里当场红 ——
    # 而那棵树只有 `src/core` 一个仓库,父目录那份 `docs/` 根本不在。
    # **那种红指控的是被测的东西,而真相是这台机器上没有那几个兄弟仓库**
    # ——`test_autonomy` 那个 5 秒挂钟、`test_packaging` 那次联网建 sdist,
    # 这个仓库已经为同一种错栽过两次,这是第三次(当场逮住的)。
    if not _SIBLING_DOCS.is_dir():
        pytest.skip(
            f"父目录那份 docs/ 不在手上({_SIBLING_DOCS})—— 这个仓库是可以单独"
            "克隆的,而那时这道闸答不出来。**这不是「路径都对」的结论。**"
        )
    broken: list[str] = []
    for path in _docs():
        if not path.exists():
            continue
        for raw in _OUTWARD.findall(path.read_text(encoding="utf-8")):
            target = raw.rstrip(".,;:)")
            # 锚点(`file.md#section`)只查文件那一半
            target = target.split("#", 1)[0]
            if not target:
                continue
            if target.startswith(("http://", "https://")) or "…" in target:
                continue
            # `docs/…` 那一类要小心:**它有两种合法读法,还有一种不是链接**。
            #   · 从仓库根写它(`CHANGELOG.md` / `README.md` 就在根上)→ 指
            #     `src/core/docs/…`,对的;
            #   · 从 `docs/` 目录里写它 → 指 `docs/docs/…`,几乎总是错的;
            #   · **而它也可能根本不是链接**,是一句讲别的仓库的话
            #     (「诉求写进**你们自己**的 `docs/引擎接口诉求-….md`」)。
            #
            # 🔴 **一道分不出"链接"和"讲别人家的路径"的闸,会被人关掉。**
            # 所以 `docs/` 这一半只在**能证明它是错前缀**时才报:同一条路径在
            # **父仓库**里真的存在 —— 那才说明作者想指的是那一份,只是少退了两级。
            candidates = [(path.parent / target).resolve()]
            if target.startswith("docs/"):
                candidates.append((_ROOT / target).resolve())
            if any(c.exists() for c in candidates):
                continue
            if target.startswith("docs/"):
                parent_copy = (_SIBLING_DOCS.parent / target).resolve()
                if not parent_copy.exists():
                    continue      # 讲的是别的仓库的 docs/,不是这儿的死链
                broken.append(
                    f"{path.relative_to(_ROOT)}:{target} —— 少退了两级,"
                    f"它真正指的是 {parent_copy}")
                continue
            resolved = candidates[0]
            broken.append(
                f"{path.relative_to(_ROOT)}:{target} → {resolved}(不存在)")
    assert not broken, (
        "这些跨仓库相对路径指不到东西 —— 读它的是另一个仓库:\n  "
        + "\n  ".join(sorted(set(broken)))
    )


def test_每一节里的小节字母都是顺的():
    """🔴 **这是第三次被报同一件事**(3.11.2,验收 B)——(c)(d)(e) 在文件里
    乱序,读的人照目录找不到。前两次都是"就地调一下",而**一件被报三次的事,
    该有一道闸**,不该有第四次。

    ⚠️ 只查**同一节内**的字母序:小节属于哪一节由它上面最近的 `## ` 决定。
    """
    import re

    text = (_ROOT / "docs" / "FOR-STUDIO.md").read_text(encoding="utf-8")
    section = ""
    letters: list[str] = []
    bad: list[str] = []

    def check() -> None:
        if letters and letters != sorted(letters):
            bad.append(f"{section}:{letters}")

    for line in text.splitlines():
        if line.startswith("## "):
            check()
            section, letters = line[3:40], []
        elif line.startswith("### ("):
            m = re.match(r"### \(([a-z])\)", line)
            if m:
                letters.append(m.group(1))
    check()
    assert not bad, "这几节的小节字母乱序了:\n  " + "\n  ".join(bad)
