# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 这个仓库是什么

**anima-world —— ANIMA 世界引擎**,可发布的 pip 包。**只做引擎**:跑世界、快进、打包成
`.cyberworld`。创作已拆成独立的桌面程序 `anima-studio`(`../tool`),原因见下。

**引擎是纯库,没有 HTTP。** 任何要用世界的模块 import 本包,通过 `anima_world.api.World`
的函数操作 `world.db`;世界活在调用方进程里。一个 `world.db` 就是一个世界。

`README.md` 是本仓库最准的文档,`docs/REFERENCE.md` 是逐函数/逐命令的参考,改架构前先读。

## 在 ANIMA 系统里的位置

ANIMA 是多个**互相独立**的仓库。协作方式:**Python 宿主 import 本包**(pip 钉版),
非 Python 端与跨版本场景走 **CLI 子进程 + `.cyberworld` 文件**:

```
任何 Python 宿主(如网站后端)──import anima_world.api──▶ world.db(世界在宿主进程里活)
anima-operator(运维台,Node)──CLI / .cyberworld 文件──▶ 本包
anima-studio(创作台)──子进程──▶ 本包的某个版本 ──▶ .cyberworld
```

本机上兄弟仓库在 `../platform`(运维台)、`../player`(网站)、`../tool`(创作工作台)。

**创作台为什么独立**:一个世界文件钉死在生成它的 core 版本上。工具要能同时持有多个引擎
版本并精确指明用哪个,就不能住在其中任何一个里面 —— 它给每个版本装一个隔离 venv,
全部交互走子进程,**永不 import 本包**。所以本仓库里不该再出现任何创作代码。

## 对外契约:本仓库是权威,别人持有镜像

跨语言协作走文件格式,这三个模块是权威定义 —— **改它们的线格式 = 改跨仓库契约**,
必须同步改镜像端:

| 权威模块 | 内容 | 谁镜像了它 |
|---|---|---|
| `anima_world/world_package.py` | `.cyberworld` 数据包格式 | 运维台 `lib/worldPackage.js` |
| `anima_world/world_seed.py` | 种子 schema 校验 | 运维台 `lib/worldSeed.js` |
| `anima_world/beats.py` | 节拍脚本严格校验 | (无镜像;创作台经 CLI 委托校验) |

数据包与种子两项由运维台的 `test/contract.test.js` 与本包**双向互验**(引擎不可用时整体 skip)。
节拍脚本的严格校验有个硬要求:**坏脚本必须在加载时当场报错,不能流到世界启动**。
**显式指定的种子同规矩**(`WorldSeedError`):种子只读进空库一次,静默降级成内置演示世界
不可挽回;只有内置种子才降级(装坏了也得能开机)。

Python 侧的对外接口是 `anima_world/api.py` 的 `World` 门面(加上 CLI)。它是宿主应用
依赖的 API 面:**只加不改**,破坏性变更等于跨仓库破坏。

## 常用命令

```bash
pip install -e ".[dev]"

python3.13 -m pytest -q               # 109 项;pyproject 的 addopts 已屏蔽 ROS 的 pytest 插件
python -m build                       # → dist/*.whl + dist/*.tar.gz
python -m twine upload dist/*         # 发布

# 给人用的三个命令(onboarding.py + __main__.py 的 run_start/run_config/run_doctor)
anima-world start                     # 引导配 LLM → 建世界 → 前台运行;新世界用演示速度
anima-world doctor                    # 体检:密钥文件、db 格式、真调一次 LLM、时钟翻译成人话
anima-world config set llm.api_key sk-…   # 改配置不用写代码(按声明类型强转后再写)

# 给部署/脚本用的
anima-world run --db-path saves/world.db               # 前台宿主,Ctrl-C 停
anima-world simulate --db-path w.db --ticks 288        # 无头快进
anima-world world export --seed seed.json --output my.cyberworld \
    --world-id my-world --name "我的世界" --mode template
anima-world world import my.cyberworld --destination ./instances
```

嵌入到应用里(主要用法):`from anima_world.api import World` → `World.open(db_path)`。

