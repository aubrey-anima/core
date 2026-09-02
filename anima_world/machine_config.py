"""机器级配置:LLM 的钥匙和端点住在这台机器上,不住在世界里。

## 为什么

`llm.api_key` 此前存在**世界文件**里。它是 secret,所以要加密,所以有了
`world.db.key`,所以有了那条不变量 —— "**Fernet 密钥必须随 db 搬迁**",丢了就全线
降级 Mock。整条耦合链的根,就是把一把 API key 存进了世界文件。

而它带来的还不只是麻烦:`.cyberworld` 是**分发物**。一个世界打包发出去,里面躺着
作者的 key(哪怕是密文,配上同时搬过去的 keyfile 就是明文)。种子里禁止写密文键
(`world_seed.apply_seed_config` 那条)防的正是同一件事,而世界文件这条路一直开着。

**你用哪家模型、哪把钥匙,和"这个世界是什么样"无关。** 它属于这台机器。

## 形状

兄弟仓库已经解过一次:创作台的 `~/.anima-studio/settings.json` —— **0600、明文
key、绝不入库**。这里照抄那个形状,不发明新的:

    ~/.anima-world/config.json     0600,JSON,只有这台机器上的人能读

明文而不加密是有意的:0600 的家目录文件和"加密文件 + 就放在旁边的密钥文件"防的是
同一类人(能读你磁盘的人),而后者多一层假的安全感,还多一个必须一起搬的东西。

## 解析顺序

    环境变量  →  机器配置  →  世界配置(旧世界兼容,点名警告)  →  代码里的默认值

环境变量在最前面,是为了容器和 CI —— 那种场合本来就靠环境变量注入。
但**人不该手写环境变量**:`anima-world config set llm.api_key sk-…` 和 `start` 的
引导直接写机器配置文件,那才是给人用的门。

世界配置排在倒数第二**只为向后兼容**:1.3.0 之前建的世界里真的有这一行,读得出来
就照用,同时点名说清它该搬走。默认值永远兜底 —— 什么都没配的世界照样能开机(降级
成 Mock),这是这个引擎最老的一条规矩。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 属于这台机器、不属于任何世界的键。**判据**:换一个世界,这个值要不要跟着变?
# 不要 → 它是机器的。你用哪家模型、超时多少秒、时钟跑多快,和"这个世界是什么样"无关。
MACHINE_KEYS = (
    "llm.api_key",
    "llm.base_url",
    "llm.model",
    "llm.background.model",
    "llm.timeout",
    "llm.max_retries",
    # 3.9.0(收件箱 D51):流式的两把尺。**属于这台机器**,判据和 `llm.timeout` 逐字
    # 相同 —— 换一个世界,"这条网络多久算卡住"不跟着变。
    "llm.stream.first_timeout",
    "llm.stream.gap_timeout",
)

# 环境变量别名。给容器和 CI 用 —— 人走 `config set`,不手写这些。
ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "llm.api_key": ("ANIMA_LLM_API_KEY", "OPENAI_API_KEY", "LONGCAT_API_KEY"),
    "llm.base_url": ("ANIMA_LLM_BASE_URL", "OPENAI_BASE_URL"),
    "llm.model": ("ANIMA_LLM_MODEL", "OPENAI_MODEL"),
    "llm.background.model": ("ANIMA_LLM_BACKGROUND_MODEL",),
    "llm.timeout": ("ANIMA_LLM_TIMEOUT",),
    "llm.max_retries": ("ANIMA_LLM_MAX_RETRIES",),
    "llm.stream.first_timeout": ("ANIMA_LLM_STREAM_FIRST_TIMEOUT",),
    "llm.stream.gap_timeout": ("ANIMA_LLM_STREAM_GAP_TIMEOUT",),
}

_HOME_ENV = "ANIMA_WORLD_HOME"


def home() -> Path:
    """这台机器上放引擎配置的地方。`ANIMA_WORLD_HOME` 可改(测试与多环境用)。"""
    raw = os.environ.get(_HOME_ENV)
    return Path(raw).expanduser() if raw else Path.home() / ".anima-world"


def config_path() -> Path:
    return home() / "config.json"


def is_machine_key(key: str) -> bool:
    return key in MACHINE_KEYS


def load() -> dict[str, Any]:
    path = config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # 读不出来不该让世界开不了机 —— 降级成"没配过",而这一条会点名。
        logger.warning("机器配置 %s 读不出来(%s);按没配过处理", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def save(values: dict[str, Any]) -> Path:
    """整份写回。**0600** —— 里面有明文 key。"""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先建再写:文件已经存在时 chmod 也要重设一次(有人可能手改过权限)。
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(
        json.dumps(values, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def set_value(key: str, value: Any) -> Path:
    values = load()
    values[key] = value
    return save(values)


def env_value(key: str) -> str | None:
    for name in ENV_ALIASES.get(key, ()):
        raw = os.environ.get(name)
        if raw:
            return raw
    return None


def resolve(key: str) -> tuple[Any, str] | None:
    """`(值, 它从哪来)`;机器这一层没有就返回 None,由调用方继续往下找。

    返回来源是给 `doctor` 用的:**一个值来自哪儿,和它是什么一样重要** ——
    "为什么我改了配置没生效"几乎总是这个问题。
    """
    from_env = env_value(key)
    if from_env is not None:
        return (from_env, "环境变量")
    values = load()
    if key in values and values[key] not in (None, ""):
        return (values[key], f"机器配置 {config_path()}")
    return _longcat_companion(key)


# 只给了 `LONGCAT_API_KEY` 时,端点和模型名跟着走 —— longcat 的钥匙配 OpenAI 的
# 端点,一次也用不出去。这两条此前长在**创世播种**里,于是只有新建的世界吃得到:
# 一个已经存在的世界加上 LONGCAT_API_KEY,base_url 仍是空,照跑但一次都打不通。
# 哪家的 key 本来就是机器的事,所以它归这一层。
_LONGCAT_COMPANIONS = {
    "llm.base_url": "https://api.longcat.chat/openai/v1",
    "llm.model": "LongCat-2.0",
}


def _longcat_companion(key: str) -> tuple[Any, str] | None:
    if key not in _LONGCAT_COMPANIONS:
        return None
    if not os.environ.get("LONGCAT_API_KEY"):
        return None
    if os.environ.get("ANIMA_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        return None
    return (_LONGCAT_COMPANIONS[key], "LONGCAT_API_KEY 的配套默认值")
