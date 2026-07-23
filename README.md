# anima-world — 世界引擎(纯库,可发布 pip 包)

一个 `world.db` 就是一个世界。这个包**只做引擎**:跑世界、快进世界、把世界打包成可分发的
`.cyberworld`。**创作是另一个程序** —— `anima-studio`(见 `../tool`),它管理多个 core 版本,
所以不能住在其中任何一个里面。

**引擎是一个库,不是一个服务。** 没有 HTTP、没有端口:任何要用世界的模块 import 本包,
通过 `anima_world.api.World` 的函数操作 `world.db`。世界活在调用方进程里。

## 30 秒上手

```bash
pip install -e .        # ← 现在只能这样:包还没发布到 PyPI,从源码装
anima-world start
```

`start` 依次做三件事,不用你先读任何文档:

1. **配 LLM** —— 检测到没配就当场问你要 key(直接回车 = 先用 Mock 试跑),写进 db 时自动加密,
   并**真的调用一次**确认能通
2. **建世界** —— 没有 db 就新建,新世界的时钟用**演示速度**(1 tick/秒,约 5 分钟走完一个世界日);
   想要真实时间的一行命令会打印出来
3. **运行** —— 世界在本进程里活起来,叙事逐行打印,Ctrl-C 停止

出问题了先跑体检:

```bash
anima-world doctor
anima-world config list            # 看配置(密钥自动打码)
anima-world config set llm.api_key sk-…      # 改配置,立即生效
anima-world config set scheduler.tick_rate 0.00333   # 切回真实时间
```

## 在程序里用(这是主要接口)

```python
from anima_world.api import World

with World.open("saves/world.db") as world:
    world.start_clock()                      # 后台走时钟;或手动 world.tick(n)
    print(world.state()["world_time"])       # 完整世界快照

    # 代玩家和角色聊天(流式;完整转录归宿主应用管,世界不落)
    for chunk in world.chat("夏", [{"role": "user", "content": "你好"}],
                            player_id="p1", display_name="阿宇"):
        print(chunk, end="")

    # 把一个完成的回合记入世界:摘要 + 一个事件 + 关系判定
    world.record_chat_turn("夏", "p1", [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好呀"},
    ])

    world.player_move("p1", "cafe")          # 玩家移动/动作
    world.config_set("scheduler.tick_rate", 1.0)   # 热改配置
# 离开 with:停时钟、排干 LLM 线程池(事件每 tick 已落盘,退出时不额外写)
```

三条使用纪律(`anima_world/api.py` 的 docstring 是权威版本):

1. **一个运行中的世界独占它的 world.db** —— 别的进程不要碰同一个文件;
2. **一个进程一个引擎版本** —— 世界钉死在生成它的版本上,多版本按进程隔离
   (anima-studio 的隔离 venv + 子进程就是这个模式);
3. **信任边界是进程边界** —— `player_id` 只是参数,验证调用者是宿主应用的责任。

## 命令

```bash
anima-world --help    # start / config / doctor / run / simulate / world export|import
```

> 每个命令的完整参数、World 的逐函数说明、配置键 / 环境变量 / 节拍脚本格式,
> 见 **[docs/REFERENCE.md](docs/REFERENCE.md)**(功能与接口参考)。
> 想先理解**为什么是这样**——真相模型、tick 帧、线程与锁、不变量、已知架构债,
> 见 **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**(架构)。

`start` 是给人用的门(引导 + 演示速度);`run` 是无引导的前台宿主(给部署 / 脚本);
真正嵌入到应用里用 `anima_world.api.World`。

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
anima-world run --db-path saves/world.db             # 前台宿主,Ctrl-C 停
anima-world simulate --db-path w.db --ticks 288      # 无头快进
```

## 包结构

```
anima_world/
├── 库门面     api.py(World:开世界/时钟/状态/聊天/玩家/配置 —— 对外的主接口)
├── 事件核     events.py(append-only 日志)projection.py types.py db.py
├── 决策       agent.py bt_nodes.py(行为树)brain.py actions.py world_time.py
├── 编排       scheduler.py(世界时钟/邮箱;系统唯一的 RLock)
├── 叙事/LLM   narrative.py llm_client.py planner.py relationship_judge.py
├── 聊天       chat_service.py chat_session.py chat_store.py(与事件核解耦)
├── 记忆/图谱  memory_store.py memory_triggers.py graph.py
├── 配置       config_store.py(Fernet 加密 secret,keyfile=<db>.key)prompt_store.py
├── 世界定义   world_store.py world_seed.py world_seed.json locations.py beats.py
├── 打包       world_package.py(.cyberworld 导入导出)
└── CLI        __main__.py onboarding.py
```

`world_seed.json` 是本包**唯一的 package data**,随 wheel 分发(见 `tests/test_packaging.py` 的回归)。

## 对外契约(其他 surface 只依赖这三个文件格式)

跨仓库协作走**文件与 import**,不走网络。文件格式的权威定义在这里,
对端(如运维台的 JS 实现)持有冻结镜像,靠互验测试对齐:

| 模块 | 权威内容 | 谁镜像了它 |
|---|---|---|
| `anima_world.world_package` | `.cyberworld` 数据包格式 | 运维台 `lib/worldPackage.js` |
| `anima_world.world_seed` | 种子 schema 校验 | 运维台 `lib/worldSeed.js` |
| `anima_world.beats` | 节拍脚本严格校验 | 创作工作台(`anima-studio`)通过 CLI 委托校验 |

Python 侧的对外接口是 `anima_world.api`(函数门面)与 CLI;宿主应用直接 import,
版本以 pip 钉死(一个进程一个版本)。

## 数据(一个世界=一个卷)

`world.db`(事件/聊天/记忆/配置)+ `world.db.key`(**搬迁必须随行**,丢了 `llm.api_key` 静默降级 Mock)
+ `world_seed.json` + `beats.json`。空卷首启自播种。

## 版本即契约(硬钉版模型)

一个 core 版本 = **(引擎代码, db format 版本, 包格式版本)** 一起冻结。
宿主基于某个 core 版本装上后就只依赖该版本,不做跨版本迁移:

- `anima_world/__init__.py` 的 `__version__` 是**唯一版本源**(pyproject 动态读取)
- **主版本号 = db 格式**:db 格式变才升第一位,第二/三位都是程序优化
- `db.py` 的 `DB_FORMAT_VERSION` / `MIN_SUPPORTED_DB_FORMAT` 是运行期安全联锁:
  挂错卷会当场拒绝而不是静默写坏

## 测试与发布

```bash
pip install -e ".[dev]"
python -m pytest                 # tests/(db 格式闸门 + 打包契约 + api 门面)
python -m build                  # → dist/*.whl + dist/*.tar.gz
python -m twine upload dist/*    # 发布
```

## 许可证

[Apache License 2.0](LICENSE) —— 随便用、改、闭源商用,保留版权声明并标注改动过
哪些文件即可。相比 MIT 多一条**专利授权与报复条款**:贡献者授予你专利许可,而谁
拿这份代码去打专利官司,他自己的授权当场终止。

引擎是给宿主 `import` 的库,所以许可证必须宽松到能嵌进闭源应用 —— copyleft
(GPL/AGPL)会传染到每一个宿主,那和这个包存在的理由是矛盾的。
