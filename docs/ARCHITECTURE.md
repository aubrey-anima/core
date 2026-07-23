# anima-world 架构

> 这份文档回答"为什么是这样",接口的逐条说明在 [REFERENCE.md](REFERENCE.md),
> 未来规划在 [ROADMAP.md](ROADMAP.md)。对应引擎版本 1.0.0(db 格式 1)。
>
> 读这份文档前先接受一件事:**引擎不是一个数据库访问层**。它是一个进程内运行时,
> 世界的真相一半在内存里。下面几乎所有设计都是这个事实的推论。

---

## 目录

1. [一句话与三条边界](#1-一句话与三条边界)
2. [真相模型:什么算数据](#2-真相模型什么算数据)
3. [进程内运行时的形态](#3-进程内运行时的形态)
4. [一个 tick 里发生什么](#4-一个-tick-里发生什么)
5. [LLM:四类任务,三个池,零个在 tick 线程上](#5-llm四类任务三个池零个在-tick-线程上)
6. [角色怎么决定做什么](#6-角色怎么决定做什么)
7. [模块地图与依赖方向](#7-模块地图与依赖方向)
8. [关键不变量](#8-关键不变量)
9. [在 ANIMA 系统里的位置](#9-在-anima-系统里的位置)
10. [架构债:已还的与还欠的](#10-架构债已还的与还欠的)

---

## 1. 一句话与三条边界

**anima-world 是一个可 pip 安装的纯库,把一个 `world.db` 跑成一个活着的世界。**

```python
from anima_world.api import World

with World.open("saves/world.db") as world:
    world.tick(288)                  # 快进一个世界日
    print(world.state())
```

没有 HTTP,没有服务器,没有子进程。宿主 `import` 它,世界就活在宿主自己的进程里。

三条边界,都是刻意的:

- **不发任何 HTTP,也不发任何 HTML。** 需要网络暴露的话,由宿主应用自己包一层。
  曾经的 FastAPI web 层(三组 REST API + membership claim 鉴权)已整体移除。
- **不含任何创作代码。** 创作是 `anima-studio` 的事,它给每个引擎版本装隔离 venv、
  全部交互走子进程、**永不 import 本包** —— 因为一个世界文件钉死在生成它的 core
  版本上,能同时持有多版本的工具就不能住在其中任何一个里面。
- **不做跨版本迁移。** 一个 core 版本 = (引擎代码, db 格式, 包格式) 一起冻结。

---

## 2. 真相模型:什么算数据

这是整个引擎最需要先理解的一层。世界里的信息分成**四类**,归属不同、寿命不同:

| 层 | 存在哪 | 例子 | 怎么恢复 |
|---|---|---|---|
| **历史(唯一真相)** | `events` 表,append-only | 谁去了哪、谁给谁付了钱、关系变动了多少 | 它就是真相,不需要恢复 |
| **投影(派生)** | 只在内存 | 余额、库存、在场、关系亲密度、当前地图占位 | 开世界时从 seq=0 全量重放 |
| **当前值(data-plane)** | 独立的表 | `agent_needs` 需求水平、`shop_stock` 货架、`config`、`locations` 地图、`bt_nodes` 行为树 | 表里就是当前值 |
| **派生缓存** | 表,随时可丢 | `cliques` 小团体、`memories` 记忆 | 重算/重放即可 |

### 为什么余额没有表

因为**两个真相源迟早会打架**。如果既有 `payment` 事件、又有 `balances.amount` 字段,
那么"写事件成功但更新余额失败""某段代码绕过事件直接改余额""并发下两条路径交错"
都会让它们分叉 —— 而且你**无法判断哪个是对的**。经典的凭空造钱 bug 就是这么来的。

事件日志唯一时,**对账 = 重放**,天生防复制品。`world.db` 里存的不是"夏有 50 块",
而是"夏为什么有 50 块"的完整账目;前者能从后者算出来,后者不能从前者算出来。

同一条原则适用于关系亲密度、在场位置、叙事日志。

### 那为什么需求曲线又落表了

因为它们是**连续量,不是交易**。需求每 tick 都在变,记成事件会淹没日志;而它们是
纯数学(衰减率 × tick 数 + 动作恢复),重放 tick 即可重建。所以 `agent_needs` 只在
日切和关闭时做检查点,进程被 kill 掉最多退回上一个检查点。

判据是:**"发生了一件事"进事件日志,"现在是多少"进 data-plane 表。**

### 没有快照表

曾经有一张 `snapshots`,想做重放加速器。它被删了 —— 详见[第 10 节](#10-架构债已还的与还欠的)。

---

## 3. 进程内运行时的形态

`World.open()` 建的不是一个连接,是一个运行时。实测它的线程形态:

```
import 后:  ['MainThread']
open  后:  ['MainThread']                                   ← 只建对象,线程池懒启动
tick  后:  ['MainThread', 'narrative_0', 'narrative_1']     ← LLM 池按需起线程
start_clock 后: [..., 'Thread-1 (_loop)', 'Thread-2 (_reaper)']
close 后:  ['MainThread']                                   ← 排干,幂等
```

内存里持有的东西:

- **`Projection`** —— 在场角色、关系(含三轴)、地点、余额、库存、叙事日志
- **世界时钟** `scheduler.clock`(tick 计数)
- **行为树黑板** —— 每个角色的 `need.*`、`time.*`、`mailbox`、计划步
- **三个 LLM 线程池** —— narrative / planner / judge
- **一把 RLock** —— 全系统唯一(投影、时钟、邮箱都归它守)

由此来的三条硬约束:

1. **一个运行中的世界独占它的 `world.db`。** 第二个进程绕过 `World` 直写同一个 db
   会立刻分叉 —— 内存里那一半不知道你改了什么。离线处置(打包、导出)在世界关闭后做。
2. **`close()` 不是可选的。** 它停时钟、排干线程池。事件每 tick 已落盘,所以退出时
   不额外写;但需求曲线这类只在日切落盘的会退回上一个检查点。
3. **一个进程一个引擎版本。** `import` 是进程级的,信任边界就是进程边界。

### 两种驱动方式

| | 谁推时钟 | 用途 |
|---|---|---|
| `world.tick(n)` | 宿主自己的循环 | 快进、测试、批处理;**确定性** |
| `world.start_clock()` | 后台线程按 `tick_rate` | 真的"活着"的世界,宿主在别的线程读 `state()` |

`start_clock` 的睡眠被切成 ≤0.5 秒的片、每片重读速率 —— 否则 1 tick/5 分钟的世界里
一次 `sleep(300)` 会把热更新和优雅停机钉住五分钟。

---

## 4. 一个 tick 里发生什么

`Scheduler.tick()` 全程持有那把唯一的 RLock,顺序**是有意的**:

```
0. 推进时钟 → 跨过世界日边界则 _on_day_rollover()
1. 排干事件队列(处理,然后触发大脑)
2. 落地到达 —— 本 tick 结束旅程的角色先被放到目的地
3. 触发剧情节拍(beats)
4. 逐角色:写时间到黑板 → 结算需求 → 收邮箱 → 写计划步 → 跑行为树 → 发动作
5. idle 看门狗(给休眠角色注入 idle 事件)
```

**第 2、3 步为什么必须在第 4 步之前**:同一条规则 —— *派生状态不许滞后于事件*。
到达先落地,树才能"基于真的在那儿"做决定;节拍先注入,下面每个决定才看得到被改过的
状态。反过来就会出现"角色在门口徘徊一个 tick"这类幽灵行为。

### 日切(`_on_day_rollover`)

每世界日一次,不是每 tick 一次。做四件纯算术/SQL 的事:

- 记忆遗忘曲线结算(Ebbinghaus:强度越高忘得越慢,闲置越久掉得越多)
- 需求水平落盘检查点
- 经济日结:发工资、补货、价格漂移
- 小团体重算(friendship 连通分量)

**这里和 tick 帧里一样,一次 LLM 都不许调。**

---

## 5. LLM:四类任务,三个池,零个在 tick 线程上

这是全引擎最重要的一条纪律:**LLM 客户端是注入的,且永不在 tick 线程调用。**
一次 30 秒超时的调用要是落在 tick 线程上,整个世界会卡住 30 秒。

| 任务 | 跑在 | 干什么 | 失败了会怎样 |
|---|---|---|---|
| **叙事** | `narrative` 池(2 线程) | 把动作写成一行文字 | 丢一行文风,世界照跑 |
| **规划** | `planner` 池(2 线程) | 给自由时间排计划 | 树 fall through 到 idle |
| **关系判定** | `judge` 池(2 线程) | 聊完给出摘要 + 非对称的关系增量 | 关系不变 |
| **反思** | 复用 `judge` 池 | 由记忆归纳出洞察 | 不产生洞察 |

反思复用 judge 池而不是新开一个,是因为两者延迟画像相同、都只做**记录**、
从不有任何东西等它们 —— 除了 `stop()` 时的最后排干。

工作线程的纪律:**在锁内快照上下文,在锁外调 LLM,拿到结果再进锁记事件。**
工作线程从不读活状态。

LLM 不可用时全线降级 Mock,**世界照跑** —— 但降级不许无声:
`ConfigStore.undecryptable_secrets()` 区分"没配过"和"读不出来",开机点名,
`World.state()` 的 `runtime.llm.degraded_reason` 常驻。

---

## 6. 角色怎么决定做什么

```
需求带(高优先级)          ← 饿了就先吃,不管你今天该开店
  ├ NeedAction energy < 0.15 → go_sleep
  ├ NeedAction hunger < 0.15 → eat
  └ NeedAction social < 0.15 → idle_social
作者写的树
  ├ TimeWindow 08:00–18:00 → 值班动作
  ├ follow_plan            ← LLM 规划的这一步
  └ idle_wander            ← 兜底,永不"脑死"
```

需求带**无条件包在作者的树外面**,但它是惰性的:`NeedAction` 读 `need.<name>`,
黑板上没有需求值(需求系统没点亮)就返回 FAILURE,树的行为与不带它时逐 tick 一致。
这让"给所有树套上需求带"这件事变得安全。

动作产生后:

```
动作 → agent_action 事件 → 投影折叠 → 记忆触发器 → (够重要?) → 记忆
                                    → 关系判定(chat)→ state_change 事件 → 三轴关系
                                    → 八卦抽样(chat / idle_social)→ memory_seed 事件
```

**注意八卦走的也是普通 memory_seed 事件** —— tick 线程只负责掷骰子,传闻本身是可
重放的历史,记忆触发管线一行没改。这是"新机制不要长出第二条写入路径"的例子。

---

## 7. 模块地图与依赖方向

依赖是**单向**的,从下往上:

```
门面        api.py(World —— 宿主唯一需要认识的类)、__main__.py(CLI)
  ↑
编排        scheduler.py(世界时钟、邮箱、tick 帧;系统唯一的 RLock)
  ↑
子系统      brain.py agent.py bt_nodes.py actions.py      决策
            narrative.py planner.py relationship_judge.py  LLM 任务
            chat_service.py chat_session.py chat_store.py  聊天(与事件核解耦)
            memory_store.py memory_triggers.py graph.py    记忆与图谱
            world_store.py locations.py prompt_store.py    世界数据
            config_store.py llm_client.py beats.py         配置/LLM/节拍
  ↑
事件核      events.py(append-only 日志)projection.py types.py db.py
  ↑
纯函数      needs.py economy.py gossip.py cliques.py memory_retrieval.py
            world_time.py
```

**最底下那层是刻意的**:`needs` / `economy` / `gossip` / `cliques` /
`memory_retrieval` 不 import 任何存储层、不碰锁、不掷骰子之外做主意。它们是可以
单独推理和测试的纯数学。想把 `memory_retrieval.similarity()` 换成向量嵌入?
换掉那一个函数,打分公式不动。

对外还有三个模块是**跨仓库契约的权威定义**(改它们的线格式 = 改契约):

| 模块 | 内容 | 谁镜像了它 |
|---|---|---|
| `world_package.py` | `.cyberworld` 数据包格式 | 运维台 `lib/worldPackage.js` |
| `world_seed.py` | 种子 schema 校验 | 运维台 `lib/worldSeed.js` |
| `beats.py` | 节拍脚本严格校验 | (无镜像;创作台经 CLI 委托) |

---

## 8. 关键不变量

改代码前请先读这一节。每一条后面都有一次真实的事故。

- **scheduler 持有系统唯一的 RLock。** 别引入第二把 —— 两把锁就是死锁的开始。
- **`start` 是人的门,`run` / `World.open` 是程序的门。** `start` 会引导配置 LLM、
  给新世界换成演示速度(1 tick/秒);另外两个一概不做。别把这两条路径搅在一起。
- **`world.db.key`(Fernet 密钥)必须随 db 搬迁。** 丢了 `llm.api_key` 就解不开,
  全线降级 Mock。降级是设计,**无声降级不是**。
- **聊天子系统与事件核解耦。** 整场会话只在关闭时发一个事件;完整转录留在宿主应用里,
  不落世界。世界只收当轮有限历史。
- **坏节拍脚本必须在加载时当场报错**,不能流到世界启动。
- **表重建迁移必须在单个事务里。** `executescript()` 会隐式 COMMIT —— 曾经因此
  在崩溃后留下已提交的空新表,检测误判"已迁移"永不重试,legacy 数据永久孤儿。
- **db 格式联锁**:`DB_FORMAT_VERSION` 与 `MIN_SUPPORTED_DB_FORMAT` 相等即"硬不兼容",
  挂错卷当场拒绝而不是静默写坏。
- **`world_seed.json` 是唯一的 package data**,随 wheel 分发。
- **发行包只装引擎代码。** 测试、文档、世界数据都不进 wheel/sdist
  (`MANIFEST.in` + `tests/test_packaging.py` 双向守着)。

---

## 9. 在 ANIMA 系统里的位置

ANIMA 是多个**互相独立**的仓库,跨仓库零 import:

```
任何 Python 宿主 ──import anima_world.api──▶ world.db(世界在宿主进程里活)
anima-operator(运维台,Node)──CLI / .cyberworld 文件──▶ 本包
anima-studio(创作台)──子进程──▶ 本包的某个版本 ──▶ .cyberworld
```

协作只有两种方式:**Python 宿主直接 import(pip 钉版)**,或**CLI 子进程 +
`.cyberworld` 文件**(非 Python 端、跨版本场景)。非 Python 的宿主接不进来,
这是纯库化的直接代价,也是刻意的取舍 —— 换来的是没有网络层要维护、没有鉴权要防守。

Python 侧的对外接口是 `api.py` 的 `World` 门面(加 CLI)。它是宿主应用依赖的 API 面:
**只加不改**,破坏性变更等于跨仓库破坏。

---

## 10. 架构债:已还的与还欠的

### 已还(1.0.0 发版前清理)

**两份投影合并成一份。** 引擎曾同时持有 `scheduler._memory_projection`(每条事件都
折叠,始终正确)和 `_WorldView.projection`(开机全量重放建起来,此后只同步叙事日志和
角色位置,经济与关系一概不折叠)。后者在运行中停在开机状态 —— 从它读余额或关系会读到
旧值,开世界还要多重放一遍整个日志。现在 `_WorldView.projection` 是个 property,直接
返回 scheduler 那份;守它的 `_projection_lock` 也一并删掉,系统重新只剩一把锁。
角色位置改为在 `state()` 快照时读活黑板,不再维护副本。

> 已删除的 `snapshots` 表正是这笔债的产物:它把那份陈旧投影写回库、还盖上当前的
> `MAX(seq)` 戳,于是每次 close 都往库里留一份自称最新、实际停在开机状态的错账,
> 误差单调累积。而真正驱动世界的投影从不读它,加速一次也没兑现。

**反思水位线离开 tick 热路径。** 它曾在每次记忆写入时做 INSERT + SELECT + COMMIT,
在世界唯一的锁里,只为维护一个丢了也不要紧的计数器。现在活在内存里,只在三个时刻碰库:
每个角色首次读取、反思触发、日切与关闭的检查点。

**遗忘曲线不再依赖 SQL 的 `pow()`。** 那个函数要 SQLite 编译时打开
`SQLITE_ENABLE_MATH_FUNCTIONS` 才有;缺了会抛 `OperationalError`,而日切把异常吞成
一条 warning —— **遗忘会静默地永不发生**。算术改到 Python 里做(一天一次、每人几十行,
代价可忽略),数值与原 SQL 版逐位一致。

**`retrieve()` 的写入变成显式的。** 它是个名字像"读"的写接口(命中即加固,
"检索就是复习"是有意的设计),但宿主每轮聊天经 `agent_context` 调一次就写 5 行。
现在加了 `reinforce: bool = True`,调试和只读视图可以传 `False` 走纯读路径。

### 还欠的

**八卦只在同地传播。** 没有跨地点的路径,谣言不会自己走到别的镇子。设计如此,记在这里
是因为它常被误以为是 bug。

**经济还没有"劳动"。** `economy.daily_wage` 是小镇金库无条件发的,不需要角色去打工;
金库允许无限负债。ROADMAP 里"行为树会去打工"那部分尚未实现。

**genesis stipend 无条件落盘。** 即使 `economy.enabled=False`,每个角色开局也有一条
30.0 的 `payment` 事件。无害(经济关掉时账本没人读),但"经济关掉"的世界事件日志里
仍然有账本事件。刻意不加开关:否则后来才点亮经济的世界会一分钱都没有。

**货架对滞销品也补货。** `durable` 类(如速写本)没人买也每天补 3 件直到上限 20,
同时价格一路跌向 `base×0.25`。等 v4 之后的经济迭代一起处理。
