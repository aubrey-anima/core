# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 这个仓库是什么

**anima-world —— ANIMA 世界引擎**,可发布的 pip 包。**只做引擎**:跑世界、快进、打包成
`.cyberworld`。创作已拆成独立的桌面程序 `anima-studio`(`../tool`),原因见下。

一个 `world.db` 就是一个世界。这是整套系统里**唯一的库** —— 另外两个仓库向下依赖这里定义的
格式契约,但它们不 import 本包(见下),反向零依赖。

`README.md` 是本仓库最准的文档,改架构前先读。

## 在 ANIMA 系统里的位置

ANIMA 是四个**互相独立**的仓库,跨仓库零 import,协作只走 HTTP + env 凭证 + `.cyberworld` 文件:

```
anima-site(网站)──HTTP──▶ anima-operator /directory/v1(公开目录)
              └──HTTP──▶ 世界容器 /internal/v1(bearer + HMAC claim)
anima-operator(运维台)──docker.sock──▶ 世界容器(一实例一容器)
anima-studio(创作台)──子进程──▶ 本仓库的某个版本 ──▶ .cyberworld → 运维台导入
本仓库 ──构建──▶ 世界镜像
```

本机上兄弟仓库在 `../platform`(运维台)、`../player`(网站)、`../tool`(创作工作台)。

**创作台为什么独立**:一个世界文件钉死在生成它的 core 版本上。工具要能同时持有多个引擎
版本并精确指明用哪个,就不能住在其中任何一个里面 —— 它给每个版本装一个隔离 venv,
全部交互走子进程,**永不 import 本包**。所以本仓库里不该再出现任何创作代码。

## 对外契约:本仓库是权威,别人持有镜像

其他两个仓库**不 import 本包**,而是各自持有一份冻结的镜像实现,靠互验测试对齐。
这里是权威定义 —— **改这四个模块的线格式 = 改跨仓库契约**,必须同步改镜像端:

| 权威模块 | 内容 | 谁镜像了它 |
|---|---|---|
| `anima_world/world/auth.py` | membership claim 的 HMAC 签名/验签 | 网站 `backend/app/services/claims.py` |
| `anima_world/world_package.py` | `.cyberworld` 数据包格式 | 运维台 `lib/worldPackage.js` |
| `anima_world/world_seed.py` | 种子 schema 校验 | 运维台 `lib/worldSeed.js` |
| `anima_world/beats.py` | 节拍脚本严格校验 | (无镜像;创作台经 CLI 委托校验) |

数据包与种子两项由运维台的 `test/contract.test.js` 与本包**双向互验**(引擎不可用时整体 skip)。
节拍脚本的严格校验有个硬要求:**坏脚本必须在加载时当场报错,不能流到世界启动**。

其中 **claim 线格式已永久冻结**:字段集(恰好 6 个)、序列化、签名算法都不许动,
新需求走请求体或并行新 header,永不修改本格式。`tests/test_claim_freeze.py` 用黄金向量
(固定密钥+固定 payload → 逐字节钉死的 token)机器强制这条冻结,网站仓库持有同一组向量。

## 常用命令

```bash
pip install -e ".[dev]"

python3.13 -m pytest -q               # 52 项;pyproject 的 addopts 已屏蔽 ROS 的 pytest 插件
python -m build                       # → dist/*.whl + dist/*.tar.gz
python -m twine upload dist/*         # 发布

# 给人用的三个命令(onboarding.py + __main__.py 的 run_start/run_config/run_doctor)
anima-world start                     # 引导配 LLM → 建世界 → 开浏览器;新世界用演示速度
anima-world doctor                    # 体检:密钥文件、db 格式、真调一次 LLM、时钟翻译成人话
anima-world config set llm.api_key sk-…   # 改配置不用 curl(按声明类型强转后再写)

# 给部署用的(行为不受上面影响,世界镜像跑的是 serve)
anima-world serve --host 127.0.0.1 --port 8000 --db-path saves/world.db
anima-world simulate --db-path w.db --ticks 288        # 无头快进
anima-world world export --seed seed.json --output my.cyberworld \
    --world-id my-world --name "我的世界" --mode template
anima-world world import my.cyberworld --destination ./instances

```

## 关键不变量

- **`start` 是人的门,`serve` 是部署的门**:`start` 会引导配置 LLM、给新世界换成演示速度
  (1 tick/秒);`serve` 一概不做,语义与运维台/世界镜像的契约保持不变。
  改 onboarding 时别把这两条路径搅在一起。
- **`world.db.key`(Fernet 密钥)必须随 db 搬迁** —— 丢了 `llm.api_key` 就解不开,全线降级 Mock。
  降级本身是设计(世界照跑),但**不许无声**:`ConfigStore.undecryptable_secrets()` 区分"没配过"
  与"读不出来",`build_serve_scheduler` 开机点名,`/api/state` 的 `runtime.llm.degraded_reason`
  常驻(见 `tests/test_startup_diagnostics.py`)。
- **scheduler 持有系统唯一的 RLock**(世界时钟 / 邮箱);别再引入第二把锁。
- **聊天子系统与事件核解耦**:整场会话只在关闭时发一个事件。
- 世界只收当轮有限历史(chat-evolution 回传),完整转录留在网站库里,不落世界。
- **LLM 客户端注入**,**永不在请求线程/tick 线程调用**;叙事、规划、关系判定各自跑在线程池上。
- **版本即契约(硬钉版模型)**:一个 core 版本 = (引擎代码, 内部协议版本, db format 版本,
  claim 签名格式) 一起冻结。世界镜像基于某个 core 版本构建后就只依赖该版本,不做跨版本迁移。
  - `anima_world/__init__.py` 的 `__version__` 是**唯一版本源**(pyproject 动态读取)
  - `db.py` 的 `DB_FORMAT_VERSION` / `MIN_SUPPORTED_DB_FORMAT` 是运行期安全联锁:两者相等即
    "硬不兼容",挂错卷会当场拒绝而不是静默写坏(见 `tests/test_db_format.py`)
- `world_seed.json` 是本包**唯一的 package data**,随 wheel 分发
  (`tests/test_packaging.py` 盯着,漏了会让世界镜像里少文件)。
- **本包不发任何 HTML,也不含任何创作代码**。曾经的玩家页(写死了两个虚构世界)、
  它专属的 `/api/chat` / `/api/player/move` / `--legacy-player-routes`,以及整个
  `anima_world/author/` 都已删除。玩家界面归网站(走 `/internal/v1`),创作归 `anima-studio`。
  `tests/test_packaging.py` 两条测试守着这条边界:wheel 里不许再出现 HTML,
  `import anima_world.author` 必须 ModuleNotFoundError —— 留个 shim 就等于给工具开后门。

## 当前状态

包已就绪(`dist/` 里有 0.1.0 的 wheel 与 sdist),但**尚未发布到包索引**。世界镜像目前仍由
开发库 `~/src/vibecoding/anima/deploy/Dockerfile.world` 构建,发包后应改为 `pip install anima-world`。

多仓库拆分前的合并历史归档在 `/home/super/src/vibecoding/Anima正式版-history.git`(bare)。
