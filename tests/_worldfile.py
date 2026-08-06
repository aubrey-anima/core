"""world.db 路径时代测试的批量改造缝。

老测试拿 `tmp_path / "w.db"` 当世界的身份:唯一(每个测试自己的 tmp_path)、
可重开(同一路径 = 同一个世界)。SQLite 退役后这两条语义由这里保住:
**一个"路径"对应一个 fakeredis 实例**,重开同一路径就是重连同一个世界。

新写的测试别用这个 —— 用 conftest 的 `open_world` 夹具。
"""
from __future__ import annotations

import fakeredis

_WORLDS: dict[str, "fakeredis.FakeStrictRedis"] = {}
_LAST: list = [None]   # 当前测试最近用到的客户端;CLI 测试经它连"同一个世界"


def redis_for(path) -> "fakeredis.FakeStrictRedis":
    """这个"世界路径"的 fakeredis;同一路径永远拿到同一个。"""
    client = _WORLDS.setdefault(
        str(path), fakeredis.FakeStrictRedis(decode_responses=True)
    )
    _LAST[0] = client
    return client


def current_client() -> "fakeredis.FakeStrictRedis":
    """CLI(main([...]))在测试里连到的 Redis:最近那个世界,没有就新开一个。"""
    if _LAST[0] is None:
        _LAST[0] = fakeredis.FakeStrictRedis(decode_responses=True)
    return _LAST[0]


def reset_current() -> None:
    _LAST[0] = None




def bundled_seed() -> dict:
    """内置演示世界的**作者层**,聚合回 section 字典的样子。

    v3 之后 `world_seed.json` 没有了 —— 内置世界是 `demo.cyberworld`。
    测试要问"橱窗里写了什么"就走这里,**只此一处知道它住在哪、是什么格式**。
    """
    from importlib import resources

    from anima_world.world_file import author_records_to_seed, read_world_file

    path = resources.files("anima_world") / "demo.cyberworld"
    _, records = read_world_file(str(path))
    return author_records_to_seed(list(records))


def write_seed_file(path, seed: dict) -> str:
    """把一个 section 字典写成世界文件。**测试夹具专用的最短路径。**

    v3 把"人写的世界描述"和"导出的 dump"合成了一种文件格式,于是种子这个概念
    没有了 —— 但 26 个测试文件是按 section 字典写夹具的,而 v3 换掉的是容器、
    不是那些字段。所以这里转一道:走的是**真的**装载路径,只是入口换成同一份
    内容的另一种写法,比逐个重写那 26 个文件诚实。

    **新写的测试直接写 `author` 记录,别走这里。**
    """
    from anima_world.world_file import (
        WorldFileManifest, seed_to_author_records, write_world_file,
    )

    write_world_file(
        path, WorldFileManifest(world_id="fixture"), seed_to_author_records(seed),
        compress=False, checksum=False,
    )
    return str(path)


def read_seed_file(path) -> dict:
    """一个世界文件的**作者层**,聚合回 section 字典 —— `write_seed_file` 的反向。

    夹具常常是"拿 bare_seed 改两条再存回去"。v3 之后那个路径上躺的是世界文件,
    不是 JSON,所以读回来也得走这条。
    """
    from anima_world.world_file import author_records_to_seed, read_world_file

    _, records = read_world_file(str(path))
    return author_records_to_seed(list(records))


def as_world_file(seed_path) -> str:
    """`seed_path=<某个 JSON>` → 一个世界文件路径。已经是世界文件就原样返回。"""
    import json
    import tempfile
    from pathlib import Path

    source = Path(seed_path)
    if source.suffix != ".json" or not source.exists():
        # 读不了的路径**原样透传** —— 有些测试就是要验"作者指名了一个坏文件,
        # 引擎当场抛"。在缝里替它抛掉,验的就成了这条缝而不是引擎。
        return str(source)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return str(source)          # 同上:坏 JSON 交给引擎去拒
    target = Path(tempfile.mkdtemp()) / "fixture.cyberworld"
    return write_seed_file(target, data)

def open_world_at(path, **kwargs):
    """`World.open(str(tmp_path / "w.db"), …)` 的一比一替身。"""
    from anima_world.api import World

    kwargs.pop("world_id", None)   # 旧 redis 测试的残参:世界名现在就是第一个参数
    legacy = kwargs.pop("seed_path", None)
    if legacy is not None:
        kwargs["world_file"] = as_world_file(legacy)
    client = kwargs.pop("redis", None) or redis_for(path)
    return World.open("w", redis=client, **kwargs)


def run_cli(*args, input: str | None = None):
    """`subprocess.run([sys.executable, "-m", "anima_world", …])` 的进程内替身。

    子进程连不到 monkeypatch 过的 fakeredis —— CLI 测试必须在进程内跑,
    这里把 main() 包成 CompletedProcess 的形状,老断言原样能用。
    """
    import contextlib
    import io
    import subprocess as _subprocess
    import sys as _sys

    from anima_world.__main__ import main

    out, err = io.StringIO(), io.StringIO()
    old_stdin = _sys.stdin
    if input is not None:
        _sys.stdin = io.StringIO(input)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(list(args))
            except SystemExit as exc:  # argparse 的 error 出口
                code = int(exc.code or 0)
    finally:
        _sys.stdin = old_stdin
    return _subprocess.CompletedProcess(
        args=list(args), returncode=int(code or 0),
        stdout=out.getvalue(), stderr=err.getvalue(),
    )
