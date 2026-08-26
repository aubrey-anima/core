# anima-world 架构

> 这份文档回答"为什么是这样",接口的逐条说明在 [REFERENCE.md](REFERENCE.md)。
>
> 读这份文档前先接受一件事:**引擎不是一个数据库访问层**。它是一个进程内运行时,
> 世界的真相一半在 Redis 上、一半在内存里(折出来的投影)。下面几乎所有设计都是这个事实的推论。

## 这份文档对到哪一版:**逐节盖章,不盖一个总数**

**2026-08-25 逐节核过一遍。** 这一节是这次核对的结果,也是下一个人接着核的起点。

盖一个总的版本号是这份文档上一次烂掉的方式:它抬头写着「对应引擎版本 1.0.0(db 格式 1)」,
而**「db 格式」这个东西 2.0 那一轮整个删掉了**(`ls anima_world/db.py` → 没有那个文件)。
一个数盖不住十节各自漂了多远 —— **所以下面一节一个日期、一条敲得动的判据。**

| 节 | 对到 | 这一轮做了什么 | 判据(照着敲) |
|---|---|---|---|
| [1 一句话与三条边界](#1-一句话与三条边界) | **3.7.0** | 那段示例代码原先打开 `saves/world.db`,**照着敲会当场报错**;换成今天真跑得动的三行 | 见该节代码块下面那条 |
| [2 真相模型](#2-真相模型什么算数据) | **3.7.0** | 道理一个字没动,**衬底换了**:表名 → Redis 键 / MySQL 表 | `anima-world contract --json \| jq .storage` |
| [3 运行时的形态](#3-进程内运行时的形态) | **3.7.0** | 线程形态**重新实测**(多出 `anima-chat-loop`);🔴 "一个世界独占它的 db" 这条**今天是反的** | 该节那段脚本 |
| [4 一个 tick 里发生什么](#4-一个-tick-里发生什么) | **3.7.0** | 原先 6 步,今天 **15 步**:规律、长过程、邀请、自主、contact、量与位置全是 2.0 之后长出来的 | `sed -n '/def tick/,/_idle_watchdog()/p' anima_world/scheduler.py` |
| [5 LLM 与线程](#5-llm四类任务三个池零个在-tick-线程上) | **3.7.0** | 三个池仍然对,但**漏了那条常驻 asyncio 循环**(聊天 / 自主 / contact 都跑在它上面) | `grep -n ThreadPoolExecutor anima_world/scheduler.py` |
| [6 她怎么决定做什么](#6-角色怎么决定做什么) | **3.7.0** | 原先只有"需求带 + 作者的树";**本体、代价、感知、自主轮次四层是 2.0 之后加的**,全补上 | `anima-world contract --json \| jq .seed.affordance_keys`(12 格,`requires`/`costs`/`consumes`/`duration` 都在里面) |
| [7 模块地图](#7-模块地图与依赖方向) | **3.7.0** | `db.py` / `graph.py` 已不存在,而地图上还画着;契约那张表也旧了 | `ls anima_world/*.py \| wc -l` |
| [8 关键不变量](#8-关键不变量) | **3.7.0**,但**它不是权威** | 四条讲 SQLite 的已作废;**这一节现在只留指路牌** —— 不变量的权威是 `../CLAUDE.md` | 见该节 |
| [9 在 ANIMA 系统里的位置](#9-在-anima-系统里的位置) | **3.7.0** | 图里的 `world.db` 换成 Redis;"协作只有两种"漏了 HTTP 那一种(不在引擎里,但读的人要知道) | `../../../docs/ANIMA-分工与协作.md` |
| [10 架构债](#10-架构债已还的与还欠的) | 🔴 **1.0.0(历史存档,有意不改)** | 整节讲的是 1.0.0 发版前那次清理,里面的 `snapshots` 表、SQL `pow()`、`_projection_lock` 今天都不存在了。**它是病历,不是现状** —— 删掉等于把当初为什么这么做也一起删了 | 该节抬头 |

**这份文档不是任何东西的权威。** 当前实现以 [REFERENCE.md](REFERENCE.md)(公开方法与配置键
都有测试闸盯着)、[../CLAUDE.md](../CLAUDE.md)(不变量)与 [CHANGELOG.md](../CHANGELOG.md) 为准。
[ROADMAP.md](ROADMAP.md) **不是未来规划,是 1.0.0 之前的设计存档**。

🔴 **而"不是权威"在这个仓库里有一个具体的、会咬人的含义:这份文档上没有任何一道闸。**
`tests/test_reference_docs.py` 只读 `REFERENCE.md` 与 `FOR-STUDIO.md`
(判据:`git grep -n '_DOCS_DIR /' tests/test_reference_docs.py` —— 两行,没有 ARCHITECTURE),
所以这里写一个**根本不存在的符号名**、画一个**已经删掉的模块**,测试一条都不会红。
上一版正是这么烂的:`db.py` 在地图上画了半年,而那个文件 2.0 那轮就删了。
**上面那张逐节盖章表是这道缺失的闸的替代品** —— 它不会自己变红,但它给了下一个人
一条一条去敲的路。改这份文档的人有一件义务:**改哪一节,就把那一节的日期和判据一起改。**

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

**anima-world 是一个可 pip 安装的纯库,把一个 Redis 键前缀跑成一个活着的世界。**

```python
import redis
from anima_world.api import World

client = redis.Redis.from_url("redis://127.0.0.1:6379/0", decode_responses=True)
with World.open("world", redis=client) as world:
    world.tick(288)                  # 快进一个世界日
    print(world.state())
```

判据(这三行是真跑过的,不需要真 Redis):

```console
$ python -c "import fakeredis; from anima_world.api import World; \
    w=World.open('w', redis=fakeredis.FakeRedis(decode_responses=True), force_mock_llm=True); \
    w.tick(288); print(w.state()['world_time']); w.close()"
{'day': 1, 'hour': 14, 'minute': 30, 'minute_of_day': 870, 'tick': 462}
```

⚠️ **`tick` 那一格是 462 而不是 288,这不是打错** —— 内置橱窗世界的创世把时钟拨到了
`world.start_time`(不是 0),而 `world.tick(n)` 是**再推 n 下**。拿 `tick` 当"我推了几下"
读会差出一个开局时刻,而两个数看上去都很像对的。

⚠️ **`decode_responses=True` 是契约的一部分**,不是随手写的:裸 bytes 客户端下量名会变成
`b'树高'`,规律**静默失配** —— 世界照跑,树三个月不长。

没有 HTTP,没有服务器,没有子进程。宿主 `import` 它,世界就活在宿主自己的进程里
(数据在 Redis 上,所以**多个进程可以驱动同一个世界**,见第 3 节)。

三条边界,都是刻意的:

- **不发任何 HTTP,也不发任何 HTML。** 需要网络暴露的话,由宿主应用自己包一层。
  曾经的 FastAPI web 层(三组 REST API + membership claim 鉴权)已整体移除,
  今天那层壳住在运维台的 `deploy/world-image/server/`。
- **不含任何创作代码。** 创作是 `anima-studio` 的事,它给每个引擎版本装隔离 venv、
  全部交互走子进程、**永不 import 本包** —— 因为一个世界包钉死在生成它的 core
  版本上,能同时持有多版本的工具就不能住在其中任何一个里面。
  `import anima_world.author` 必须 `ModuleNotFoundError`,`tests/test_packaging.py` 守着。
- **不做跨版本迁移。** 一个 core 版本 = (引擎代码, 存储契约, 包格式) 一起冻结,
  换钉是显式的运维决定。

---

## 2. 真相模型:什么算数据

这是整个引擎最需要先理解的一层。世界里的信息分成**四类**,归属不同、寿命不同:

| 层 | 存在哪 | 例子 | 怎么恢复 |
|---|---|---|---|
| **历史(唯一真相)** | `:events`(无 MySQL 时)或 `{world_id}_events` 表,append-only | 谁去了哪、谁给谁付了钱、关系变动了多少 | 它就是真相,不需要恢复 |
| **投影(派生)** | 只在进程内存 | 余额、库存、在场、关系亲密度、当前地图占位 | 开世界时从 seq=0 全量重放 |
| **当前值(data-plane)** | 各自的 Redis 键 | `:needs` 需求水平、`:shop_stock` 货架、`:config`、`:locations` 地图、`:bt_nodes` 行为树、`:stock:{owner}` 世界的量 | 键里就是当前值 |
| **派生缓存** | 键,随时可丢 | `:cliques` 小团体、`:memories` 记忆 | 重算/重放即可 |

⚠️ **别照这张表写代码** —— 键名的权威是 `anima-world contract --json` 的 `storage` 段
与 [REFERENCE.md §8](REFERENCE.md) 那张键清单;这里列几个只是为了讲清"哪一类住哪儿"。

### 为什么余额没有表

因为**两个真相源迟早会打架**。如果既有 `payment` 事件、又有一个 `balances` 字段,
那么"写事件成功但更新余额失败""某段代码绕过事件直接改余额""并发下两条路径交错"
都会让它们分叉 —— 而且你**无法判断哪个是对的**。经典的凭空造钱 bug 就是这么来的。

事件日志唯一时,**对账 = 重放**,天生防复制品。世界里存的不是"夏有 50 块",
而是"夏为什么有 50 块"的完整账目;前者能从后者算出来,后者不能从前者算出来。

⚠️ 这条不只是引擎内部的洁癖,它是**跨仓库的权限矩阵**的地基:总图里
「演化态谁都不许改」那一条,理由逐字就是这一段 —— **手改一条演化态 = 伪造历史 =
投影和日志对不上,而且没有任何地方会报错。**

同一条原则适用于关系亲密度、在场位置、叙事日志。

### 那为什么需求曲线和"量"又落键了

因为它们是**连续量,不是交易**。需求每 tick 都在变,记成事件会淹没日志;而它们是
纯数学(衰减率 × tick 数 + 动作恢复),重放 tick 即可重建。所以 `:needs` 只在
日切和关闭时做检查点,进程被 kill 掉最多退回上一个检查点。

**世界的量(`:stock:{owner}`)同理,而且它把这条推到了尽头**:2.0 之后树怎么长、
矿怎么枯是**作者写的规律**(`:world_rules`)在算,规律**连续变化不发事件** ——
只有 `emit` 的门槛跨过去了才发一条。不这么做的话,一棵树长三个月就是三万条日志。

判据是:**"发生了一件事"进事件日志,"现在是多少"进 data-plane 键。**

### 那什么进 MySQL

第二条分家线和上面那条是**正交**的:同样是"历史",有的必须留在 Redis,有的必须搬走。
判据不是冷热,也不是增长性,是 **"她带不带得进上下文"**:

```
进得了提示词的  →  必须有界  →  Redis
进不了提示词的  →  可以无限  →  MySQL(要用时按 k 取回来)
```

给了 `mysql=` 的世界把四样搬过去:`events` / `memories` / `conversations` / `messages`
(表前缀 `{world_id}_`)。这条比"冷热"好用,因为它**可验**:分对了的话提示词就不随
世界变老而涨(实测 60 世界日,后端涨 61 倍而提示词 2251 → 2272 字,`tests/test_bounded.py` 是这道闸)。

⚠️ **`edges` 一度被分错**,照着"像不像历史"分的:它有 `UNIQUE(subject,predicate,object)`
且谓词是闭集,上界 2×N²,**按世界的规模封顶而不是按时间涨** —— 它属于 Redis。
那个闭集是承重的,放开谓词就要重算这笔账。

### 没有快照表

曾经有一张 `snapshots`,想做重放加速器。它被删了 —— 详见[第 10 节](#10-架构债已还的与还欠的)。

---

## 3. 进程内运行时的形态

`World.open()` 建的不是一个连接,是一个运行时。**2026-08-25 实测**它的线程形态
(fakeredis,`force_mock_llm=True`):

```
import 后:      ['MainThread']
open  后:      ['MainThread', 'anima-chat-loop']          ← 那条常驻 asyncio 循环
tick  后:      ['MainThread', 'anima-chat-loop', 'narrative_0', 'narrative_1']
start_clock 后: [..., 'Thread-1 (_loop)', 'Thread-2 (_reaper)']
close 后:      ['MainThread']                              ← 排干,幂等
```

判据(照着敲,不需要真 Redis):

```console
$ python - <<'PY'
import threading, fakeredis
from anima_world.api import World
n = lambda: sorted({t.name for t in threading.enumerate()})
r = fakeredis.FakeRedis(decode_responses=True)
w = World.open("w", redis=r, force_mock_llm=True); print("open :", n())
w.tick(1);                                        print("tick :", n())
w.close();                                        print("close:", n())
PY
```

⚠️ 上一版这里少了 `anima-chat-loop`,而它是 `open` 就起的、聊天与定时轮次全跑在它上面
—— 少画一条线程的代价不是排版,是**下一个人会以为那些活跑在池子里**。

内存里持有的东西:

- **`Projection`** —— 在场角色、关系(含三轴)、地点、余额、库存、叙事日志
- **世界时钟** `scheduler.clock`(tick 计数)
- **行为树黑板** —— 每个角色的 `need.*`、`time.*`、`mailbox`、计划步
- **三个 LLM 线程池** —— narrative / planner / judge
- **一把进程内 RLock** —— 进程内唯一(投影、时钟、邮箱都归它守)

由此来的三条硬约束(**第 1 条 2.0 之后整个反过来了**):

1. 🔴 **一个世界可以被很多进程同时驱动。** 这一条上一版写的是"一个运行中的世界
   **独占**它的 `world.db`,第二个进程直写就分叉" —— 那是 SQLite 时代的话。今天世界住
   Redis,`act()` / `intend()` / 导出**在一把跨进程的世界锁(`:lock`)下执行**
   (`RedisLock`,`api.py` 的 `world._world_lock`)。
   代价换了个形状,但没有消失:**内存里那份投影是各进程各一份的**,所以水位
   (`_projection_seq`)的含义是「≤ 它的事件我都折过了」,任何一处把它推过没折过的事件,
   那些事件**再也补不回来**。跑着的世界会在下一次追加时自愈,**暂停的不会** ——
   于是只读的门(`state()` / `roster()`)自己补课。
2. **`close()` 不是可选的。** 它停时钟、排干线程池、放掉那把跨进程锁。事件每 tick 已落库,
   所以退出时不额外写;但需求曲线这类只在日切落盘的会退回上一个检查点。
3. **一个进程一个引擎版本。** `import` 是进程级的,**信任边界就是进程边界**。

⚠️ 还有一条不在这三条里、但同级要紧的:**开机会点名这个 Redis 的持久化**
(`durability_warning`)。AOF 关着的 Redis 一重启,世界退回创世那一刻,**而且不报错**。

### 两种驱动方式

| | 谁推时钟 | 用途 |
|---|---|---|
| `world.tick(n)` | 宿主自己的循环 | 快进、测试、批处理;**确定性** |
| `world.start_clock()` | 后台线程按 `tick_rate` | 真的"活着"的世界,宿主在别的线程读 `state()` |

`start_clock` 的睡眠被切成 ≤0.5 秒的片、每片重读速率 —— 否则 1 tick/5 分钟的世界里
一次 `sleep(300)` 会把热更新和优雅停机钉住五分钟。

---

## 4. 一个 tick 里发生什么

`Scheduler.tick()` 全程持有那把进程内唯一的 RLock,顺序**是有意的**。
**2026-08-25 逐行核过**(上一版停在 6 步,那是 1.x 的帧;下面这 15 步是今天的):

```
0.  盖本次开机水位(只在这一趟第一次真推时钟时)  ← doctor 的"最近这一段"从这儿起算
0.5 推进时钟 → 跨过世界日边界则 _on_day_rollover()
1.  排干事件队列(处理,然后触发大脑)
2.  落地到达 —— 本 tick 结束旅程的角色先被放到目的地
3.  触发剧情节拍(beats)
3.5 到点的"等会儿再说":她真的回来敲一次门
3.55 在场玩家此刻各自在做什么(快照)          ← **必须早于规律**
3.6 世界的规律:树在长、矿在枯(world-rules)   ← 纯算术,tick 线程上跑
3.65 到点的长过程收尾:椅子做好了、孩子生下来了  ← **必须早于行为树**
3.67 到点没人答的邀请(按世界时钟数,不按墙钟)
3.7 定时轮次:问问她此刻要不要自己做点什么      ← 只投递,立刻返回
3.75 她会不会想起一个**不在跟前**的玩家
3.8 在场玩家站在哪 → 可见性表
4.  逐角色:写时间到黑板 → 结算需求 → 量进黑板(只放她感知得到的)→ 她站在哪进可见性表
       → 工资计时 → 收邮箱 → 写计划步 → 跑行为树 → 发动作
5.  idle 看门狗(给休眠角色注入 idle 事件)
```

判据:

```console
$ sed -n '/    def tick(self)/,/_idle_watchdog()/p' anima_world/scheduler.py
```

**为什么这几步必须排在行为树前面** —— 全是同一条规则:*派生状态不许滞后于事件*。

- **到达先落地**,树才能"基于真的在那儿"做决定;反过来就是"角色在门口徘徊一个 tick"。
- **节拍先注入**,下面每个决定才看得到被改过的状态。
- **玩家在做什么先快照**,因为规律里 `{"action": "chat"}` 那半边读的就是这份名单 ——
  晚一步等于拿上一 tick 的名单算这一 tick 的量。
- **长过程先收尾**,不然占着她的那件事这一 tick 还被当成在忙,**每个长过程都白白多占
  一 tick,而且没人看得出来**。
- **自主轮次只投递**:它要打网络,所以丢到别条线程上去(第 5 节),
  `tick()` 本身立刻返回 —— **时钟永不等网络**。

### 日切(`_on_day_rollover`)

每世界日一次,不是每 tick 一次。做的全是纯算术/SQL:

- 记忆:**夜间固化**开着就跑固化(它自带一遍衰减),否则跑遗忘曲线衰减
  —— ⚠️ **是 if/else 不是两段都跑**:`decay_pass` 不幂等,跑两遍是平方,
  实测一天之后强度 0.125(该是 0.35),配上 `prune_below` 就成了
  "她一觉醒来把昨天忘干净了",而**日志一条不错、每条记忆单看都合法**
- 需求水平落盘检查点
- 反思水位线落盘
- 经济日结:发工资、补货、价格漂移
- 当日计数清零(八卦、她今天开口约了谁)
- 小团体重算(friendship 连通分量)

**这里和 tick 帧里一样,一次 LLM 都不许调。**
(⚠️ 夜间固化里那次反思是 LLM —— 它跑在 judge 池上,日切只是**触发**它。)

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

**上面那张表漏了第五个执行体,而 2.0 之后最要紧的几件事都跑在它上面**
(`anima-chat-loop`,`open` 就起、一条常驻的 asyncio 循环):

| 谁 | 怎么上去的 |
|---|---|
| 聊天(`World.chat` / `chat_burst`) | 门面把协程投上去,同步等结果 |
| **定时轮次**(autonomy) | tick 线程 `run_coroutine_threadsafe` **投完就走**,异常经回调喂回 `autonomy_stats()` |
| **她想起一个不在跟前的玩家**(contact) | 同上 |

**"投完就走"是这条不变量在 2.0 之后的新战场**:自主轮次是 tick 帧里唯一一件要打网络的事,
它但凡同步等一下,时钟就等了网络。`tests/test_autonomy.py::test_the_clock_never_waits_for_the_network`
是这条的闸,而**那条测试自己的判据被换过一次** —— 它原先量 `tick()` 花了几秒,
于是一台忙机器就能让它红,红出来的话还是「tick 被 LLM 拖住了」(2026-08-25 改成
"那次调用做完了没有"这个事实,见 CHANGELOG)。

工作线程的纪律:**在锁内快照上下文,在锁外调 LLM,拿到结果再进锁记事件。**
工作线程从不读活状态。

LLM 不可用时全线降级 Mock,**世界照跑** —— 但降级不许无声:开机点名,
`World.state()` 的 `runtime.llm.degraded_reason` 常驻,`anima-world doctor` 会真调一次。

⚠️ **钥匙不住在世界里**(`~/.anima-world/config.json`,0600):`.cyberworld` 是**分发物**,
打包发出去的世界不该带着作者的钥匙。往世界里写非空密文值今天是**当场 `RuntimeError`** ——
不是加密,是引擎手里已经没有钥匙了(Fernet 随 SQLite 一起退役)。

---

## 6. 角色怎么决定做什么

### 6.1 行为树那一半(1.x 就有,今天一个字没变)

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

### 6.2 本体层:她做得成什么(**2.0 加的,上一版这份文档整层都没有**)

行为树答的是"她想做什么",本体层答的是"她**做不做得成**、要付什么"。
作者在世界文件里声明 `kinds`(这个世界有哪些**种类**的东西:量、能力、提示词预算)
与 `entities`(每一个的 id/name/gloss/location),一次能力调用走

```
act(agent, "interact", {"target": …, "verb": …}, surface="body")
```

**声明本身就是开关**:不写 `kinds` 的世界这一层整个缺席,行为与从前逐位相同。
一旦作者写下 `kinds`,建议就变成闸 —— 量名拼错(`树髙` vs `树高`)**当场开不了机**。
放行的样子才是这个仓库最怕的:安静地建成第二个量,规律照跑、日志干净,
作者要到发现那棵树三个月没长才知道。

能力是**施动者与对象之间的关系,不是对象单方面的属性**(Gibson)。四种代价,
一层比一层封得死:

| 代价 | 是什么 | 为什么要它 |
|---|---|---|
| `requires` | 只准读 `me_*`(她身上的量) | 不成立**永远只有一个意思**:你做不了。能读对象的量它就和 `when` 分不开,而分得开正是它存在的全部理由 |
| `costs` | 花掉她的量 | 一个**总能成功的动作产生不了任何决策** —— 没有理由挑先做哪件、歇一会儿、或者变强 |
| `consumes` | 花掉随身的东西(`have_X` 读库存) | 自带一道"你得有"的门,**不必再写一遍 `requires`** |
| `duration` / `occupies` | 时间 | 量和材料都能靠"睡一觉就回来"绕开,**一段时间过不去就是过不去** |

于是拒绝**不是一类,是四类**,而且必须分得开:`conditions`(这会儿不行 → 等)、
`incapable`(她做不了 → 去歇着)、`busy`(手上那件长过程还没完 → 等自己)、
以及"讲不通"的那一摞(不认识的东西/动词、不在一处)。
**合成一个的话,一个累坏了的人会挨棵树轮着试过去,每一棵都回她"再等等"。**

世界还**自己长得出新东西**(`spawn` / `destroys_target`):一个不能长出新东西的世界是个
西洋镜。**生成必须要代价,而代价由作者写、不是引擎发配额** —— 声明了 `spawn` 却没写
任何一样代价,**开不了机**。理由:配额是**引擎的天花板**,撞上去时她收到的拒绝在世界里
没有意义,她也永远学不会;代价是**世界的理由**,她知道自己为什么做不到。
⚠️ 而代价只封得住速率、封不住存量(体力天天回满 → 一百天一百个孩子),所以**生灭同一轮加**。

### 6.3 感知层:她**知道**世界里的哪些(1.3.0 加,2.0 之后跟着本体层长)

世界的量里她感知得到哪些,四档 `self` / `here` / `public` / `hidden`,
**没声明 = 感知不到**。反过来的错不可挽回:一个"暗中的恨意"若默认公开,角色下一句就说出来。
和本体层逐字同构 —— **声明本身就是开关**,没有 `perception.enabled`。

感知同时进**聊天 grounding** 与**定时轮次的决定上下文**。后者是关键:不接上的话,
模拟层和角色层就是两套跑在一个进程里的系统。

**截断了必须吭声**(`Perception.overflow` → "你没细看"):不说的话,她在一个"她以为只有
三棵树"的世界里做决定,而**她永远不会知道自己被骗了**。
⚠️ 有界性是**渲染器的属性,不是存储的属性** —— 本体层进来之后世界里可以有一万棵树,
它们住 Redis 还是 MySQL 一样把提示词撑爆。

### 6.4 她自己开口那一半(自主轮次,2.0 之后)

上面三层都是"有人跟她说话 / 时钟到点跑树"。第四条是**没人理她的时候**:
`autonomy.enabled` 每隔 `autonomy.interval_ticks` 问她一次要不要做点什么,
决定与执行都在世界自己那条循环上跑(第 5 节)。3.6.0 起她甚至能**约你一起做一件事**
(`agent_invites` / `invitation_settled`),而**拒绝是一等公民** ——
只给"答应"就是把同意重新变成不可拒绝的。

### 6.5 动作产生之后

```
动作 → agent_action 事件 → 投影折叠 → 记忆触发器 → (够重要?) → 记忆
                                    → 关系判定(chat)→ state_change 事件 → 三轴关系
                                    → 八卦抽样(chat / idle_social)→ memory_seed 事件
```

**注意八卦走的也是普通 memory_seed 事件** —— tick 线程只负责掷骰子,传闻本身是可
重放的历史,记忆触发管线一行没改。这是"新机制不要长出第二条写入路径"的例子。

3.1.0/3.6.0 又给这条链加了一格 `importance`:规律的 `emit` 与能力都能声明
"这件事有多值得被记住",于是**看见的人也会记住**(见证记忆)。同样是**不写 = 这一层整个缺席**。

---

## 7. 模块地图与依赖方向

依赖是**单向**的,从下往上。**2026-08-25 按 `ls anima_world/*.py` 重画**
(上一版画着 `db.py` 与 `graph.py`,两个文件今天都不存在):

```
门面        api.py(World —— 宿主唯一需要认识的类)、__main__.py(CLI)
  ↑
编排        scheduler.py(世界时钟、邮箱、tick 帧;进程内唯一的 RLock)
  ↑
子系统      brain.py agent.py bt_nodes.py actions.py        决策
            ontology.py rules.py expressions.py stocks.py   本体 / 规律 / 量
            perception.py together.py autonomy.py contact.py 感知 / 一起做事 / 主动
            narrative.py planner.py relationship_judge.py    LLM 任务
            chat_service.py chat_session.py chat_store.py    聊天(与事件核解耦)
            chat_state.py directives.py intent.py stance.py  聊天里的能力与姿态
            memory_store.py memory_triggers.py               记忆
            memory_admission.py memory_retrieval.py drift.py
            world_store.py locations.py prompt_store.py      世界数据
            config_store.py machine_config.py llm_client.py  配置 / 钥匙 / LLM
            beats.py character_card.py media.py              节拍 / 角色卡 / 图
  ↑
事件核      events.py(append-only 日志)projection.py types.py
  ↑
存储后端    redis_state.py(世界的家)mysql_state.py(无限增长的那四样)
  ↑
纯函数      needs.py economy.py gossip.py cliques.py world_time.py
```

**最底下那层是刻意的**:`needs` / `economy` / `gossip` / `cliques` /
`memory_retrieval` 不 import 任何存储层、不碰锁、不掷骰子之外做主意。它们是可以
单独推理和测试的纯数学。想把 `memory_retrieval.similarity()` 换成向量嵌入?
换掉那一个函数,打分公式不动。

**倒数第二层也是刻意的**:`world_file.py` 只管线格式、`world_package.py` 只管落库 ——
**格式模块不认识 Redis,落库模块不认识 gzip**,两边各自能被单独测。

对外有四个模块是**跨仓库契约的权威定义**(改它们的线格式 = 改契约):

| 模块 | 内容 | 谁镜像了它 |
|---|---|---|
| `world_file.py` | `.cyberworld` **线格式**(v3,gzip JSONL) | 运维台 `lib/worldPackage.js`(只读镜像) |
| `world_package.py` | 落库那一层(dump / install / import / inspect) | 同上 |
| `world_seed.py` | **作者层** schema 校验(种子这个概念没了,闸还在) | 运维台 `lib/worldSeed.js` **已删,无镜像** |
| `beats.py` | 节拍脚本严格校验 | (无镜像;创作台经 CLI 委托) |

外加一条**不在模块里的契约**:`anima-world contract --json` 的 `storage` 段是存储契约的
权威,运维台与创作台**都真读它**(不是照文档抄一份),所以那一段改一格,
运维台的 `test/contract.test.js` 会当场红 —— 镜像本该是这个样子。

---

## 8. 关键不变量

🔴 **这一节不再列清单了(2026-08-25)。**

上一版这里有九条,而其中四条讲的是**已经不存在的东西**:`world.db.key`(Fernet)、
`executescript()` 的事务、`DB_FORMAT_VERSION` 联锁、`world_seed.json` 是唯一 package data
(今天是 `demo.cyberworld`)。判据:

```console
$ git grep -c 'DB_FORMAT_VERSION\|executescript' -- anima_world/   # 一行不印,rc=1
$ python -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['tool']['setuptools']['package-data'])"
{'anima_world': ['demo.cyberworld']}
```

**一份写着四条作废不变量的"关键不变量"清单,比没有这一节更坏** ——
它长得像一份还在生效的检查表。而病根不是那四条写错了,是**这一节从一开始就是一份镜像**:
不变量的权威在 [`../CLAUDE.md`](../CLAUDE.md) 的「关键不变量」一节,那份跟着代码走、
每次定版都会被读一遍;这一份不会,所以它必然、而且安静地烂掉。

**要看不变量,读 [`../CLAUDE.md`](../CLAUDE.md)。** 这里只留三条**属于"为什么"而不属于
"是什么"**的、这份文档解释得比别处清楚的:

- **scheduler 持有进程内唯一的 RLock。** 别引入第二把 —— 两把锁就是死锁的开始。
  跨进程那把是 `RedisLock`,**在它之外,不是替代**。
- **`start` 是人的门,`run` / `World.open` 是程序的门。** `start` 会引导配置 LLM、
  给新世界换成演示速度(1 tick/秒);另外两个一概不做。别把这两条路径搅在一起。
- **坏声明一个字都不写。** 坏节拍、坏 `kinds`、坏作者层必须在**第一次写之前**当场报错
  ——不是"报个错然后照写一半"。一份写错 `kinds` 的文件从前会留下地图和规律、`kinds` 空着,
  而那次失败让这个前缀不再是空的,**重试走的已经不是创世那条路**。

---

## 9. 在 ANIMA 系统里的位置

ANIMA 是多个**互相独立**的仓库,跨仓库零 import:

```
任何 Python 宿主 ──import anima_world.api──▶ Redis 上的世界(anima:{world_id}:*)
anima-operator(运维台,Node)──CLI / .cyberworld 文件──▶ 本包
anima-studio(创作台)──子进程──▶ 本包的某个版本 ──▶ .cyberworld
```

和引擎打交道只有两种方式:**Python 宿主直接 import(pip 钉版)**,或**CLI 子进程 +
`.cyberworld` 文件**(非 Python 端、跨版本场景)。非 Python 的宿主接不进来,
这是纯库化的直接代价,也是刻意的取舍 —— 换来的是没有网络层要维护、没有鉴权要防守。

⚠️ **系统里确实有第三种协作形态,只是它不在引擎里**:网站 ↔ 运维台/世界容器之间走 HTTP。
那层壳是**运维台**包的(`deploy/world-image/server/`),引擎本身永远没有 HTTP。
上一版这里写"协作只有两种",读的人会以为整个系统里没有 HTTP —— 而他下一步就会去
世界容器上找一扇门。全系统的分工与每条契约的权威在
[`ANIMA-分工与协作.md`](../../../docs/ANIMA-分工与协作.md)。

Python 侧的对外接口是 `api.py` 的 `World` 门面(加 CLI)。它是宿主应用依赖的 API 面:
**只加不改**,破坏性变更等于跨仓库破坏。`tests/test_reference_docs.py` 是这条纪律的闸门
——加公开方法就必须写进 [REFERENCE.md](REFERENCE.md),而且形参名要对得上。

---

## 10. 架构债:已还的与还欠的

🔴 **整节是 1.0.0 的病历,有意留在原样(2026-08-25 判定)。**
下面提到的 `snapshots` 表、`_projection_lock`、SQL 的 `pow()`、`retrieve()` 的
`reinforce` 参数,说的都是 **SQLite 时代**的引擎 —— 那一层 2.0 整个退役了。
**留着不是因为它还成立,是因为删掉等于把"当初为什么这么做"也一起删了**:
这几笔债的形状(两份真相打架、把热路径写进锁里、把算术交给存储层)在 Redis 上一样会长出来。

### 已还(1.0.0 发版前清理)

**两份投影合并成一份。** 引擎曾同时持有 `scheduler._memory_projection`(每条事件都
折叠,始终正确)和 `_WorldView.projection`(开机全量重放建起来,此后只同步叙事日志和
角色位置,经济与关系一概不折叠)。后者在运行中停在开机状态 —— 从它读余额或关系会读到
旧值,开世界还要多重放一遍整个日志。现在 `_WorldView.projection` 是个 property,直接
返回 scheduler 那份;守它的 `_projection_lock` 也一并删掉,系统重新只剩一把锁。

> 已删除的 `snapshots` 表正是这笔债的产物:它把那份陈旧投影写回库、还盖上当前的
> `MAX(seq)` 戳,于是每次 close 都往库里留一份自称最新、实际停在开机状态的错账,
> 误差单调累积。而真正驱动世界的投影从不读它,加速一次也没兑现。

⚠️ **这笔债在 Redis 上又长了一次,而且更贵**:水位 `_projection_seq` 被推过没折过的事件时,
线上从维护容器写的四张角色卡,长驻世界里永远是 `card: null`。
**一个跑着的世界每 tick 都在追加事件,所以这不是竞态,是必然,而且零报错。**
今天守它的是 `tests/test_cross_process_projection.py`。

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

### 还欠的(2026-08-25 逐条现敲复核过,四条**今天仍然成立**)

**八卦只在同地传播。** 没有跨地点的路径,谣言不会自己走到别的镇子。设计如此,记在这里
是因为它常被误以为是 bug。判据:听者是"此刻站在那儿的人"(`scheduler.py` 里
`pick_gossip` 的调用点上方那段 docstring 逐字写着 `whoever is standing there`)。

**经济还没有"劳动"的一半。** `economy.daily_wage` 是小镇金库发的,金库允许无限负债。
⚠️ **但"不去上班照样领全薪"这半句 2.0 之后不成立了**:工资**按真的上过多久班发**
(tick 帧第 4 步数 `work` 动作的 tick 数,判据 `git grep -n _worked_ticks anima_world/scheduler.py`)。
还欠的是**金库那一半** —— 钱从哪儿来仍然没有约束。

**genesis stipend 无条件落盘。** 即使 `economy.enabled=False`,每个角色开局也有一条
30.0 的 `payment` 事件(`git grep -n GENESIS_STIPEND anima_world/` → `economy.py:35` 定义、
`__main__.py` 三处播,**一处都不看那个开关**)。无害(经济关掉时账本没人读),但
"经济关掉"的世界事件日志里仍然有账本事件。刻意不加开关:否则后来才点亮经济的世界会
一分钱都没有。

**货架对滞销品也补货。** `durable` 类(如速写本)没人买也每天补 3 件直到上限 20,
同时价格一路跌向 `base×0.25`。判据:`redis_state.py` 那一行
`row["quantity"] = min(MAX_STOCK, int(row["quantity"]) + RESTOCK_PER_DAY)` **对每一行都跑,
不看 `kind`**;三个数在 `economy.py` 的 `MAX_STOCK` / `RESTOCK_PER_DAY` / `floor=0.25`。
等下一轮经济迭代一起处理。
