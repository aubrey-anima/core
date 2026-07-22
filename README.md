# anima-world — 世界引擎(可发布 pip 包)

一个 `world.db` 就是一个世界。这个包**只做引擎**:跑世界、快进世界、把世界打包成可分发的
`.cyberworld`。**创作是另一个程序** —— `anima-studio`(见 `../tool`),它管理多个 core 版本,
所以不能住在其中任何一个里面。

这是整套系统里**唯一的库**:其他仓库向下依赖这里的契约模块,反向零依赖。

## 30 秒上手

```bash
pip install -e .        # ← 现在只能这样:包还没发布到 PyPI,从源码装
anima-world start
```

装完 `anima-world` 命令就在 PATH 上了。**没装之前它是找不到的命令** ——
临时想跑可以在仓库根目录用 `python -m anima_world start`,但换个目录就不灵了。

> 发布之后这里会变成 `pip install anima-world`。

`start` 依次做三件事,不用你先读任何文档:

1. **配 LLM** —— 检测到没配就当场问你要 key(直接回车 = 先用 Mock 试跑),写进 db 时自动加密,
   并**真的调用一次**确认能通
2. **建世界** —— 没有 db 就新建,新世界的时钟用**演示速度**(1 tick/秒,约 5 分钟走完一个世界日);
   想要真实时间的一行命令会打印出来
3. **运行** —— 端口被占就自动往后找

```
  ① LLM      ✓ 已加密写入 saves/world.db  ✓ 连通性测试通过
  ② 世界     ✓ 新建 saves/world.db   时钟:1 tick/秒 —— 约 5 分钟走完一个世界日
             ✓ 3 个角色就位: 苏晚夏、陆知遥、沈亦柔
  ③ 运行     http://127.0.0.1:8000   (API,没有网页界面)
```

**引擎不发 HTML。** 玩家界面是网站仓库的事(走 `/internal/v1`),管理台是运维台的事,
创作是 `anima-studio` 的事。`/` 返回一段说明自己是什么的 JSON,而不是空白 404。

出问题了先跑体检 —— 它检查密钥文件、db 格式、**真实调用一次 LLM**,并把时钟快慢翻译成人话:

```bash
anima-world doctor
anima-world config list            # 看配置(密钥自动打码)
anima-world config set llm.api_key sk-…      # 改配置,立即生效,不用 curl 也不用重启
anima-world config set scheduler.tick_rate 0.00333   # 切回真实时间
```

## 安装

```bash
pip install -e .                 # 从源码装(当前唯一可用方式)
pip install -e ".[dev]"          # 本地开发,多带 pytest/build/twine
pip install anima-world          # 发布后
```

## 命令

```bash
anima-world --help    # start / config / doctor / serve / simulate / world export|import
```

`start` 是给人用的门(引导 + 演示速度);**`serve` 是给部署用的**,行为一如既往
(不引导、不改时钟),世界镜像跑的是它。

### 想创作一个世界?

那是 `anima-studio` 的事 —— 一个独立的桌面程序,在 `../tool`:

```bash
cd ../tool && .venv/bin/anima-studio
```

它把小说变成世界种子,然后**用你选定的那个 core 版本**生成世界文件。
之所以独立成一个程序,是因为世界文件钉死在生成它的引擎版本上,
而工具要能同时持有好几个版本。

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
├── web 层     world/app.py(/api、/internal/v1、/api/admin/v1)world/auth.py —— 纯 API,不发 HTML
└── CLI        __main__.py
```

`world_seed.json` 是本包**唯一的 package data**,随 wheel 分发(见 `tests/test_packaging.py` 的回归)。

**世界引擎不发 HTML。** 它只有三组 API:`/api/*`(本地查看与改配置)、`/internal/v1/*`(网站,
服务凭证 + membership claim)、`/api/admin/v1/*`(运维台)。玩家界面是网站仓库的事;
`/` 返回一段说明自己是什么的 JSON。

## 对外契约(其他 surface 只依赖这四个)

其他 surface **不 import 本包**——它们各自持有一份冻结的镜像实现,靠互验测试对齐。
这里是权威定义:改这四个模块的线格式 = 改跨 surface 契约。

| 模块 | 权威内容 | 谁镜像了它 |
|---|---|---|
| `anima_world.world.auth` | membership claim 的 HMAC 签名/验签 | 网站后端 `app/services/claims.py` |
| `anima_world.world_package` | `.cyberworld` 数据包格式 | 运维台 `lib/worldPackage.js` |
| `anima_world.world_seed` | 种子 schema 校验 | 运维台 `lib/worldSeed.js` |
| `anima_world.beats` | 节拍脚本严格校验 | 创作工作台(`anima-studio`)通过 CLI 委托校验 |

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
