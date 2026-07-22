"""Console-script entry point for the author studio.

``anima-author <args>`` is exactly ``anima-world author <args>`` — the creator
workflow gets its own front door without forking the argument parser.
"""

from __future__ import annotations

import sys

from anima_world.__main__ import main as _world_main


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    return _world_main(["author", *args])


if __name__ == "__main__":
    sys.exit(main())
