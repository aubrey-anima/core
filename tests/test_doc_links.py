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
_OUTWARD = re.compile(r"(?<![\w/.])(\.\./(?:\.\./)+[^\s)`\"'|]+)")


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
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                broken.append(
                    f"{path.relative_to(_ROOT)}:{target} → {resolved}(不存在)")
    assert not broken, (
        "这些跨仓库相对路径指不到东西 —— 读它的是另一个仓库:\n  "
        + "\n  ".join(sorted(set(broken)))
    )
