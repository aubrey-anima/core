# anima-world 功能与接口参考

> 本文档面向两类读者:想了解引擎能做什么的人,和要对接它的程序(宿主应用、运维台、创作台 anima-studio)。
> 契约级别的权威定义永远以代码为准(见 [README](../README.md) 的契约表);本文是可查阅的展开说明。
> 对应引擎版本:1.1.0(db 格式 1,包格式 1)。首发已并入原 [ROADMAP](ROADMAP.md)
> 2.0–5.0 的四大机制,详见 [2.5](#25-记忆-20)~[2.8](#28-社交八卦与小团体)。
> 想先理解"为什么是这样",读 [ARCHITECTURE.md](ARCHITECTURE.md)。

---

## 目录

1. [引擎是什么、不是什么](#1-引擎是什么不是什么)
2. [功能详解](#2-功能详解)
3. [Python API 参考(主接口)](#3-python-api-参考主接口)
4. [CLI 参考](#4-cli-参考)
5. [环境变量](#5-环境变量)
6. [配置键参考](#6-配置键参考)
7. [提示词模板](#7-提示词模板)
8. [数据文件](#8-数据文件)
9. [节拍脚本格式](#9-节拍脚本格式)

设计意图与不变量:[ARCHITECTURE.md](ARCHITECTURE.md)。

---

## 1. 引擎是什么、不是什么

**是**:一个可 pip 安装的**纯库**。一个 `world.db` 文件就是一个世界;引擎负责跑世界
(时钟、角色决策、LLM 叙事/规划/关系)、快进世界、把世界打包成可分发的 `.cyberworld`。
任何要用世界的模块 import 本包,通过 `anima_world.api.World` 的函数操作 db ——
**本质就是用函数操作数据库**,像 SQLite 那样被链接进宿主。

**不是**:
- **没有 HTTP、没有端口、没有网页**。引擎不监听任何东西;要网络暴露,宿主应用自己包一层。
- **没有创作功能**。把小说变成世界种子、编排剧情,是独立桌面程序 anima-studio 的事。
  studio 以子进程方式驱动本引擎(每个引擎版本一个隔离 venv),永不 import 本包 ——
  `tests/test_packaging.py` 机器强制这条边界。
- **不做跨版本迁移**。一个世界钉死在生成它的引擎版本上(版本即契约,详见 §2.9)。

**三条使用纪律**(权威版本在 `anima_world/api.py` docstring):

1. 一个运行中的世界**独占**它的 world.db —— 世界的真相一半在内存(时钟/投影/锁/线程池),
   第二个进程绕过 World 直写同一文件会立刻分叉;
2. **一个进程一个引擎版本** —— 多版本共存按进程隔离(studio 的 venv+子进程模式);
3. **信任边界是进程边界** —— `player_id` 只是参数,验证调用者是谁是宿主的责任。

---

## 2. 功能详解

### 2.1 事件核(event sourcing)

世界的全部历史是一条 **append-only 事件日志**(`events` 表,`seq` 自增主键即全局逻辑时钟)。
只有 INSERT,没有 UPDATE/DELETE。当前状态(角色、关系、地点、叙事日志)是把事件流 fold
出来的**投影**,开世界时从 seq=0 全量重放重建 —— 日志是唯一真相,没有第二处物化状态。

事件类型(投影器处理的全集):`agent_join` / `agent_move` / `agent_action` / `agent_idle` /
`agent_leave` / `agent_return` / `location_join` / `narrative` / `user_message` /
`capability_registered` / `state_change`(按 `payload.kind` 二级分发:`sentiment`、
`sentiment_delta`、`r_type`、`agent_state`、`persona_update` 等)。未知类型静默忽略 ——
废弃旧事件天然向后兼容。

关系值有两条写入路径:`sentiment`(绝对赋值,只用于创世注入)和 `sentiment_delta`
(累加并 clamp 到 [-1,1],运行期一律用它)—— 保证一次闲聊不会覆盖种子设定的宿怨。

**data-plane 原则**:事件日志只放"发生了什么";地图、行为树、种子、节拍脚本是"配置",
存在表里或 JSON 文件里,不进日志。

#### 事件的 payload 字段

`events` 表每行有 `seq` / `ts`(世界 tick)/ `type` / `who` / `loc` / `payload`(JSON)。
宿主要自己搭时间线或做统计,直接读这张表即可(世界关闭后)。下表是各类型 `payload`
里的字段:

| 类型 | payload 字段 | 说明 |
|---|---|---|
| `narrative` | `text`, `speaker` | 叙事文本;LLM 未配置时是模板 |
| `agent_action` | `action` | 动作名(`idle_wander` / `eat` / `go_sleep` …) |
| `state_change` | `kind`, `state`, `location` | 按 `kind` 二级分发,见上文 |
| `agent_join` | `spec`, `state`, `location` | `spec` 里有 `name` / `personality` / `goals`;创世的 `ts=0` |
| `location_join` | `id`, `name`, `description` | 创世时播下的地点 |
| `travel` | `from`, `to`, `minutes`, `arrive_at` | `arrive_at` 是到达的 tick |
| `payment` | `from`, `to`, `amount`, `reason` | 经济账本的唯一真相,余额是它的投影 |
| `item_consume` | `who`, `item_id`, `source` | |
| `memory_seed` | `agent_id`, `kind`, `summary`, `importance`, `source_ids` | `kind` 为 `hearsay*`(八卦)或 `reflection`(反思) |
| `conversation` | `agent_id`, `conversation_id`, `summary`, `message_count`, `started_at`, `closed_at`, `participants`, `location` | 整场会话只发这一条,在关闭时 |
| `capability_registered` | `id`, `kind`, `description`, `params_schema` | 首启生成的能力目录 |
| `player_action` | (空) | 字段在事件顶层:`player_id` / `role` / `action` / `details` |

**稳定性**:字段**只加不改**,与 `World` 门面同一条纪律 —— 已有类型不会删字段或改语义。
未知类型投影器静默忽略,所以将来新增类型不会让旧宿主的读取逻辑崩掉。

### 2.2 世界时间

世界时钟是**一个整数 tick**,日历(第几天、几点几分)永远现算、从不存储。

- 1 tick = `world.minutes_per_tick` 世界分钟(默认 5)→ 1 世界日 = 288 tick
- `scheduler.tick_rate` = 每现实秒走多少 tick(热更新配置)。默认 1/300,即世界与现实 1:1;
  `start` 给新世界设为 1.0(演示速度,约 5 分钟走完一个世界日)

### 2.3 角色决策(行为树 + LLM 规划)

每个角色一棵行为树,固定结构为 Selector 三层:

1. **职责(duty)**:`time_window` 节点表达"08:00–18:30 在咖啡店上班"(支持跨午夜),
   窗口内必胜。职责来自种子里每个 agent 的 `duties` 数组。
2. **自由时间计划(follow_plan)**:planner 每世界日一次、在后台线程池里请 LLM 为空闲
   窗口排一串 in-character 动作。LLM 只能从"活世界展开的动作空间"(现存地点、在场角色)
   里挑;坏步骤逐条丢弃,整个计划失败就落到第 3 层。
3. **兜底(idle_wander)**:什么都没有就闲逛。

动作种类:`walk`(有旅行时间,按地图距离 × `world.travel_minutes_per_unit` 折算)、
`work`、`sleep`、`chat`、`idle_wander`、`idle_social`。在途旅行是"已承诺"的,只有
改道去别处能打断;chat 对象不在场时不落地、下 tick 重试 —— 约束变等待,等待变相遇。

### 2.4 LLM 子系统

- **客户端注入**,OpenAI 兼容(`openai` SDK);配置(key/base_url/model/timeout)每次调用
  live 读取,`config_set` 改完下次调用即生效。**没有 key 时全线降级 Mock**:世界照跑、
  事件照发,只是文本是模板 —— 降级不许无声(见 §2.8)。
- **三个独立线程池**(各 2 worker):叙事(把动作写成人读文本)、规划(自由时间计划)、
  关系判定。**LLM 永不在 tick 线程调用**;世界事件在提交前已记录,LLM 结果回来时才
  补落地,LLM 挂了世界不停。
- **关系判定(relationship judge)**:一次聊天结束后,LLM 裁定双向**不对称**的好感度变化,
  单次上限 ±0.2;同一对角色同日多次判定按 0.5^(N-1) 阻尼;跨越关系档位
  (宿敌/交恶/淡漠/熟识/亲近/挚交)时由 LLM 用一句 ≤20 字中文短语重写关系描述。

### 2.5 记忆 2.0

**常开,没有开关。**

- **写入**:规则式触发器决定什么值得记 —— 会话摘要、关系跨档位。容量 `memory.capacity`
  (默认 50)条,`anchor` 记忆永不删。
- **检索**(`world.retrieve_memories`):三因子打分
  `score = 时近 + 重要 + 1.5×相关`。时近按 tick 年龄指数衰减(半衰期
  `memory.half_life_days`,默认 3 个世界日);相关用字符二元组重叠系数 —— 对中文友好、
  零依赖。**命中即加固**:返回的记忆 `strength += 0.3`(上限 3.0),检索就是复习。
- **遗忘**(日切自动):Ebbinghaus 曲线,强度越高忘得越慢、闲置越久掉得越多;
  `anchor` 不衰减。容量淘汰按 **strength 最弱**优先,不再是最旧 —— 一条常被想起的
  旧记忆活得过一条没人搭理的新记忆。
- **反思**(`world.reflections`):累计重要度越过 `memory.reflection_threshold`
  (默认 3.0)时,LLM 由近期记忆归纳出洞察,作为 `kind='reflection'` 的普通记忆落地。
  反思本身不累计 —— 洞察催生洞察是风暴,不是思考。
- **图谱**(跨角色共享):`edges` 表存 (subject, predicate, object) 三元组,只编码关系
  **结构**(friendship / rivalry / conversation),永不含消息内容。

### 2.6 需求系统(`needs.enabled`,默认关)

三条需求随 tick 衰减、由动作恢复,`mood` 是前三者的**木桶效应**派生值(永不存储):

| 需求 | 衰减 | 恢复 |
|---|---|---|
| `energy` | 一天不睡见底 | `sleep`(8 小时回满) |
| `hunger` | 约 18 小时饿透 | `eat`(约一小时回 0.6) |
| `social` | 一天半不社交就孤独 | `chat` / `idle_social` |

低于 `0.15` 触发**需求带** —— 它包在作者写的行为树外面,饿了就先吃,不管今天该开店。
需求带在未点亮时是惰性的(黑板上没有需求值 → FAILURE),树的行为逐 tick 与不带它一致。

需求是连续量,不进事件日志;`agent_needs` 表在日切与关闭时做检查点。

### 2.7 物品与经济(`economy.enabled`,默认关)

- **账本是投影**:余额和库存**没有表**,是 `payment` / `item_transfer` /
  `item_consume` 事件折叠出来的。对账 = 重放,天生防复制品 bug。
- **表里只有定义与当前值**:`item_defs`(作者数据)、`shop_stock`(货架现价与库存)。
  两者都能从世界种子创世注入(初始物品、钱财、店铺存货),见 §8。
- **价格漂移**:每世界日一次的纯函数结算,卖得多涨、卖不动跌,夹在
  `[base×0.25, base×4]` 之间 —— 把咖啡买断,明天真的更贵。
- **日结**:小镇金库 `__town__` 按 `economy.daily_wage` 发工资(允许无限负债),
  货架补 3 件、上限 20。
- 角色吃饭会在本地货架买最便宜的吃食;**没货或没钱不会卡住** —— 降级成"吃随身干粮"。

### 2.8 社交:八卦与小团体

**三轴关系常开**;八卦与小团体由 `social.enabled` 控制(默认关)。

- **三轴关系**:`sentiment` 仍是主轴(档位、边、改称呼都以它为准),另加
  `trust` / `affection` / `respect` 三条细轴,搭同一套增量机制。老事件没有轴字段时
  逐字节重放一致。
- **八卦**:同地社交时,说者一条重要度 ≥0.5 的记忆以 25% 概率复制给听者,
  `kind='hearsay<N>'`,每转一手重要度打折 15%,**三手之后自然消亡** —— 谣言有半衰期。
  声誉由此涌现:没见过你的人也可能"听说过你"。每对角色每世界日只掷一次骰子。
  `chat` 传给对话对象,`idle_social` 传给**同地在场的每一个人**。
- **小团体**(`world.cliques`):friendship 边的连通分量(≥2 人),日切重算的派生缓存。
  刻意不用 LLM —— 50 角色毫秒级,结果确定、可测试。

### 2.9 聊天子系统

与事件核解耦:聊天回合本身不发事件,**整场会话只在关闭时发一个 summary 事件**
(零消息会话静默关闭)。会话按 (agent, player) 键控;空闲超过 `chat.idle_timeout`
(默认 600 秒)由收割线程自动关闭(`start_clock` 时启动)。

`World.chat()` 路径**不落平台历史**:宿主每次把最近 ≤20 条对话传进来,完整转录留在
宿主库里;世界侧的 prompt 由自持状态构成(persona + 世界观 + grounding 块 + 对话身份块)。
回复流式产出,内置一个 token 级状态机给全角括号内的动作描写补角色名前缀。

`World.record_chat_turn()` 把一个完成回合记入世界并立即关闭:摘要 + 一个事件 +
关系判定(读真实转录),让玩家进入和 NPC 相同的关系机制。

### 2.10 剧情节拍(beat director)

节拍脚本是编排好的剧情,打进运行中的世界。**加载严格**(坏脚本当场列出全部错误、拒绝
启动),**触发降级**(运行时谓词失败读作"未满足",坏 op 跳过并警告,绝不让世界崩溃)。
哪些 beat 已触发是历史(`beat_fired` 事件),重启后不重放。格式详见 §9。

### 2.11 配置与密钥

配置存 `config` 表,带类型(str/int/float/bool)、分类、是否 secret。secret 用 **Fernet
加密**入库,密钥在 db 旁边的 `world.db.key` 文件(0600 权限)—— **搬迁 db 必须带上它**。
丢了 keyfile,`llm.api_key` 读不出来,世界静默降级 Mock,但三处会点名真实原因
("没配过" 与 "读不出来" 严格区分):打开世界时的启动警告、`anima-world doctor`、
`World.state()` 的 `runtime.llm.degraded_reason`。

提示词模板(约 12 个)存 `prompt_templates` 表,拼 prompt 现场 live 读取,改完即生效;
保存前用代表性变量试渲染一次,占位符错误抛 `PromptRenderError`。

### 2.12 版本即契约

一个 core 版本 = (引擎代码, db 格式版本, 包格式版本) 一起冻结:

- `anima_world.__version__` 是唯一版本源(pyproject 动态读取)
- **主版本号 = db 格式**:db 格式变才升第一位;第二、三位都是程序优化(第二位加能力,
  第三位纯修 bug)
- `DB_FORMAT_VERSION` 联锁:挂上更新格式的 db 当场拒绝打开,**不写入任何表**
- `anima_world.api` 的函数面**只加不改** —— 宿主应用的代码依赖它

#### 下一个大版本发布时,已有的世界会怎样

这套机制是为了保护**数据完整性**:读不懂的世界当场拒绝打开,而不是悄悄写坏它。它把
这件事做得很好。但它的另一面从来没有被写下来过 —— 宿主和作者在大版本边界该做什么。
先如实说清楚现状,再谈能不能改善:

| | 跨得过大版本吗 | 靠什么 |
|---|---|---|
| 作者写的种子(`world_seed.json`) | **能** | 它是版本中立的 JSON,schema 是跨仓库镜像契约 |
| `template` 包 | **大版本内自由流动**;跨大版本**不能** | 见 §4.7 的区间算法 |
| `snapshot` 包 / `world.db` | **不能** | 里面是盖了格式戳的数据库 |
| 事件历史、积累的记忆、关系状态 | **不能** | 它们只活在 `world.db` 里 |

也就是说:**世界的设定活得下来,世界经历过的事活不下来。** 一个跑了三个月、
角色之间攒出真实关系的世界,在大版本边界上只能留下它出生时的样子。

引擎标榜的正是"会积累记忆与历史的世界",而这条规则按字面读把那份积累的寿命封顶在
一个大版本内 —— 这两件事不可能都保持原样。目前**没有**官方的延续通路(没有事件日志
导出、没有从既有世界反向生成种子)。事件日志是唯一真相、其余都是派生,所以一份格式
中立的事件导出在概念上是成立的;要不要做、跨 schema 断裂时"重放"意味着什么,是一个
真实的设计问题,但"没有延续通路"应该是一个决定,而不是一次遗漏。

支持窗口(旧大版本还发不发安全补丁、发多久)见 [SECURITY.md](../SECURITY.md)。

---

## 3. Python API 参考(主接口)

```python
from anima_world.api import World
```

### 生命周期

| 函数 | 说明 |
|---|---|
| `World.open(db_path, *, seed_path=None, beats_path=None, agents=None, force_mock_llm=False)` | 打开(或创建)一个世界。空库首启从 seed 播种(缺省内置种子),并把这份种子存进 `db_meta` 当出生证明;已有库的 seed 被忽略并警告;**显式指定**的坏 seed / 坏 beats 当场抛 `WorldSeedError` / `BeatScriptError`。开机会收割上次崩溃遗留的 open 会话(补摘要、发事件) |
| `world.close(wait=True)` | 停时钟、排干 LLM 线程池。幂等;`with World.open(...) as world:` 自动调用。事件每 tick 已落盘,退出时不额外写 |
| `world.export_snapshot(output_path, *, world_id, name, seed_path=None, beats_path=None, …)` | **活体导出**:世界不停,当场打出完整 snapshot 包。先刷检查点,持锁瞬间用 SQLite backup 拷一致副本,打包在锁外;密文当场剥除。种子按 显式参数 → db_meta 出生种子 → 内置种子(记警告) 解析。返回 `WorldPackageManifest` |

交互即检查点:`record_chat_turn` / `player_action` / `player_buy` / `close_conversation`
结束时会顺手把 needs / 反思水位 / 时钟检查点刷进 db —— 玩家碰过世界的那一刻,db 就是
完整的,崩溃或活体导出都不缩水。安静挂机的损失上限仍是"上个日切以来的检查点数据"。

### 时钟

| 函数 | 说明 |
|---|---|
| `world.tick(n=1)` | 手动推进 n 个 tick,返回当前时钟(测试/自定义宿主循环用) |
| `world.start_clock(fallback_tick_rate=1.0)` | 后台线程按 `scheduler.tick_rate` 走时钟(热更新生效),并启动会话收割线程 |
| `world.stop_clock()` | 停后台时钟 |
| `world.pause()` / `world.resume()` / `world.paused` | 暂停位(时钟线程空转,不推进) |

### 读世界

| 函数 | 说明 |
|---|---|
| `world.state()` | 完整快照:agents(位置/状态/活动/在途)、world_time、locations(地图行)、relations、narrative_log、recent_events、players、simulation、runtime(db/事件/LLM 诊断,`runtime.llm.degraded_reason` 常驻) |
| `world.world_time()` | 世界日历(day/hour/minute/minute_of_day) |
| `world.memories(agent_id)` | 某角色的全部记忆行(按存储序) |
| `world.retrieve_memories(agent_id, query=None, k=5)` | 三因子检索(时近×重要×相关),返回最相关的 k 条。**命中即加固**遗忘曲线 —— 这个「读」接口会写库,是设计不是副作用。底层 `MemoryStore.retrieve(..., reinforce=False)` 可走纯读路径(调试 / 只读视图) |
| `world.reflections(agent_id)` | 该角色的反思(由记忆归纳出的洞察) |
| `world.needs(agent_id)` | 当前需求 `{energy, hunger, social, mood}`;未点亮或首 tick 前返回 `{}` |
| `world.graph(agent_id=None)` | 关系图谱三元组 |
| `world.cliques()` | 小团体(friendship 连通分量,日切重算) |
| `world.events(since_seq=None)` | 近期事件缓冲(全量历史离线读 `events` 表) |
| `world.subscribe()` / `world.unsubscribe(q)` | 事件推送订阅(线程安全队列,批量帧 `{type:'batch', events:[…]}`) |
| `world.agent_context(agent_id, interlocutor_id)` | 有界 grounding:锁内一次快照角色的 lived state(检索 `chat.recall_k` 条记忆 + 在场 + 关系档位),只读、无 LLM、无 IO。`world.world_context(...)` 是同一个函数的别名 |

### 聊天与玩家

| 函数 | 说明 |
|---|---|
| `world.chat(agent_id, messages, *, player_id, display_name=None, role="player")` | 代玩家聊一轮,**流式**产出文本块。messages 是宿主持有的近期对话(≤20 条,末条须 user);世界不落转录。未知角色抛 KeyError |
| `world.chat_reply(...)` | 同上,非流式,直接返回整段 |
| `world.record_chat_turn(agent_id, player_id, messages)` | 把完成回合(恰好 user→assistant 两条)记入世界并关闭:摘要 + 一个 conversation 事件 + 关系判定。返回会话 id。失败即异常,重试由调用方决定 |
| `world.conversations(agent_id)` / `world.conversation_messages(id)` | 会话列表 / 消息 |
| `world.close_conversation(id)` | 手动关会话(摘要+事件+判定) |
| `world.player_move(player_id, location)` | 玩家移动;目标必须是 `point` 地点,否则 KeyError |
| `world.player_action(player_id, action, details=None)` | 玩家动作,落一条 `player_action` 事件 |

### 经济(`economy.enabled` 点亮后才有意义)

| 函数 | 说明 |
|---|---|
| `world.balance(holder)` | 余额(事件账本的投影)。`holder` 可以是角色 id、`player:<id>`、`__town__` |
| `world.inventory(holder)` | 随身库存 `{item_id: qty}` |
| `world.shop(location_id)` | 某地货架:物品、名称、类别、现价、库存 |
| `world.player_topup(player_id, amount)` | 给玩家钱包充值,返回新余额。`amount ≤ 0` 抛 ValueError。**钱包是在场状态,重启即清** |
| `world.player_buy(player_id, location_id, item_id)` | 玩家买货:钱包扣款、货架减一,落 `payment` + `item_transfer`。没货 KeyError,钱不够 ValueError |

### 配置与提示词

| 函数 | 说明 |
|---|---|
| `world.config_list(category=None, mask=True)` | 全部配置(secret 默认打码为 `前3***后4`) |
| `world.config_get(key, default=None)` / `world.config_set(key, value)` | 读/写;写按声明类型强转、立即生效;未知键 KeyError,secret 空值 / 非法 tick_rate 抛 ValueError |
| `world.prompt_list()` / `world.prompt_set(name, template)` | 提示词模板;保存前试渲染,占位符错误抛 `PromptRenderError` |

### 持久化与底层

| 函数 | 说明 |
|---|---|
| `world.scheduler` / `world.chat_store` | 底层对象,进阶用;绕过它们直写 db 违反纪律 1 |

打包与校验是模块级函数(也是 CLI 的底座):
`anima_world.world_package.export_world_package / import_world_package / inspect_world_package`、
`anima_world.beats.BeatScript.load`(严格校验)、`anima_world.world_seed.is_valid_world_seed`。

---

## 4. CLI 参考

命令分两拨:**给人打的**(`start` / `config` / `doctor`)和**给部署/脚本打的**
(`run` / `simulate` / `world`)。裸 `anima-world` 打印欢迎页指路 `start`。
创作台 anima-studio 只通过这些子进程命令驱动引擎。

### 4.1 anima-world start —— 人的门

引导配 LLM(真调一次验证连通;直接回车 = 先用 Mock)→ 建世界(新世界用演示速度
1 tick/秒)→ **前台运行**,叙事逐行打印,Ctrl-C 停止。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--db-path` | `saves/world.db` | 世界文件位置,不存在就新建 |
| `--seed` | 内置种子 | 世界种子 JSON,**只对新建世界生效** |
| `--beats` | 无 | 节拍脚本 JSON |
| `--no-input` | - | 不交互提问(CI / 脚本) |
| `--real-time` | - | 新世界也用真实时间,不用演示速度 |

### 4.2 anima-world chat —— 和一个角色说话

```bash
anima-world chat --db-path saves/world.db                  # 不给 --agent:列出这个世界住着谁
anima-world chat --db-path saves/world.db --agent 夏 --name 阿檀
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--db-path` | `saves/world.db` | 世界文件 |
| `--agent` | 无 | 找谁说话;省略或写错都会列出名册(写错时退出码 2) |
| `--player-id` | `cli` | 你的身份 id —— **角色对你的印象记在它头上**,换 id 就是换个人 |
| `--name` | `访客` | 你在角色眼里的称呼 |
| `--list` | - | 只列名册就退出 |

空行或 Ctrl-D / Ctrl-C 结束。每说完一轮就落进世界(会话关闭、摘要、关系判定),
所以**说完一句话那一刻 db 就是完整的**。

**时钟不走**:对话发生在世界的此刻,退出时世界还停在原地。要一边活一边聊,那是
宿主应用的事(`World.open` + `start_clock` + `World.chat`)—— 一个 CLI 不该趁你
打字偷偷推进别人的世界。转录留在这个进程里,每轮只把最近 20 条传进世界。

### 4.3 anima-world run —— 无引导的前台宿主

不引导、不改时钟,打开世界让时钟跑,Ctrl-C 停。给部署和脚本;程序里嵌入请直接用
`World.open`。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--db-path` | `saves/world.db` | 世界文件 |
| `--seed` / `--beats` / `--agents` | - | 同 start;坏 seed / 坏 beats 拒绝启动(退出码 2) |
| `--quiet` | - | 不回显叙事事件 |

### 4.4 anima-world config

```bash
anima-world config list [--category llm]     # 密钥自动打码,未设置显示"(未设置)"
anima-world config get llm.model
anima-world config set llm.api_key sk-…      # 按声明类型强转后写入,立即生效
```

`set` 未知键返回退出码 2。

### 4.5 anima-world doctor

检查:世界文件、`world.db.key` 是否在(不在则警告旧密钥永久读不出)、db 格式版本、
事件/角色计数、LLM 四态(没建库/读不出来/没配/正常)+ **真调一次 LLM**(`--skip-probe`
跳过)、时钟快慢翻译成人话。有问题退出码 1。

### 4.6 anima-world simulate —— 无头快进

| 参数 | 说明 |
|---|---|
| `--db-path`(必填) | 世界文件 |
| `--days N` / `--ticks N` | 二选一必填 |
| `--llm full\|planner\|mock` | 三档:全真 / 真规划+Mock 叙事(长跑推荐)/ 全 Mock |
| `--no-llm` | `--llm mock` 的别名,同时给时它赢 |
| `--plan-wait-cap` | 每世界日等待在途计划的秒数上限(默认 2×planner.timeout) |
| `--report PATH` | 跑完写一份运行摘要 JSON(`-` = 写到 stdout) |
| `--seed` / `--beats` / `--agents` | 同 run |

非 mock 档会**先预检 LLM 再建世界**(坏 key 不会把降级能力目录种进新库)。内置"计划
等待预算":连续两个世界日等待耗尽则判定 planner 死亡、不再等待,绝不挂起。

`--ticks 0` = 建库但不跑,这是唯一的无头建世界口径(没有 init/create 子命令)。

#### 运行摘要(`--report`)

这份数据全在事件日志里,但 **db 格式与事件 schema 是引擎的私有契约** —— 消费方伸手
进 `world.db` 去数会把自己钉死在某个 db format 上。所以口径归引擎定,消费方按需读取。
`anima_world.sim_report.build_run_report()` 是同一份逻辑的库入口(纯函数,可离线对
任何 `world.db` 重算)。

顶层带 `report_format_version`(**与引擎版本分开**:口径变了不该逼消费方升引擎)、
`engine_version`、`db_format_version`。

| 字段 | 内容 |
|---|---|
| `world` | `ticks` / `days` / `minutes_per_tick` / `ticks_per_day` / `agents`(名册取自 `agent_join`) |
| `events.total` / `events.by_type` | 总数与按细分类型计数(`state_change` 按 `payload.kind` 细分) |
| `events.by_day[]` | `day` / `total` / `buckets` —— 桶:`move` `work` `sleep` `chat` `idle` `plan` `narrative` `relation` `economy` `memory` `genesis` `other`。**一个事件只进一个桶,桶的并集恒等于 total** |
| `agents[]` | `id` / `events` / `ticks_by_activity` / `share_by_activity` / **`idle_only`** |
| `encounters[]` | `a` / `b` / `meetings`(相遇次数)/ `ticks` / `minutes` / `by_location` |
| `relationships[]` | `as` / `target` / `start` / `end` / `min` / `max` / `changes` / `turning_points` |

两条口径值得写明:

- **在途不算在场**。出发即离场,到达才算到 —— 否则"作息设计的相遇窗口兑现没有"永远
  答"兑现了"。
- **一段活动持续到下一段开始**,与引擎"动作变了才发事件"的语义一致;叙事事件与关系
  判定事件不打断活动(它们是对刚才那个动作的描写与事后结算)。
  `idle_only` = 整场只有闲逛/睡觉/赶路,没有一件"发生了什么"。

### 4.7 anima-world world export / import / inspect —— 打包

```bash
anima-world world export --seed seed.json --output my.cyberworld \
    --world-id my-world --name "我的世界" --mode template          # 模板包
anima-world world export --seed seed.json --db-path saves/world.db \
    [--beats beats.json] --output my.cyberworld \
    --world-id my-world --name "我的世界" --mode snapshot          # 快照包(secret 剥除)
anima-world world import my.cyberworld --destination ./instances
anima-world world inspect my.cyberworld [--json]                  # 它需要什么引擎?
```

成功时 stdout 输出一行 JSON(`export` / `import`)或一份清单(`inspect`,`--json` 给
一行 JSON);失败退出码 2。`--world-id` 必须匹配 `^[a-z0-9][a-z0-9._-]{0,63}$`。

#### 各命令的 JSON 字段集

第三方工具按这几组字段编码,所以它们是**线格式**,与 `.cyberworld` 本身同一条纪律。

| 命令 | 字段 |
|---|---|
| `export` | `operation` / `world_id` / `revision_id` / `mode` |
| `import` | `operation` / `world_id` / `instance_id` / `path` |
| `inspect --json` | manifest 全字段(`world_id` / `name` / `summary` / `genre` / `setting` / `theme` / `export_mode` / `revision_id` / `created_at` / `files` / `source_engine_version` / `package_format_version` / `engine_min` / `engine_max_exclusive`)+ `current_engine_version` / `runnable` / `operation` |

#### `inspect` 跑不了的包也要回答

**读封皮不需要先能跑它**。`inspect` 只做与版本无关的校验(归档安全、`checksums.json`
与归档一一对应、`manifest.json` 的摘要对得上、manifest 结构合法),然后把兼容性作为
**数据**给出:`runnable: false` + 退出码 **0**。

这条是格式存在的意义:`.cyberworld` 就是用来在引擎不匹配的机器之间搬运的,拒绝回答
"你需要什么"给最需要问的那个调用方,方向是反的。只有 `package_format_version` 允许
硬拒解析 —— 那是封皮自己的版本。归档读不了(不是 ZIP、校验和不符)照旧退出码 2。

拒收时 stderr 按类别给一句人话,四类各说各的:校验和不符(包坏了,重传没用,重新
导出)/ 引擎区间不匹配(换匹配的 core 重导)/ 种子不合 schema(逐条点名哪个 agent
缺哪个键)/ 归档防护触发。

#### 引擎兼容区间怎么算

导出时盖章,`[engine_min, engine_max_exclusive)`:

| | `engine_min` | `engine_max_exclusive` |
|---|---|---|
| `snapshot` | **导出它的那个引擎版本** | 下一个大版本 |
| `template` | **当前大版本的地板**(`{major}.0.0`) | 下一个大版本 |

差别的理由:snapshot 带着盖了格式戳的 `world.db`,老引擎没有理由能打开它;template
只装 `world_seed.json` —— 版本中立的作者数据,其 schema 本来就是跨仓库镜像契约,
为的正是能travel。两者盖同一个章,代价就从"存档带不走"(已决定、已写进文档的取舍)
变成"作品带不走"(没有人决定过)。

---

## 5. 环境变量

| 变量 | 用途 |
|---|---|
| `ANIMA_SETTINGS_KEY` | Fernet 密钥(优先于 `world.db.key` 文件) |
| `ANIMA_LLM_API_KEY` / `OPENAI_API_KEY` / `LONGCAT_API_KEY` | 仅首启播种 `llm.api_key` 时读取 |
| `ANIMA_LLM_BASE_URL` / `OPENAI_BASE_URL` | 仅首启播种 `llm.base_url` |
| `ANIMA_LLM_MODEL` / `OPENAI_MODEL` | 仅首启播种 `llm.model` |
| `NO_COLOR` | 关闭 CLI 彩色输出 |

只设 `LONGCAT_API_KEY` 时自动播种 LongCat 端点与模型。**首启之后 `llm.*` 一律以 db
配置为准**,环境变量不再被读。

## 6. 配置键参考

`anima-world config list` / `world.config_list()` 可见,全部支持热更新。

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `llm.api_key` | str(secret) | 空 | LLM API key,Fernet 加密存储;空 = Mock 降级 |
| `llm.base_url` | str | 空 | OpenAI 兼容端点 |
| `llm.model` | str | `gpt-4o-mini` | 模型名 |
| `llm.timeout` | float | 30.0 | 单次调用超时(秒) |
| `llm.max_retries` | int | 2 | SDK 重试次数 |
| `scheduler.tick_rate` | float | 1/300 | 每现实秒的 tick 数;1/300 = 与现实 1:1,1.0 = 演示速度 |
| `agent.idle_timeout` | float | 30.0 | 行为树 idle 看门狗阈值(秒) |
| `world.minutes_per_tick` | int | 5 | 一 tick 代表的世界分钟(5 → 一天 288 tick) |
| `world.travel_minutes_per_unit` | int | 60 | 步行横穿一个画布单位的世界分钟 |
| `planner.enabled` | bool | true | 是否用 LLM 规划自由时间 |
| `planner.timeout` | float | 30.0 | 规划调用超时 |
| `judge.timeout` | float | 30.0 | 关系判定调用超时 |
| `chat.idle_timeout` | int | 600 | 会话闲置自动关闭(秒) |
| `chat.recall_k` | int | 3 | 拼进 prompt 的历史会话摘要条数 |
| `chat.recall_n` | int | 10 | 拼进 prompt 的当前会话轮数 |
| `memory.capacity` | int | 50 | 每角色记忆容量(anchor 不占淘汰),超出按 strength 最弱优先 |
| `memory.sentiment_threshold` | float | 0.3 | 关系变动触发记忆的阈值 |
| `memory.half_life_days` | float | 3.0 | 检索时近因子的半衰期(世界日) |
| `memory.reflection_threshold` | float | 3.0 | 累计重要度越过它就触发一次反思 |
| `needs.enabled` | bool | false | 需求曲线(energy/hunger/social)驱动行为 |
| `economy.enabled` | bool | false | 物品、金钱、店铺与价格漂移 |
| `economy.daily_wage` | float | 20.0 | 小镇金库每日发给每个角色的工资 |
| `social.enabled` | bool | false | 八卦传播与小团体检测(三轴关系不受此开关影响,常开) |

## 7. 提示词模板

`world.prompt_list()` 可见、`prompt_set` 可改、live 生效。清单:

`chat.system_persona`(角色人设)· `chat.memory_block` / `chat.world_memory_block` /
`chat.presence_block` / `chat.relation_block`(四个 grounding 块)· `chat.session_summary`
(会话摘要)· `narrative.describe`(叙事)· `planner.freetime`(自由时间规划)·
`judge.relationship` / `judge.user_relationship` / `judge.relabel`(关系判定三件套)·
`world.setting`(世界观,**原样使用不做 format**,可放字面量 `{}`)。

另有 **Mock 叙事模板**:`narrative.mock.<动作种类>`(`walk` / `chat` / `work` / `sleep` /
`eat` / `idle_wander` / `idle_social` / `custom`,占位符 `{agent}` `{location}` `{target}`)
与 `narrative.mock_memory_suffix`(占位符 `{summary}`)。

没有配 key 是**默认状态**,所以这些是新用户看到的第一屏,而引擎无从知道自己在跑哪个
世界 —— 那是种子决定的。因此模板跟着世界走:种子的 `mock_narration` 首启写入,之后
`prompt_set` 热改即生效。引擎没听说过的动作种类(节拍脚本里的自定义动作)也可以写
自己的模板。保存时会按调用点真正传的变量做渲染检查,占位符写错当场拒绝。

## 8. 数据文件

**一个世界 = 一个卷**,包含:

| 文件 | 说明 |
|---|---|
| `world.db` | SQLite(WAL):事件、聊天、记忆、图谱、配置、提示词、地图、行为树、格式戳 |
| `world.db.key` | Fernet 密钥,**搬迁必须随行**;丢失 = secret 永久读不出(降级 Mock,但会点名) |
| `world_seed.json` | 种子,字段见下表。**内置**种子畸形时降级到硬编码默认(不阻断启动);经 `--seed`/`seed_path` 显式指定的种子畸形则当场报错 —— 种子只读进空库一次,静默降级不可挽回 |
| `beats.json` | 可选节拍脚本(见 §9) |

**种子字段**。只有 `agents` 与 `locations` 的必填键进**校验**(那是运维台
`lib/worldSeed.js` 镜像的最小契约);其余全是可选,遵守同一条宽容原则:
**缺字段 = 今天的行为,坏条目逐条丢弃、绝不拦住启动**。

| 字段 | 内容 |
|---|---|
| `agents[]` | **必填** `id` / `name` / `location` / `personality`;可选 `duties`(职责窗口)、`goals`、`money`、`inventory` |
| `locations[]` | **必填** `id` / `name` / `description`;可选 `kind` / `parent` / `x` / `y` / `w` / `h`(嵌套邻接树,region 带 x/y/w/h、point 带 x/y,相对父区域 0~1)、`stock` |
| `relations[]` | `a` / `b` / `sentiment` / `r_type` / `r_type_back`,双向播种 |
| `memories[]` | 创世记忆 |
| `world_setting` | 世界观,首启写进 `world.setting` 提示词 |
| `items[]` | 物品定义:`id` / `name` / `kind`(`consumable`/`durable`/`artwork`)/ `base_price` / `restores` |
| `mock_narration` | Mock 叙事模板,键是动作种类(外加 `memory_suffix`),见 §7 |

**物质层的创世入口**(经济与需求从首发就有机制,过去却没有创世入口):

```jsonc
{
  "items": [ {"id": "coal", "name": "煤", "kind": "consumable", "base_price": 3.0} ],
  "agents": [ {
    "id": "夏",
    "money": 120,                                              // 覆写创世安家费(默认 30;写 0 = 一分没有)
    "inventory": [ {"item": "父亲的怀表", "note": "从不离身"} ] // qty 默认 1
  } ],
  "locations": [ {"id": "cafe", "stock": [ {"item": "coal", "qty": 20, "price": 3.0} ]} ]
}
```

- **引用即存在**:只被引用、没在 `items` 里定义的 id 自动补一条定义(名字就是 id、
  `durable`、0 价),所以 `{"item": "父亲的怀表"}` 直接可用;要精确控制名称/种类/价格
  再写 `items`。
- **随身物品与钱是事件**(`item_transfer` / `payment`),不是表 —— 账本仍然是事件的
  投影,对账即重放。`note` 原样落在事件载荷里跟着世界走,但**不会自动变成一条记忆**;
  想让角色记得这件事,`memories` 才是那个入口。
- **种子一碰物质层,内置演示物品就整体让位**(空表才种的规矩)。一个自带怀表和过冬煤
  的世界不该再被塞进三份演示咖啡 —— 半真半假的货架比空货架更难查。

**`.cyberworld` 包** = 受严格约束的 ZIP:

```
manifest.json      # 格式版本、world_id、revision_id、mode、引擎兼容区间、名称/简介/题材、文件清单
checksums.json     # sha256 逐文件校验(algorithm 必须 sha256,清单必须与归档一致)
world_seed.json    # 必有
world.db           # 仅 snapshot 模式;导出时经 sqlite3.backup 一致性快照并剥除全部 secret 行
beats.json         # 可选
assets/…           # 可选,扩展名白名单
```

安全约束:压缩后 ≤256MB、解压 ≤512MB、≤128 个文件、拒绝 zip 炸弹/符号链接/路径穿越/
加密成员/重复名。导出是**确定性 ZIP**(固定时间戳/权限/排序,同输入同字节),先自检再
原子落地;导入解到暂存、校验通过后原子替换,并写 `instance.json`(instance_id 等)。

## 9. 节拍脚本格式

```jsonc
{
  "beats": [
    {
      "id": "第一夜",             // 必填,全脚本唯一
      "once": true,              // 可选,v1 只支持 true
      "trigger": {               // at / after / when 至少一项,同时给则 AND
        "at": { "day": 0, "minute_of_day": 1200 },   // "不早于"语义
        "after": "某个前置节拍的id",                   // 不许自引用/成环
        "when": [
          { "pred": "sentiment", "as": "夏", "target": "遥", "op": "gte", "value": 0.2 },
          { "pred": "co_located", "agents": ["夏", "遥"] }
        ]
      },
      "payload": [ /* 非空 op 列表 */ ]
    }
  ]
}
```

**Op 清单**(载荷动作):

| op | 必填字段 | 作用 |
|---|---|---|
| `memory` | agent, summary | 给一个角色种记忆(可选 kind/importance/anchor) |
| `broadcast_memory` | agents, summary | 给多个角色种同一记忆 |
| `sentiment_delta` | as, target, delta | 推关系值(累加) |
| `r_type` | as, target, r_type | 改关系描述 |
| `persona_update` | agent, spec(object) | 改人设 |
| `agent_join` | agent(完整 bundle) | 新角色入场(bundle 里的 relations/memories/goals 同样严格校验) |
| `agent_leave` / `agent_return` | agent | 离场/返场(leave 无配对 return 只警告不阻塞) |
| `location_desc` | location, description | 改地点描述 |

**校验语义**:加载期一次列出**全部**错误(id 重复、未知 op/谓词、缺字段、类型错、after
成环…),坏脚本拒绝启动;运行期单个谓词求值失败读作"未满足"(下 tick 重试),坏 op
跳过并警告,beat 无论如何标记已触发 —— 坏 op 不能楔死剧本。