## 关键不变量

- **`start` 是人的门,`run`/`World.open` 是程序的门**:`start` 会引导配置 LLM、给新世界
  换成演示速度(1 tick/秒);`run` 和 `World.open` 一概不做。改 onboarding 时别把这
  两条路径搅在一起。
- **一个运行中的世界独占它的 world.db**:世界的真相一半在内存(时钟/投影/锁/线程池),
  第二个进程绕过 `World` 直接写同一个 db 会立刻分叉。离线处置(打包/快进)在世界关闭
  后进行。**一个进程一个引擎版本;信任边界是进程边界**(`api.py` docstring 的三条纪律)。
- **`world.db.key`(Fernet 密钥)必须随 db 搬迁** —— 丢了 `llm.api_key` 就解不开,全线降级 Mock。
  降级本身是设计(世界照跑),但**不许无声**:`ConfigStore.undecryptable_secrets()` 区分"没配过"
  与"读不出来",`build_serve_scheduler` 开机点名,`World.state()` 的
  `runtime.llm.degraded_reason` 常驻(见 `tests/test_startup_diagnostics.py`)。
- **scheduler 持有系统唯一的 RLock**(世界时钟 / 邮箱);别再引入第二把锁。
- **聊天子系统与事件核解耦**:整场会话只在关闭时发一个事件。
- 世界只收当轮有限历史(`World.chat` 传入、`record_chat_turn` 回传),完整转录留在
  宿主应用里,不落世界。
- **LLM 客户端注入**,**永不在 tick 线程调用**;叙事、规划、关系判定各自跑在线程池上。
- **版本即契约(硬钉版模型)**:一个 core 版本 = (引擎代码, db format 版本, 包格式版本)
  一起冻结。宿主装上某个版本后就只依赖该版本,不做跨版本迁移。
  - `anima_world/__init__.py` 的 `__version__` 是**唯一版本源**(pyproject 动态读取)
  - **主版本号 = db 格式**:db 格式变才升第一位;第二位加能力,第三位纯修 bug
  - `db.py` 的 `DB_FORMAT_VERSION` / `MIN_SUPPORTED_DB_FORMAT` 是运行期安全联锁:两者相等即
    "硬不兼容",挂错卷会当场拒绝而不是静默写坏(见 `tests/test_db_format.py`)
- `world_seed.json` 是本包**唯一的 package data**,随 wheel 分发
  (`tests/test_packaging.py` 盯着,漏了会让宿主环境里少文件)。
- **本包无 HTTP、无 HTML、无创作代码**。曾经的 FastAPI web 层(`anima_world/world/`,
  三组 REST API + membership claim 鉴权)已在纯库化改造中整体移除 —— 需要网络暴露的话,
  由宿主应用自己包一层,不归引擎管。`anima_world/author/` 也已删除,
  `import anima_world.author` 必须 ModuleNotFoundError(`tests/test_packaging.py` 守着)——
  留个 shim 就等于给工具开后门。

## 当前状态

**首发版本 1.0.0(db 格式 1),尚未发布到包索引。** 主版本 = db 格式由
`tests/test_version_contract.py` 机器强制。原路线图(docs/ROADMAP.md)的
2.0–5.0 四大机制已并入首发,全部带默认关闭的开关:

- **记忆 2.0**(常开):三因子检索、反思、遗忘曲线
- **需求系统** `needs.enabled`:energy/hunger/social 曲线驱动行为树紧急带
- **经济** `economy.enabled`:物品/钱/店铺/价格漂移,账本是事件投影
- **社交** `social.enabled`:三轴关系(常开)+ 八卦传播 + 小团体

HTTP 层于 2026-07 移除:网站/运维台若要对接,走 import(Python)或 CLI + `.cyberworld`
(非 Python);旧的 `/internal/v1` 协议与 membership claim 实现在 git 历史里
(commit `e7e3188` 之前)可考古。

多仓库拆分前的合并历史归档在 `/home/super/src/vibecoding/Anima正式版-history.git`(bare)。
