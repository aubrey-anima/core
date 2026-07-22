# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 这个仓库是什么

**anima-world —— ANIMA 世界引擎 + 创作工作台**,可发布的 pip 包。一个包两件事:
**能创作**(小说/概念 → 世界种子)、**能打包**(→ 可分发的 `.cyberworld` 数据包),外加**能跑**。

一个 `world.db` 就是一个世界。这是整套系统里**唯一的库** —— 另外两个仓库向下依赖这里定义的
格式契约,但它们不 import 本包(见下),反向零依赖。

`README.md` 是本仓库最准的文档,改架构前先读。

## 在 ANIMA 系统里的位置

ANIMA 是三个**互相独立**的仓库,跨仓库零 import,协作只走 HTTP + env 凭证 + `.cyberworld` 文件:

```
anima-site(网站)──HTTP──▶ anima-operator /directory/v1(公开目录)
              └──HTTP──▶ 世界容器 /internal/v1(bearer + HMAC claim)
anima-operator(运维台)──docker.sock──▶ 世界容器(一实例一容器)
本仓库 ──构建──▶ 世界镜像;──创作台──▶ .cyberworld 数据包 → 运维台导入
```

本机上兄弟仓库在 `../platform`(运维台)与 `../player`(网站)。

## 对外契约:本仓库是权威,别人持有镜像

其他两个仓库**不 import 本包**,而是各自持有一份冻结的镜像实现,靠互验测试对齐。
这里是权威定义 —— **改这四个模块的线格式 = 改跨仓库契约**,必须同步改镜像端:

| 权威模块 | 内容 | 谁镜像了它 |
|---|---|---|
| `anima_world/world/auth.py` | membership claim 的 HMAC 签名/验签 | 网站 `backend/app/services/claims.py` |
| `anima_world/world_package.py` | `.cyberworld` 数据包格式 | 运维台 `lib/worldPackage.js` |
| `anima_world/world_seed.py` | 种子 schema 校验 | 运维台 `lib/worldSeed.js` |
| `anima_world/beats.py` | 节拍脚本严格校验 | (无镜像,只有本包内的创作台用) |

数据包与种子两项由运维台的 `test/contract.test.js` 与本包**双向互验**(引擎不可用时整体 skip)。
节拍脚本的严格校验有个硬要求:**坏脚本必须在创作台当场报错,不能流到世界启动**。

## 常用命令

```bash
pip install -e ".[dev]"

PYTHONPATH= python3.13 -m pytest -q   # 11 项;本机 PYTHONPATH 被 ROS 污染,必须清空
python -m build                       # → dist/*.whl + dist/*.tar.gz
python -m twine upload dist/*         # 发布

anima-world serve --host 127.0.0.1 --port 8000 --db-path saves/world.db
anima-world simulate --db-path w.db --ticks 288        # 无头快进
anima-world world export --seed seed.json --output my.cyberworld \
    --world-id my-world --name "我的世界" --mode template
anima-world world import my.cyberworld --destination ./instances

anima-author serve --port 8402                          # 创作工作台(= anima-world author serve)
anima-author generate --concept "雨季不停的港口小镇" --output seed.json
```

## 关键不变量

- **`world.db.key`(Fernet 密钥)必须随 db 搬迁** —— 丢了 `llm.api_key` 会静默降级 Mock。
- **scheduler 持有系统唯一的 RLock**(世界时钟 / 邮箱);别再引入第二把锁。
- **聊天子系统与事件核解耦**:整场会话只在关闭时发一个事件。
- 世界只收当轮有限历史(chat-evolution 回传),完整转录留在网站库里,不落世界。
- **LLM 客户端注入**(`llm_factory`),**永不在请求线程调用**;创作台的扫描跑在工作线程池,断点可续。
- **版本即契约(硬钉版模型)**:一个 core 版本 = (引擎代码, 内部协议版本, db format 版本,
  claim 签名格式) 一起冻结。世界镜像基于某个 core 版本构建后就只依赖该版本,不做跨版本迁移。
  - `anima_world/__init__.py` 的 `__version__` 是**唯一版本源**(pyproject 动态读取)
  - `db.py` 的 `DB_FORMAT_VERSION` / `MIN_SUPPORTED_DB_FORMAT` 是运行期安全联锁:两者相等即
    "硬不兼容",挂错卷会当场拒绝而不是静默写坏(见 `tests/test_db_format.py`)
- `world_seed.json` 与两个 `static/index.html` 是 **package data**,随 wheel 分发
  (`tests/test_packaging.py` 盯着,漏了会让世界镜像里少文件)。

## 当前状态

包已就绪(`dist/` 里有 0.1.0 的 wheel 与 sdist),但**尚未发布到包索引**。世界镜像目前仍由
开发库 `~/src/vibecoding/anima/deploy/Dockerfile.world` 构建,发包后应改为 `pip install anima-world`。

三仓库拆分前的合并历史归档在 `/home/super/src/vibecoding/Anima正式版-history.git`(bare)。
