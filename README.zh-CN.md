# anima-world — 世界引擎(纯库,可发布 pip 包)

**给 LLM 角色住的世界模拟引擎。** 角色会醒来、会饿、会去上班、会背后议论别人、会抱团、
会花钱、会记得上周发生的事、会改变对你的看法。一个 `world.db` 就是一个世界。

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](pyproject.toml)
[![PyPI](https://img.shields.io/pypi/v/anima-world.svg)](https://pypi.org/project/anima-world/)

[English](README.md) | 中文

---

这个包**只做引擎**:跑世界、快进世界、把世界打包成可分发的 `.cyberworld`。

**引擎是一个库,不是一个服务。** 没有 HTTP、没有端口:任何要用世界的模块 import 本包,
通过 `anima_world.api.World` 的函数操作 `world.db`。世界活在调用方进程里。

LLM 在模拟**旁边**,不在里面。它负责叙事、给角色安排空闲时间、判定一场对话如何改变了
关系 —— 全都跑在后台线程上。**时钟永远不等网络。** 把 LLM 整个拿掉世界照样跑,只是文本
变成模板。

## 30 秒上手

```bash
pip install anima-world
anima-world start
```

`start` 依次做三件事,不用你先读任何文档:

1. **配 LLM** —— 检测到没配就当场问你要 key(直接回车 = 先用 Mock 试跑),写进 db 时自动加密,
   并**真的调用一次**确认能通
2. **建世界** —— 没有 db 就新建,新世界的时钟用**演示速度**(1 tick/秒,约 5 分钟走完一个世界日);
   想要真实时间的一行命令会打印出来
3. **运行** —— 世界在本进程里活起来,叙事逐行打印,Ctrl-C 停止

跑起来长这样:

```console
$ anima-world start

  ANIMA 世界引擎
  ────────────────────────────────────────────

  ① LLM
     ! LLM 未配置 —— 叙事、空闲计划、关系判定都会降级成模板文本
       修复:anima-world config set llm.api_key sk-…

  ② 世界
     ✓ 新建 saves/world.db
     时钟:1 tick/秒 —— 约 5 分钟走完一个世界日(现实时间的 300 倍速)
     ✓ 3 个角色就位: 苏晚夏、陆知遥、沈亦柔

  ③ 运行
     世界在本进程里运行,叙事会打印在下面;停止:Ctrl-C

  [第0天 00:10] 遥:遥四处走了走
  [第0天 00:10] 夏:夏睡下了
  ^C
  世界已停下,快照已保存。下次接着跑:anima-world start
```

出问题了先跑体检:

```bash
anima-world doctor
anima-world config list            # 看配置(密钥自动打码)
anima-world config set llm.api_key sk-…      # 改配置,立即生效
anima-world config set scheduler.tick_rate 0.00333   # 切回真实时间
```

从源码装(开发):

```bash
git clone https://github.com/aubrey-anima/core.git anima-world
cd anima-world && pip install -e ".[dev]" && python -m pytest
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

批处理场景自己推时钟 —— `world.tick(n)` 是同步且确定性的,快进和测试都靠它:

```python
with World.open("w.db") as world:
    world.tick(288)                     # 一个世界日
    print(world.memories("夏"))         # 她记得什么,按分排序
    print(world.needs("夏"))            # {'energy': 0.59, 'hunger': 0.16, ...}
    print(world.cliques())              # 谁和谁抱团了
```

三条使用纪律(`anima_world/api.py` 的 docstring 是权威版本):

1. **一个运行中的世界独占它的 world.db** —— 别的进程不要碰同一个文件;
2. **一个进程一个引擎版本** —— 世界钉死在生成它的版本上,多版本按进程隔离;
3. **信任边界是进程边界** —— `player_id` 只是参数,验证调用者是宿主应用的责任。

## 世界里有什么

四套机制,除记忆外都是默认关闭的开关。默认关是因为:一个只会走路和说话的世界也是正经
世界,而每点亮一套都意味着更多 LLM 开销和更多要操心的面。

| 机制 | 开关 | 点亮后多了什么 |
|---|---|---|
| **记忆** | 常开 | 相关度 × 新近度 × 重要度 三因子检索、定期反思(写出更高阶的记忆)、遗忘曲线 |
| **需求** | `needs.enabled` | `energy` / `hunger` / `social` 逐 tick 衰减,驱动行为树的紧急带 —— 累了就会中断手头的事去睡 |
| **经济** | `economy.enabled` | 物品、钱、店铺、工资、价格漂移。账本是 `payment` 事件的投影,所以余额造不了假 |
| **社交** | `social.enabled` | 三轴关系(常开)+ 八卦二手传播(每手衰减置信度)+ 自发形成的小团体 |

```bash
anima-world config set needs.enabled true --db-path saves/world.db
```

## 它是怎么搭的

**一个卷就是一个世界。** `world.db`(事件/聊天/记忆/配置)+ `world.db.key`
(**搬迁必须随行**,丢了 `llm.api_key` 就解不开,全线降级 Mock)。降级本身是设计,
但不许无声 —— 开机点名,`World.state()` 里的 `runtime.llm.degraded_reason` 常驻。

**事件日志是唯一真相。** 没有 `balances` 表 —— 两个真相源迟早会打架,而且你无法判断
哪个是对的。`world.db` 里存的不是"夏有 50 块",而是"夏为什么有 50 块"。对账 = 重放。

**一个运行中的世界独占它的文件。** 真相有一半在内存里(时钟、投影、锁、线程池),
第二个进程绕过 `World` 直写同一个 db 会立刻分叉。离线处置(打包、快进)在世界关闭后做。

**版本即契约。** 一个版本 = (引擎代码, db format 版本, 包格式版本) 一起冻结:

- `anima_world/__init__.py` 的 `__version__` 是**唯一版本源**(pyproject 动态读取)
- **主版本号 = db 格式**:db 格式变才升第一位,第二/三位都是程序优化
- `db.py` 的 `DB_FORMAT_VERSION` / `MIN_SUPPORTED_DB_FORMAT` 是运行期安全联锁:
  挂错卷会当场拒绝而不是静默写坏

## 把世界寄给别人

世界可以打成一个 `.cyberworld` 文件 —— 模板包(只有种子,世界首启自建 db)或快照包
(带一个跑过的 world.db)。

```bash
anima-world world export --seed seed.json --output my.cyberworld \
    --world-id my-world --name "我的世界" --mode template

anima-world world export --seed seed.json --db-path saves/world.db \
    --beats beats.json --output my.cyberworld \
    --world-id my-world --name "我的世界" --mode snapshot

anima-world world import my.cyberworld --destination ./instances
```

一个包能说出自己需要什么引擎,**而不必先能跑它** —— 会问这个问题的,恰恰是那个还
没装上对应引擎的启动器:

```bash
anima-world world inspect my.cyberworld --json
# {"world_id": "my-world", "engine_min": "2.0.0", …, "runnable": false}
# 跑不了也照样回答,退出码 0 —— 在这里拒绝回答就等于废掉这个格式
```

## 命令

```bash
anima-world start          # 建世界 + 引导 + 运行 —— 从这里开始
anima-world doctor         # 体检:世界文件、密钥、LLM 连通性、时钟快慢
anima-world config         # 读写配置,密钥加密存储、打码显示
anima-world chat           # 和一个角色说话;不给 --agent 就列出住着谁
anima-world run            # 无引导的前台宿主(部署 / 脚本用)
anima-world simulate       # 无头快进(--report 输出运行摘要)
anima-world world          # 导出 / 导入 / 查看 .cyberworld 数据包
```

`start` 是给人用的门(引导 + 演示速度);`run` 是无引导的前台宿主;
真正嵌入到应用里用 `anima_world.api.World`。

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

## 对外契约

跨仓库协作走**文件与 import**,不走网络。文件格式的权威定义在这里,
对端持有冻结镜像,靠互验测试对齐 —— 改它们的线格式等于改跨仓库契约:

| 模块 | 权威内容 | 谁镜像了它 |
|---|---|---|
| `anima_world.world_package` | `.cyberworld` 数据包格式 | 运维台 `lib/worldPackage.js` |
| `anima_world.world_seed` | 种子 schema 校验 | 运维台 `lib/worldSeed.js` |
| `anima_world.beats` | 节拍脚本严格校验 | 创作工作台通过 CLI 委托校验 |

## 文档

| | |
|---|---|
| [docs/REFERENCE.md](docs/REFERENCE.md) | 逐命令、逐函数、逐配置键的参考,以及节拍脚本格式 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 为什么是这个形状:真相模型、tick 帧、线程与锁、不变量、已知架构债 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 开发环境、不能碰的不变量、怎么提改动 |
| [CHANGELOG.md](CHANGELOG.md) | 版本历史 |

## 开发

```bash
pip install -e ".[dev]"
python -m pytest                 # 109 项:db 格式闸门 + 打包契约 + api 门面 + 四大机制
python -m build                  # → dist/*.whl + dist/*.tar.gz
```

## 许可证

[Apache License 2.0](LICENSE) —— 随便用、改、闭源商用,保留版权声明并标注改动过
哪些文件即可。相比 MIT 多一条**专利授权与报复条款**:贡献者授予你专利许可,而谁
拿这份代码去打专利官司,他自己的授权当场终止。

引擎是给宿主 `import` 的库,所以许可证必须宽松到能嵌进闭源应用 —— copyleft
(GPL/AGPL)会传染到每一个宿主,那和这个包存在的理由是矛盾的。
