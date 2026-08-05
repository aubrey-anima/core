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


def open_world_at(path, **kwargs):
    """`World.open(str(tmp_path / "w.db"), …)` 的一比一替身。"""
    from anima_world.api import World

    kwargs.pop("world_id", None)   # 旧 redis 测试的残参:世界名现在就是第一个参数
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
