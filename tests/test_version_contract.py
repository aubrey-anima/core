"""版本规则的机器强制:主版本号 = db 格式。

规则(仓库所有者定):db 格式变才升第一位版本号,第二/三位都是程序优化。
这条测试让"改了 DB_FORMAT_VERSION 忘了升主版本"(或反过来)当场红,
规则从纪律变成联锁 —— 编辑器只看主版本第一位就能判断两个版本的世界文件
互不互通。
"""
from __future__ import annotations

from anima_world import __version__
from anima_world.db import DB_FORMAT_VERSION, MIN_SUPPORTED_DB_FORMAT


def test_major_version_equals_db_format():
    major = int(__version__.split(".")[0])
    assert major == DB_FORMAT_VERSION, (
        f"主版本 {major} 必须等于 DB_FORMAT_VERSION {DB_FORMAT_VERSION} —— "
        "db 格式变才升第一位,第二/三位是程序优化"
    )


def test_hard_pin_window_is_closed():
    """硬钉版模型:MIN_SUPPORTED == DB_FORMAT_VERSION,不做跨格式兼容窗口。"""
    assert MIN_SUPPORTED_DB_FORMAT == DB_FORMAT_VERSION
