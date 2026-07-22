# anima-world — 世界引擎 + 创作工作台(可发布 pip 包)

一个 pip 包,两件事:**能创作**(小说/概念 → 世界种子)、**能打包**(→ 可分发的 `.cyberworld` 数据包)。
一个 `world.db` 就是一个世界。这是整套系统里**唯一的库**:player/platform 向下依赖这里的契约模块,反向零依赖。

## 安装

```bash
pip install anima-world          # 发布后
pip install -e ".[dev]"          # 本地开发(含 pytest/build/twine)
```

## 两个命令行入口

```bash
anima-world  --help              # 引擎 + 打包:serve / story / simulate / world export|import / author
anima-author --help              # 创作工作台,等价于 anima-world author
```

### 能创作

```bash
# 一句话概念 → 世界种子
anima-author generate --concept "雨季不停的港口小镇" --output seed.json --agents 4 --locations 5

# 打开工作台:上传小说 → LLM 扫描 → 蒸馏 → 实体卡片 → 物化数据包
anima-author serve --port 8402 --db saves/author.db
```

### 能打包

```bash
# 模板包(只有种子,世界首启自建 db)
anima-world world export --seed seed.json --output my.cyberworld \
    --world-id my-world --name "我的世界" --mode template

# 快照包(带一个跑过的 world.db)
anima-world world export --seed seed.json --db-path saves/world.db \
    --beats beats.json --output my.cyberworld \
    --world-id my-world --name "我的世界" --mode snapshot

anima-world world import my.cyberworld --destination ./instances
```

### 能跑

```bash
anima-world serve --host 127.0.0.1 --port 8000 --db-path saves/world.db
anima-world simulate --db-path w.db --ticks 288      # 无头快进
```

## 包结构

```
anima_world/
├── 事件核     events.py(append-only 日志)projection.py types.py db.py snapshot.py
├── 决策       agent.py bt_nodes.py(行为树)brain.py actions.py world_time.py
├── 编排       scheduler.py(世界时钟/邮箱;系统唯一的 RLock)
├── 叙事/LLM   narrative.py llm_client.py planner.py relationship_judge.py
├── 聊天       chat_service.py chat_session.py chat_store.py(与事件核解耦)
├── 记忆/图谱  memory_store.py memory_triggers.py graph.py
├── 配置       config_store.py(Fernet 加密 secret,keyfile=<db>.key)prompt_store.py
├── 世界定义   world_store.py world_seed.py world_seed.json locations.py beats.py
├── 打包       world_package.py(.cyberworld 导入导出)
├── web 层     world/app.py(/api、/internal/v1、/api/admin/v1)world/auth.py world/static/
├── 创作台     author/(app.py extract.py distill.py generate.py store.py static/)
└── CLI        __main__.py
```

`world_seed.json` 与两个 `static/index.html` 是 **package data**,随 wheel 分发(见 `tests/test_packaging.py` 的回归)。

## 对外契约(其他 surface 只依赖这四个)

其他 surface **不 import 本包**——它们各自持有一份冻结的镜像实现,靠互验测试对齐。
这里是权威定义:改这四个模块的线格式 = 改跨 surface 契约。

| 模块 | 权威内容 | 谁镜像了它 |
|---|---|---|
| `anima_world.world.auth` | membership claim 的 HMAC 签名/验签 | 网站后端 `app/services/claims.py` |
| `anima_world.world_package` | `.cyberworld` 数据包格式 | 运维台 `lib/worldPackage.js` |
| `anima_world.world_seed` | 种子 schema 校验 | 运维台 `lib/worldSeed.js` |
| `anima_world.beats` | 节拍脚本严格校验(坏脚本必须在创作台报错) | (无镜像,只有本包内的创作台用) |

数据包与种子两项由 `src/platform/test/contract.test.js` 与本包**双向互验**。

## 数据(一个世界=一个卷)

`world.db`(事件/快照/聊天/记忆/配置)+ `world.db.key`(**搬迁必须随行**,丢了 `llm.api_key` 静默降级 Mock)
+ `world_seed.json` + `beats.json`。空卷首启自播种。

## 版本即契约(硬钉版模型)

一个 core 版本 = **(引擎代码, 内部协议版本, db format 版本, claim 签名格式)** 一起冻结。
世界镜像基于某个 core 版本构建后就只依赖该版本,不做跨版本迁移:

- `anima_world/__init__.py` 的 `__version__` 是**唯一版本源**(pyproject 动态读取)
- `db.py` 的 `DB_FORMAT_VERSION` / `MIN_SUPPORTED_DB_FORMAT` 是运行期安全联锁:
  两者相等即"硬不兼容",挂错卷会当场拒绝而不是静默写坏

## 测试与发布

```bash
pip install -e ".[dev]"
python -m pytest                 # tests/(db 格式闸门 + 打包契约)
python -m build                  # → dist/*.whl + dist/*.tar.gz
python -m twine upload dist/*    # 发布
```
