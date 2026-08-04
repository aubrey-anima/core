# anima-world 功能与接口参考

> 本文档面向两类读者:想了解引擎能做什么的人,和要对接它的程序(宿主应用、运维台、创作台 anima-studio)。
> 契约级别的权威定义永远以代码为准(见 [README](../README.md) 的契约表);本文是可查阅的展开说明。
> 对应引擎版本:1.1.1+(db 格式 1,包格式 1)。首发已并入原 [ROADMAP](ROADMAP.md)
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
`sentiment_delta`、`r_type`、`agent_state`、`persona_update`、`location_join` 等)。
未知类型静默忽略 —— 废弃旧事件天然向后兼容。

**"谁在哪"的持续来源是 `state_change` + `kind=location_join`**(角色走进一个地点时发,
`payload.location` 是目的地);顶层的 `agent_join` 只说得出出生地。投影把它折成
`agents[who].location`,而重开世界时的名册正是读这个值 —— 于是"谁在哪"和其余状态
一样,是事件流的投影,不是内存里的第二份真相。

关系值有两条写入路径:`sentiment`(绝对赋值,只用于创世注入)和 `sentiment_delta`
(累加并 clamp 到 [-1,1],运行期一律用它)—— 保证一次闲聊不会覆盖种子设定的宿怨。

**data-plane 原则**:事件日志只放"发生了什么";地图、行为树、种子、节拍脚本是"配置",
存在表里或 JSON 文件里,不进日志。

#### 事件的 payload 字段

`events` 表每行有 `seq` / `ts` / `type` / `who` / `loc` / `payload`(JSON)。
宿主要自己搭时间线或做统计,直接读这张表即可(世界关闭后)。

⚠️ **`ts` 一列跑着两种时基**。引擎盖的是世界时钟(从 0 开始的 tick 数),而聊天子系统
给 `conversation` 盖的是**墙钟**(Unix 秒)。tick 数不可能长到那个量级,所以
`ts >= 1_000_000_000` 就是墙钟。**任何按 tick 做的算术都必须先过这道闸** —— 少了它,
一条聊天记录就能把"第几天"算成六百多万。引擎自己的两处(时钟恢复、`sim_report`)都
过闸,见 `world_time.WALL_CLOCK_FLOOR`。拆成两列属于 db 格式变更,留给下一个主版本。

下表是各类型 `payload` 里的字段:

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
| `subsystem_health` | `subsystem`, `status`, `reason`, `previous` | 子系统档位**切换**(ok↔degraded)。只在切换时发,不是每次降级都发 |
| `player_action` | `player_id`, `role`, `action`, `details` | 同样的四个字段**也**在事件顶层(实时流的既有形状,不变)。⚠️ payload 里有值这件事**只对 1.1.1 之后产生的事件成立** —— 更早的世界里这里是 `{}`,不补也不迁移,读历史时要当它可能缺席 |

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
   窗口排一串 in-character 动作。prompt 里带**此刻的处境** —— 她在哪、需求水平
   (需点亮)、钱包(需点亮)、别人这会儿在哪在忙什么。少了这块,规划的依据比世界
   实际拥有的信息少得多:一个在家的人会被排去"继续在咖啡店待着"。LLM 只能从"活世界展开的动作空间"(现存地点、在场角色)
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
- **没有 LLM 时关系仍然会动**(`DeterministicRelationshipJudge`)。Mock 给不出可解析的
  判定,而"判定失败"的后果不是变化小一点,是**关系数据一条都不产生** —— 跨不了档、
  不生 relation_shift 记忆、不长图谱边、没有八卦源。三轴关系是常开的机制,不该在默认
  状态下静默消失。所以 mock 档换上一个确定性替身:`Δ = 0.04 × (1 - |当前值|)`,
  按剩余空间衰减 —— 不掷骰子(世界要可重放)、越熟越难再进一步、渐近而永不饱和。
  它**不假装是判断**:方向恒为正,幅度只看剩余空间、不看说了什么。要真正的判断,配 key。
  `r_type` 的改写没有替身,始终保持原样 —— 好感度是数,有像样的机械替身;`r_type` 是
  作者写的自由文本,用机械标签盖掉它比让它冻住更糟。

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

低于 `0.15`(`needs.URGENT`)触发**需求带** —— 它包在作者写的行为树外面,饿了就先吃,
不管今天该开店。需求带在未点亮时是惰性的(黑板上没有需求值 → FAILURE),树的行为逐
tick 与不带它一致。

**带有迟滞**(施密特触发):触发线是 `URGENT`,收工线是 `needs.RELEASE`
(energy `0.85` / hunger `0.75` / social `0.50`)——**开始吃就吃到饱**,而不是跨回
触发线那一 tick 就回去干活。少了这条,角色会永远卡在触发线上方抖:实测 hunger 只有
两个取值、一顿饱饭没吃过,而每次抖动都发一条 `agent_action` + 一条 `narrative`,
12 世界日的事件量 **19.7×**、`narrative` **32×**(配了真 key 就是 32 倍 LLM 账单)。
加上迟滞后同一场景是 1.7×,饥饿度在 600 tick 里走出 528 个取值的锯齿。

迟滞的判据是黑板上的 `need._restoring`(scheduler 每 tick 写"当前动作在补哪几条需求"),
是派生值不是第二份状态 —— 重启即自愈。作者树里没写收工线的 `need_action` 节点行为不变。

需求是连续量,不进事件日志;`agent_needs` 表在日切与关闭时做检查点
(⚠️ 它是**检查点**不是实时值,读当前需求请用 `World.needs()`)。

### 2.7 物品与经济(`economy.enabled`,默认关)

- **账本是投影**:余额和库存**没有表**,是 `payment` / `item_transfer` /
  `item_consume` 事件折叠出来的。对账 = 重放,天生防复制品 bug。
  **玩家钱包也在账本上**:`player_topup` 落一笔 `payment`(`__town__` → `player:<id>`),
  `player_buy` 的门禁读投影。它此前只改内存、不发事件,于是同一个玩家有两个余额
  —— 内存里是「充值 − 花费」,账本里是「负的花费」,而 `World.balance()` 读后者。
- **表里只有定义与当前值**:`item_defs`(作者数据)、`shop_stock`(货架现价与库存)。
  两者都能从世界种子创世注入(初始物品、钱财、店铺存货),见 §8。
- **价格漂移**:每世界日一次的纯函数结算,卖得多涨、卖不动跌,夹在
  `[base×0.25, base×4]` 之间 —— 把咖啡买断,明天真的更贵。
- **日结**:小镇金库 `__town__` 按 `economy.daily_wage` 发工资(允许无限负债),
  货架补 3 件、上限 20。
- 角色吃饭会在本地货架买最便宜的吃食;**没货或没钱不会卡住** —— 降级成"吃随身干粮"。
- **工资按真的上过多久班发**:日切时 `wage × min(1, 上班 tick / 一个世界日)`,`payment`
  的 payload 里带 `worked_ticks`。此前是每天无条件一份 —— 整天睡觉的人和开了十小时店
  的人到手一样多,那"经济"就只是个每天加数的计数器。没上过班的人当天不发。
- **吃什么就补什么**:`item_consume` 按 `item_defs.restores` 补对应需求。那一列 schema
  里一直有、创世时也写进去,却**从来没有人读过** —— 作者写的"这碗面很顶饱"在世界里
  没有任何差别。需求没点亮时整条路惰性。

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

#### 玩家的一次对话,在世界里留下什么

`record_chat_turn` 之后依次发生(全部落进事件日志,重开世界照样在):

| 环节 | 结果 |
|---|---|
| `conversation` 事件 | 整场会话一条,带摘要与参与者 |
| → 记忆 | `kind=user_conversation`,**importance 0.8** —— 角色能有的最重要的一类 |
| → 关系判定 | 读**真实转录**,产出双向不对称的 `sentiment_delta` + 三轴,同日重复吃 0.5^(N-1) 阻尼 |
| → 跨档时 | 再生一条 `relation_shift` 记忆,并在图谱上长出 `friendship` / `rivalry` 双向边(阈值 ±0.2) |
| → 八卦 | 那条 0.8 的记忆是可传的(≥0.5),而且是八卦优先挑走的一条 —— **没见过你的人也会听说你**(需 `social.enabled`) |
| → 规划 | 这些记忆进 `planner.freetime` 的 prompt,影响角色第二天怎么安排(需配 LLM) |
| → 小团体 | 图谱边参与 `world.cliques()` 的连通分量计算 |

**玩家不是特例**:`player_id` 只是关系对的另一个端点,走的是和 NPC 完全相同的档位、
阻尼、图谱与八卦机制。

三条边界值得先知道:

- **角色会来找你,但只在你真的在场时。** 玩家仍然不在 `scheduler.agents`、不在投影、
  不在 planner 的动作空间里(那些属于居民,而玩家是**访客**)。但一个闲下来的角色,
  如果同地站着一个在场的玩家,会发一条 `agent_hail` —— 用 `World.inbox(player_id)`
  取,一天一次、按 (角色, 玩家) 去重。它挂在闲置动作上而不是 planner 的动作空间上:
  没有 key 就没有 planner,而没有 key 是默认状态。
  ⚠️ **敲门不是对话**:`agent_hail` 不产生记忆、不动关系、不开会话 —— 你还没回话,
  世界里什么也没发生。真正的对话仍然由 `World.chat` 发起,走完整那条链。这条边界是
  有意的:否则你会看到"她来找过我",转头问她却毫无印象。
- **玩家的在场是宿主说了算,而且不落库。** `World.player_move()` 写的是进程内的
  `world.players`,重启即失效 —— 这是有意的:玩家是访客,世界侧只留下他造成的**后果**
  (记忆、关系、图谱边、账本),不留他的位置。
  `World.player_leave()` 显式离场(幂等);任何一次交互都算"我还在",超过
  `world.player_ttl_seconds`(默认 15 分钟)没动静就当他走了 —— **不要求宿主维护
  心跳**,那道闸只是兜底,免得世界里留下一个永远站在咖啡店的幽灵。
  `World.who_is_present()` 是此刻真的在场的那份名单。
  调过 `player_move` 且与角色同地(且角色不在途)时,身份块告诉角色这是**面对面交谈**,
  她可以描写看见你;否则是**手机文字私聊**,并禁止她臆造你在场。
  **没调过 `player_move` 就是没告诉世界你在哪,一律按手机私聊处理** —— 引擎不猜。
  (`anima-world chat` 会替你先走到对方跟前。)
- **NPC 之间的关系需要 LLM,不是因为判定,是因为动作。** 判定有替身(见 §2.4),但
  NPC↔NPC 的判定只由带明确对象的 `chat` 动作触发,而选中 `chat_with_<某人>` 是
  planner 的决定 —— 没有 key 就没有 planner。`idle_social`(「想找个人说说话」)不指名
  道姓,所以它只传八卦、不产生关系判定。

#### 2.9.1 她自己的选择:stance / 能力 / 意图分派 / 连续输出(1.3.0,四个开关默认关)

1.2 之前的一轮聊天里,角色只有一件事可做:**把话接下去**。她没有可以选择的行动
(说"我走了"也没真走)、没有关系性意图(讨好、赌气、试探全被压平成"回话")、
听不出你这句是在和她说话还是在导演场景。这四条开关补的就是这些,彼此正交,可以
单独点亮:

| 开关 | 默认 | 她多出来的东西 | issue |
|---|---|---|---|
| `chat.stance.enabled` | 关 | 回复前显式选一个**关系性意图**,八个枚举之一 | #18 |
| `chat.tools.enabled` | 关 | 聊天里能**调能力**:静音 / 走开 / 等会儿再说 / 拒谈话题 / 广播 | #15 |
| `chat.intent.enabled` | 关 | 你的每条消息先**分类**:对话 / 导演场景 / 改对话规则 | #16 |
| `chat.loop.enabled` | 关 | 一次触发**连着说到她自己想停**(`World.chat_burst`) | #17 |

**线格式是行内标记,不是 OpenAI 的 `tools=`。** 她在回复流里写
`〔stance:provoke〕`、`〔tool:mute {"minutes": 5}〕`、`〔wait〕`,引擎当场摘出来、
散文原样流给玩家(一个字都不会漏)。理由是默认状态必须成立:没有 key 时世界跑在
Mock 上,而本地 ollama 与若干 OpenAI 兼容端点的 function calling 支持参差 —— 只在
原生 tools 上可用的能力,等于在默认状态下缺席。原生 tool-calling 是 v2 的路,声明
(`params_schema`)已经是同一份。

**stance(#18)** 是 (角色, 对方) 的属性,落在 `agent_stance` 表:她可以同时对你找茬、
对别人讨好。`World.stance(agent_id, target_id)` 读它,`declared=False` 表示"这是兜的
底,她没选" —— 文本上和"她选了中性"一模一样,只有这个字段能分开。惯性(不要一句
讨好下一句找茬)做成**提示词压力**而不是引擎摇骰子:摇骰子会覆盖角色自己的选择,
而且同一句话跑两次给两个结果、日志上看不出为什么。

**能力(#15)** 七条,声明在代码里(`anima_world/tools/`,`@tool` 登记),
`World.tools()` / `anima-world contract` 报出。v1 所有角色共用一套。

| id | 语义 | 世界里真发生的事 |
|---|---|---|
| `mute` | 屏蔽这个人一段时间 | 写 `agent_mutes`;下一条消息被拒(`AgentUnavailable`),发 `mute_started` |
| `end_conversation` | 结束对话,不屏蔽 | 关掉这场会话(照旧一个 `conversation` 事件) |
| `delay_reply` | 等会儿再说 | 排一条 `agent_followups`;**到点她真的回来敲门**(`agent_hail`,`reason=delayed_reply`) |
| `walk_away` | 话说到一半走人 | **面对面**时走 BT 那条路真的起程(`travel` / `location_join`);隔着手机时降级成**挂断**(`detail.degraded_to`)—— 对一个不在你面前的人"走开"是个空动作 |
| `refuse_topic` | 以后不谈这个 | 写 `agent_refused_topics`;对方再提时提示词里加一段"岔开话题" |
| `broadcast` | 当众说一句话 | 一条 `agent_broadcast` 事件(`World.broadcasts()`,payload 带 `heard_by`)**外加**给每个在场角色一条 `memory_seed` —— 他们真的记住了。听众是**同一个地方**的人,不是全世界 |
| `wait_for_user` | 我说完了,轮到你 | 连续输出的正常出口(#17) |

**软静音**:玩家还能发,世界当场拒 —— `World.chat` 抛 `AgentUnavailable`
(带 `kind` / `seconds_left` / `reason`),而不是回一句空话(空回复在宿主那边和
"LLM 挂了"长得一模一样)。硬静音(锁输入框)由宿主按 `mute_started` 事件自己决定。
`World.is_muted()` 可以先探一下,`World.unmute()` 是作者/运维的手动解除。

**广播为什么落到记忆上**:`agent_broadcast` 事件原来没有任何角色消费 —— 她"当众宣布"
的后果是一行日志,世界里谁也不知道,而菜单还告诉她"世界里的人都能看到"。这违反了
**她的选择必须在世界里兑现**那条硬不变量。兑现走现成的 `memory_seed` 路,不新造广播
收件箱:记忆本来就是这个引擎里"角色知道一件事"的表示。玩家侧不落记忆 —— 宿主自己读
`World.broadcasts()`,引擎不替 UI 做主。

**意图分派(#16)** 走**背景槽**(`llm.background.model`,空则用主模型)一次分类:

- `dialogue` —— 照旧,不动
- `style_adjust` —— 写 `persona_overrides`,**按 (角色, 玩家) 永久**:一次教会,跨会话
  跨天不忘("以后叫我霜霜" 的核心是"以后")。回一句轻确认,不走 in-character 生成。
  可写的 kind 是白名单:`address_form` / `description_style` / `tone_preference` /
  `forbidden_topics` / `nickname_for_player`。宿主也可以直接
  `World.set_persona_override()`,不必经过分类器。
- `narrative_direction` —— 交给 director:**v1 只对已经存在的角色动手**
  (`come_here` / `leave` / `act`),而且不进提示词、进**世界** —— 让某人过来就是一次真的
  行程,于是她下一次读 grounding 时会真的看到那个人在场。不认识的人一律拒绝并指出
  下一步(自然语言造人是 v2:那需要每日上限、作者 opt-in、`authored_by_user` 标记)。

分类**往 dialogue 上偏**:置信度低于 `chat.intent.min_confidence`(默认 0.6)、参数不
全、分类器抽风,一律退回对话,并把原因写进 `meta["intent_reason"]`。两种错的代价不
对称 —— 该 narrative 判成 dialogue 只是别扭,反过来会把玩家正说的话吞掉。

**连续输出(#17)** 是 `World.chat_burst()` / `World.achat_burst()`:产出结构化步骤
(`budget` / `intent` / `text` / `message` / `stance` / `tool_call` / `stop`),宿主可以
逐条弹出。⚠️ `text` 是 `message` 的**流式视图**(同一段话的两种形态):要打字机效果就
消费 `text` 并忽略 `message`,要整句就反过来 —— 两个都渲染会让每句话出现两遍。
四类停下信号缺一不可:显式让位(`〔wait〕`)、隐式让位(问句结尾)、预算耗尽、工具要求
结束;`chat.loop.max_messages` / `max_tool_calls` 是硬上限,兜住"模型不肯让位"。
另有两条防跑飞:一步什么也没说出来(`empty_step`)、以及**又把说过的话说了一遍**
(`repeated_step`)。后者按**句子**比对,并且覆盖宿主递进来的整段近期历史 —— 真模型的
样子不是一字不差的复读,而是"第二句把第一句换个说法",甚至第四轮里整段照抄第二轮说过
的一段;而一个绕圈的模型会一路绕到预算耗尽,玩家读到的是五条几乎一样的消息。**第一步
永远照旧交出去**:查重不许把一轮变成沉默。`stop` 那一步的 `reason` 报的就是这几个
(还有 `tool_yield` / `end_conversation` / `handled_by_intent`)。

⚠️ **等待时间**(2026-07-29 实测,LongCat-2.0):一轮 2~12 秒是常态,偶尔 20~30 秒,
极端一次 92 秒。连续输出把慢调用的暴露面乘上步数 —— 宿主那边"她还在说"的占位符不是
装饰,是必需品。要压时间就给背景槽配个便宜快模型(`llm.background.model`)。
预算按性格/关系/心情/时间算,而且**把依据一起交出来**(`reasons`) —— 一个说不出理由的
预算没法调参。性格倾向从 personality **文本**里抽(确定性关键词,没有 key 也算得出
来;LLM 抽取与运维台 slider 是 v2)。开关关着时它只跑一步,形状不变 —— 宿主不用写两套
消费代码。

⚠️ **观测量不进事件日志。** issue #18/#16 里写的是"每轮发一个 `agent_stance` /
`user_intent` 事件";这里没有那样做 —— 那等于把聊天转录搬进世界的历史,而
「聊天子系统与事件核解耦」是这个子系统存在的前提。取法是:每轮的 stance / intent /
tool_call 落在**消息行**上(`messages` 表的四个新列,运维台照样能逐轮显示 tag),
整场会话的分布随关闭时那**一个** `conversation` 事件出去
(`stances` / `intents` / `tools_used`),而工具造成的**后果**(走开、广播、静音)照旧
是世界事件。`World.chat(..., meta={})` 把一轮的观测量交给宿主,原样回传给
`record_chat_turn(..., meta=…)` 就落到行上。

**只有她真的选了的 stance 才上消息行。** 兜底的 neutral 不写(`declared=False`)——
写上去,下游的分布就成了"她 100% 中性",而真相是"她一次都没选过";这两件事在文本上
一模一样,而没有 key 的世界里全是后者。`World.stance()` 仍然记着那个兜底值,因为那是
她此刻的状态。`chat_burst` 那条路上分类结果作为一个 `intent` 步骤交出去(含 `reason`)
—— 判过了就得让宿主看得见。

#### 2.9.2 没人跟她说话时:定时轮次(`autonomy.enabled`,默认关)

#2.9.1 的能力全是**响应式**的——只在玩家先开口之后才有机会被选中。这条开关补的是
另一半:世界自己转的时候,她也能自己决定要不要做点什么。

**触发在调度器的 tick 上,不是另一个 loop。** 每隔 `autonomy.interval_ticks`
(默认 72 tick = 5 分钟/tick 时是 6 世界小时)问一次每个不在途中的角色。**时钟永不
等网络**:调度器只喊一声就立刻返回,快照(位置、活动、心情、在场的人、对他们的
关系)在锁内取,决定(问 LLM)与执行(调能力)丢到世界自己那条事件循环上跑。一个
角色的调用挂了只影响她自己,而且**不许无声**——异常经 `future.add_done_callback`
喂回 `World.autonomy_stats()`。

**能力面是分开的。** `reach_out` 只在这一轮上出现:她主动走到一个此刻**同地**在场
的玩家跟前搭话(在场判定和 #13 的 `_maybe_hail_player` 同一条规矩——不同地就不算,
不然一个在工作室的角色能"主动走过去"找一个在咖啡店的玩家)。走的还是 `agent_hail`
那条边界:**敲门不是对话**,不产生记忆、不动关系、不开会话。`mute` / `refuse_topic`
/ `broadcast` 两边都能用;`walk_away` / `end_conversation` / `delay_reply` /
`wait_for_user` 只在聊天里有意义(自主轮次里没有"对方"这个人,给了只会写出一堆
关掉空会话的动作)。她**只能挑能力,不产出散文**(`reach_out` 的 `text` 参数除外)
——没有人在听的时候一段独白没有去处。

**默认就是什么都不做,而且这是常态。** 提示词把"不做"列成第一个选项;什么都不做
**不发事件**(一条"她想了想,没做"的事件每六小时一条,只会把日志灌满而不带信息)。
`autonomy.max_per_day`(默认 2)防止话痨角色刷屏——但**"被问"不算"用掉额度"**,
只有真的选中一个能力才计数,否则安静的角色会被自己的沉默饿死配额。

⚠️ **提示词的措辞决定这一层活不活**(2026-07-30 实测)。第一版把"什么都不做"写了
三遍,结果真模型 **18 轮 0 次动作** —— 机制在,永远不触发。要紧的教训是:**别用提示词
做限流**,那是 `autonomy.max_per_day` 的活;提示词该写"什么时候值得主动"。改完
0/18 → 1/15。这个比例在默认节奏下约等于每三四天主动一次,偏稀 —— `autonomy.decide`
是热改模板,嫌少就自己调。

`World.autonomy_stats()` 返回 `{asked, acted, quiet, failed, last}`。存在的理由是
这条链最容易的坏法是**看着都对、其实一次没触发**(开关点了、时钟在走,她却一次都
没主动)——那可能是"她确实没什么想做的"(正常),也可能是 hook 没挂上、LLM 一直
失败、或者额度早就用完了。这五个数把这些情况分开,`last` 是最近一次发生了什么
(哪怕是"什么都没做")。

### 2.9.3 世界的规律(world-rules):树会长、矿会枯、修炼会涨功力

前面所有机制(needs 的衰减、economy 的价格漂移)的**规律都写死在 Python 里**,因为
"人会饿"是通用的。但"树怎么长""矿怎么再生""修炼一小时涨多少功力"**因世界而异** ——
不该由引擎替所有世界决定。这一层把规律本身变成数据,和提示词进 `prompt_templates`、
行为树进 `bt_nodes`、剧情进 `beats.json` 是同一条线上的最后一段。

**量 = (owner, key, value)**,owner 是任意字符串,**前缀即种类**:

| owner | 是什么 |
|---|---|
| `world` | 季节、天气这类全局的量 |
| `tree:oak_01` | 一棵树的 `size` / `growth_rate` / `max_size` |
| `agent:夏` | 挂在角色身上的量(功力、修为) |
| `location:cafe` | 一个地方自己的量 |

不发明新的实体系统是有意的:和账本的 holder(角色 id / `player:x` / `__town__`)完全
同构。**一个"实体"就是共用一个 owner 的一组量。**

一条规律:

```jsonc
{ "id": "tree_growth",
  "every": {"ticks": 12},                  // 多久算一次(节流)
  "for_each": {"kind": "tree"},            // 谁参与
  "when": ["world_season != 3"],           // 可选:条件,不满足整条不算
  "set": {"size": "min(size + growth_rate * dt, max_size)"},
  "emit": [{"when": "size >= max_size", "type": "tree_matured"}] }
```

**选择器**三种:`{"kind": "tree"}`(某一类的全部)、`{"owner": "world"}`(指定一个)、
`{"action": "work"}`(**此刻正在做这个动作的角色** —— 修炼、采矿、耕种都是这一类:
投入的是时间,速率由行为者自己的量决定)。

**表达式**能用的东西刻意很少:四则、比较、与或非、三元(`a if 条件 else b`),以及
`min` / `max` / `abs` / `round` / `clamp` / `floor` / `ceil`。变量先在这个 owner 自己的
量里找,再找 `world_<key>`(全局),外加恒有的 `dt` 与 `now`。
**绝不 `eval`** —— 表达式解析成 AST、逐节点过白名单,再由引擎自己的解释器求值;
属性访问、下标、lambda、推导式一律在**解析时**被拒。

六条要知道的性质:

| | |
|---|---|
| **`dt` 不漂** | `every` 只是节流,`dt` 带真实流逝。算得稀**不会让结果偏掉**,只是**滞后**最多一个 `every`(下次求值一次补回) |
| **规模(实测)** | 一万棵树跑一个世界日(24 次求值)约 1.4 秒,4.8ms 每 tick。依据是按类批量查 + 整轮一次 commit —— 早期版本逐个 owner 提交,2000 棵就到 72ms/tick |
| **新量不暴涨** | `dt` 从量自己的 `updated_tick` 算,不是世界年龄 —— 在跑了半年的世界里种一棵树,它不会一次性长成参天大树 |
| **连续变化不发事件** | 一万棵树每天 24 万次变化,逐条发事件会把日志淹掉(needs 有过 19.7 倍事件量的教训)。只有 `emit` 的门槛跨过去才发一条 |
| **门槛是边沿触发** | 算之前不满足、算之后满足才发。否则长满的树会每 12 tick 喊一次"我长成了" |
| **双缓冲** | 同一轮读到的都是这一轮**开始前**的值,规律之间与顺序无关(代价:连锁反应等下一轮)。两条规律抢同一个量会打警告 |
| **跑在 tick 线程上** | 纯算术 + SQL,没有 LLM —— 和 needs/economy 同类。(autonomy 正相反,那条要打网络) |

**加载时严格、运行期降级**,和节拍脚本同一条纪律:公式写错、选择器不认识、`every`
写反 —— 全部在世界启动前抛 `RuleError`(**整体拒绝,不逐条丢弃**:规律是这个世界的
物理法则,少一条不是少一点内容,是从此算错)。而运行期读到一个不存在的量、除零,
只跳过那一条并计进 `World.rule_stats()`,不掀翻 tick。

种子里写 `"stocks": [{"owner": …, "values": {…}}]` 与 `"rules": [...]`(创世一次,
之后 `stocks` / `world_rules` 表说了算)。API:`World.stock/stocks/set_stock/
set_stocks/stock_owners/rules/rule_stats`。

⚠️ **一条规律只写它自己那个 owner 的量。** 读可以更宽(任何表达式都能读 `world_*`
全局),**写不对称** —— 这两种写法开机就会被拒:

```jsonc
"set": {"world_总产量": "world_总产量 + 1"}   // ✗ 写不到全局量
"set": {"mine:north.储量": "储量 - 1"}       // ✗ 写不到别的实体身上
```

要改全局量,用 `"for_each": {"owner": "world"}` 的规律去写它自己的 `总产量`。

这道闸是踩出来的:两种写法此前都被**静默接受**,在那条规律自己的 owner 名下建了一个
叫 `world_总产量` / `mine:north.储量` 的怪名字,世界的量一动没动,而 `rule_stats()`
报的是 `written: 5, skipped: 0` —— 专门用来回答"这层跑通了吗"的仪表说的是成功。
`world_x` 尤其毒:**读**它是对的,于是作者理所当然假设写也对称。

**跨实体的相互作用 v1 表达不了**(挖矿让矿脉减少、收割让粮仓增加)。不悄悄放行的
理由是双缓冲下**扇入没有意义**:一条作用在一百棵树上的规律,每棵读到的全局量都是这
一轮开始前的同一个值,"每棵树 +1"的结果是 +1 而不是 +100 —— 一个看起来对、算出来
错的语义比当场报错坏得多。要它,得先设计好扇入(求和?最后一个赢?)再放开。

#### 2.9.4 认知层(perception):世界的量里,她感知得到哪些

§2.9.3 给了世界一堆客观的量,但**客观存在 ≠ 她知道**。这两层混成一层就会得到一个
**无所不知的角色**:她随口说出矿的确切储量、别人暗中的恨意、隔着半个地图那棵树的
高度。那比"她什么都不知道"糟得多 —— 不知道最坏是她没注意到(玩家看得见),而知道
太多是**当场破戏,且不可挽回**。

所以默认值定死:**没声明 = 感知不到。** 作者要哪个量被看见,显式声明它是哪一档:

| 档 | 意思 | 例子 |
|---|---|---|
| `self` | 只有主人自己知道 | 她自己的功力 |
| `here` | 得在同一个地方 | 这棵树多高(要 `stock_places` 说它在哪) |
| `public` | 人人皆知 | 季节、粮价、战争 |
| `hidden` | 谁也不知道(**默认**) | 矿的真实储量、暗中的恨意 |

声明按 `(owner 种类, 量名)` 走,`*` 通配 —— 可见性是"这类量什么性质"的属性,不是每个
实例的属性:所有树的 `树高` 不必一棵棵写。**逐个量算**:一棵树的 `树高` 可见,不代表
作者后来加的 `内部编号` 也可见。

**声明本身就是开关。** 没有 `perception.enabled` 这种配置项:一个没声明过任何可见性的
世界,这一层是空的、不进提示词、不花一个 token。要点亮就去声明,粒度天然比全局开关细。

感知同时进**两处**,这是有意的:
- **聊天的 grounding**(`chat.perception_block`,可热改)—— 她说话时知道
- **定时轮次的决定上下文**(§2.9.2)—— 她**做决定**时也知道,否则"矿富了所以我去挖"
  这种事永远不会发生

`World.perception(agent_id)` 报她此刻感知到什么(不是世界有什么)。存在的理由是可查:
可见性是声明出来的,而"我以为她知道/其实她不知道"是这一层最容易的错。

种子里写 `"stock_visibility": [{"kind": "tree", "key": "树高", "visible": "here"}]` 与
`"stock_places": [{"owner": "tree:x", "location": "cafe", "label": "门口那棵老橡树"}]`
(`label` 是给角色看的名字 —— 提示词里"这里的老橡树"比"这里的 tree:x"像人话)。
API:`World.declare_visibility` / `place_stock` / `visibility_rules` / `perception`。

⚠️ **还没接的一条**:八卦。`hidden` 档的量目前**没有任何途径**让角色知道 —— 真实的
形状应该是"有人告诉她"(接 §2.8 那条八卦链)。现在 `hidden` 就是绝对不可知。

#### 2.9.5 看一眼她收到了什么(`debug_prompt` / `anima-world prompt`)

提示词是这套系统里**最不可见、又最容易出错**的一层。1.3 开发期四个 bug 有三个在这儿
(stance 声明率 2/6、能力一次没用、定时轮次 18 轮 0 动作),而每一个的诊断都需要同一
件事:**她到底收到了什么**。当时唯一的办法是写 Python 往 `ChatService` 的私有属性上塞
一个假 LLM 去偷看 —— 而改模板的世界作者一点办法没有。

```bash
anima-world prompt --db-path w.db --agent 夏 --name 阿檀     # 摘要:块名、字数、占比、首行
anima-world prompt --db-path w.db --agent 夏 --full          # 连正文
anima-world prompt --db-path w.db --agent 夏 --json          # 给脚本
```
```python
seen = world.debug_prompt("夏", player_id="p1", display_name="阿檀", message="在吗")
seen["blocks"]        # [{"label","chars","text"}, …] 按真实顺序
seen["absent"]        # {块名: 为什么没出现} —— 照着这句话就能让它出现
seen["system"]        # 并起来的整段,和真聊天送进 LLM 的逐字相同
```

三条设计,每条都有代价换来的理由:

1. **它不撒谎。** 块来自 `ChatService.prompt_blocks` —— 和真聊天**同一个函数**。
   调试视图另写一遍拼装迟早会分叉,那时你会照着它去改一个不存在的问题。
   `tests/test_debug_prompt.py` 拿真聊天的 prompt 逐字比对盯着这一条。
2. **它解释缺席。** 少一块几乎总比多一块难查:世界照跑、她照说话,只是从来没提过那
   棵树,而你不知道该去改可见性声明、开关、还是模板。所以 `absent` 报的是**原因**,
   不是一句 "missing"。反过来,永远不可能缺席的块**不许**在这里写理由 —— 那是一段
   假装解释的死代码,比没有更坏。
3. **看,但不碰。** 不推时钟、不进 LLM、不写 `players.last_seen`,**静音中的角色也照样
   交出提示词**(而 `chat()` 这时会抛 `AgentUnavailable`)。她不理人的时候恰恰是你最想
   知道她收到了什么的时候,调试入口跟着一起拒就等于没有。

块顺序钉在 `chat_service.PROMPT_BLOCK_ORDER` 上,有测试盯着实际顺序是它的子序列 ——
**位置就是权重**(实测:stance 与能力菜单从中间移到末尾,声明率 2/6 → 5/6)。所以往
末尾加块之前先问它是"事实"还是"要照做的":末尾只有一个,抢的人多了就不值钱。认知层
就留在中间,实测她在那儿照样读得到(把 `树高 9.4` 说成"目测九米多快十米")。

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
- **主版本号 = db 格式 = 可挂载性**:让老引擎**读不了**世界文件的改动才升第一位
  (改列义、拆表、换单位)。第二位加能力,第三位纯修 bug
- `DB_FORMAT_VERSION` 联锁:挂上更新格式的 db 当场拒绝打开,**不写入任何表**
- **加法修订 `SCHEMA_REVISION`(1.3.0 起)**:纯加法的 schema 变化 —— 新表、新的可空列
  —— 不改可挂载性,跟着**次版本号**走。1.3.0 是修订 **2**(1.0.0~1.2.x 是 1)
- `anima_world.api` 的函数面**只加不改** —— 宿主应用的代码依赖它

#### 加法修订:为什么它不是"偷偷改了 db 格式"

1.3.0 加了六张表和 `messages` 的四个可空列。按原来的字面规则(schema 一变就升 db 格式,
而 db 格式一升就升主版本),这一版本该叫 2.0.0 —— 而那会把**所有 1.x 的世界作废**,
只为了一批加法。那条规则真正在保护的是"版本号能告诉你两个世界文件互不互通",而加法
不影响互通:

| 组合 | 结果 |
|---|---|
| 1.0~1.2 的世界 → 1.3 引擎 | 打开,补建新表,戳升到 2 |
| 1.3 的世界 → 1.2 引擎 | **打开,照跑** —— 新表被忽略,那几个开关的能力缺席 |
| 更高修订的世界 → 本引擎 | 打开,照跑,**并打一条警告**说明哪些能力这次运行会缺席 |

所以规则改成:主版本 = 可挂载性,加法修订跟次版本走,而**修订号只增不减**并且被写进
`db_meta.schema_revision`。它存在的唯一理由是让降级**看得见**:一个 1.3 的世界跑在 1.2
引擎上照样跑,但 stance / 静音 / 拒谈话题整套不生效 —— 那正是这个仓库最在意的
"照跑但给错东西"。`anima-world contract --json` 与 `anima-world doctor` 都报这个数,
镜像端(运维台)对齐时要一起读。

`tests/test_version_contract.py` 机器强制这一套:主版本仍等于 db 格式、硬钉窗口仍然
关着、修订号只增不减、以及**更高修订的世界必须仍然能挂**(它要是拒绝,那这个改动就
不是加法,应该去升主版本)。

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
| `world.history(*, since_seq=0, limit=1000, who=None, kind=None)` | **全量事件历史,分页**。事件形状与 `events()` 完全一致;`who` / `kind` 过滤。`broadcasts()` 就是它的一层壳 |
| `world.fast_forward(ticks, *, plan_wait_cap=None)` | 无头快进,每个世界日等在途的规划落地。和 `simulate` **共用** `Scheduler.fast_forward`,免得两条快进路径长出不同行为。⚠️ 定时轮次不在快进里跑(§2.9.2) |
| `world.report(*, ticks=None)` | 把跑出来的历史读成一份运行摘要,与 `simulate --report` **同一口径**(`report_format_version` 见 `contract`) |
| `world.paused` | 这个世界的时钟停没停(属性,不是方法) |
| `world.subscribe()` / `world.unsubscribe(q)` | 事件推送订阅(线程安全队列,批量帧 `{type:'batch', events:[…]}`) |
| `world.agent_context(agent_id, interlocutor_id)` | 有界 grounding:锁内一次快照角色的 lived state(检索 `chat.recall_k` 条记忆 + 在场 + 关系档位),只读、无 LLM、无 IO。`world.world_context(...)` 是同一个函数的别名 |

### 聊天与玩家

| 函数 | 说明 |
|---|---|
| `world.chat(agent_id, messages, *, player_id, display_name=None, role="player", meta=None)` | 代玩家聊一轮,**流式**产出文本块。messages 是宿主持有的近期对话(≤20 条,末条须 user);世界不落转录。未知角色抛 KeyError;她这会儿不理这个人抛 `AgentUnavailable`。`meta` 是可选收件盘,流耗尽后带这一轮的 stance / intent / tool_calls |
| `world.achat(...)` | `chat()` 的原生 async 版本(参数逐字相同) |
| `world.chat_reply(...)` | 同上,非流式,直接返回整段 |
| `world.chat_burst(agent_id, messages, *, player_id, display_name=None, role="player", interrupt_check=None)` | **连着说到她自己想停**(#17)。产出步骤 dict:`budget` / `text` / `message` / `stance` / `tool_call` / `stop`。`interrupt_check` 是一个 `() -> str | None` 回调,返回一句话就是玩家插话 —— 接着说还是转向**由她判**。`chat.loop.enabled` 关着时只跑一步,形状不变。`world.achat_burst(...)` 是 async 版 |
| `world.record_chat_turn(agent_id, player_id, messages, *, meta=None)` | 把完成回合(恰好 user→assistant 两条)记入世界并关闭:摘要 + 一个 conversation 事件 + 关系判定。返回会话 id。失败即异常,重试由调用方决定。`meta` 把 `chat()` 那轮的观测量落到消息行上(intent 落用户那行,stance / tool_calls 落她那行) |
| `world.conversations(agent_id)` / `world.conversation_messages(conversation_id)` | 会话列表 / 消息 |
| `world.close_conversation(conversation_id)` | 手动关会话(摘要+事件+判定) |
| `world.player_move(player_id, location)` | 玩家移动;目标必须是 `point` 地点,否则 KeyError |
| `world.player_action(player_id, action, details=None)` | 玩家动作,落一条 `player_action` 事件 |
| `world.inbox(player_id, *, since_seq=0, limit=50)` | 有谁来找过你(`agent_hail`)。`payload.reason == "delayed_reply"` 是她兑现"等会儿再说"那一条 |

### 让外面的进程做事(见 `docs/AGENT-RUNTIME.md`)

| 函数 | 说明 |
|---|---|
| `world.act(agent_id, verb, params=None, *, player_id="", surface="autonomy")` | **以某个角色的身份做一件事** —— 外面的进程改变这个世界的唯一入口。整个执行期持有世界那把唯一的锁,所以**一个动作是原子的**(world-rules 的双缓冲、三源仲裁、`events.seq` 的折叠顺序都要求它)。**在执行时校验,不在决定时**:她想了 6.5 秒,决定送达时世界早变了,所以"还在不在场""走不走得掉"由动词自己在执行那一刻查。未知动词 / 不在这个面上 / 工具失败一律返回 `ok=False` **并说明原因**(一个 agent 进程挑错动词不该让世界崩);未知角色抛 `KeyError`。结果形状与聊天里的工具调用**逐字相同**。⚠️ 它**不推进世界的时间** |
| `World.open(db_path, *, redis=None, world_id="world", …)` | 给了 `redis`,**世界的运行时状态整个搬进 Redis**,这个进程不再持有它。黑板那 20 个键(她在哪/在干嘛/饿不饿/打算做什么)、时钟、在途、当前动作、规划、需求、意图、关系图、姿态/静音/拒谈、量与规律 —— 此前全是纯内存,于是两个进程各开同一个世界文件会读到同一份历史、然后在各自内存里跑出**两个不同的世界**。`world_id` 进键名(一个 Redis 上跑十个世界是常态)。**接上一个已经在跑的世界不会把她按回原点**:搬家只填 Redis 里还没有的键(逐键 `HSETNX`),和时钟的 `setnx` 同一条道理 |
| — | **记忆投影仍在进程里,而且是有意的**:它是从事件重折出来的派生数据,存两份只会多一种不一致的坏法。别的进程记了一条 `payment`,这个进程靠 `catch_up_projection()` 补折 —— 重折廉价且必然正确 |
| — | 给了 `redis` 之后:**时钟**住进 Redis(只能有一个答案),`act()` / `intend()` 在一把**跨进程的世界锁**下执行(可重入、有 ttl、释放比对 token)。那把锁在调度器的 RLock **之外**,不是替代 —— RLock 还被 `threading.Condition` 用着 |
| `World.open(db_path, *, mysql=None, …)` | 给了 `mysql`,**她带不进上下文的那几样搬去 MySQL**:`events` / `memories` / `conversations` / `messages`。判据是**进不进得了提示词** —— Redis 装她此刻要带进提示词的东西,而 LLM 的上下文本来就有上限,两个"有上限"是同一个;进不了提示词的可以无限,要用时按 k 取回来。分对了的话**提示词不随世界变老而涨**(实测 60 世界日:后端涨 61 倍,提示词 2251→2272 字;`tests/test_bounded.py` 是闸)。⚠️ `edges` **不在这里**:它有 `UNIQUE(subject,predicate,object)` 且谓词是闭集,上界 2×N²,按世界的规模封顶 —— 实测一个三人世界跑 20 天,Redis 内存增量的九成是 events + memories(每世界日 13 KB;一千个世界跑一年 **4.6 GB 常驻**,永不回落),而黑板/地图/行为树随**世界的规模**有界。分家后同一份负载:20→40 世界日 Redis **一个字节没涨**,三十个聊天回合(60 条消息)Redis +0 KB。可以只给 `mysql` 不给 `redis`。⚠️ **传一个工厂,不要传裸连接**:`mysql=lambda: pymysql.connect(...)`(引擎自动包成每线程一条)。`pymysql` 的 threadsafety 是 1 而引擎有线程池 —— 共用一条连接会让协议帧交叉、连接当场作废,症状是 `InterfaceError (0, '')` 或 `read of closed file`,**而且不是必现**(大多数 tick 相安无事,某次在负载下才炸,报错离原因很远)。给裸连接照旧能开,但开机时会点名 |
| — | `events.seq` 的**连续性**在 MySQL 上不成立(自增在事务回滚后留空洞),Redis 版靠 `RPUSH` 返回长度是连续的。`since_seq` 分页照旧正确(它问的是"比这个大的"),但任何依赖 seq 连续的代码会悄悄错 —— 目前没有,写新代码时别引入 |
| — | **一个动作横跨两个后端,而崩溃不挑时候**。写序是 Redis 先、MySQL 后,所以中间死掉的样子是:在途状态写下了,而历史里没有这趟。伤面是**历史少一条**(从事件重折的东西从此少算一次,不会自愈),不是"她卡在路上"——在途带着到达 tick,时钟一到照样落地。`tests/test_mysql_state.py` 把这个伤面钉住了,判据变了会当场红 |
| `world.durability_warning()` | 这个世界的存储会不会在重启后忘掉它,不会就是 `None`。**Redis 主要活在内存里**,持久化是配置选项而默认 AOF 是关的 —— 忘掉的样子不是报错,是世界悄悄退回创世那一刻然后接着跑(实测:跑完一天 104 条事件,重启后 50 条)。探不到就沉默(`CONFIG GET` 可能被禁用) |
| `world.intend(agent_id, steps)` | **告诉她接下来打算做什么** —— 一串过日子的动作(`[{"verb","params"}]`),世界替她走完脚步。传 `None` / `[]` 取消。**调用即设定意图,不是执行到底**:立刻返回,之后每个 tick 由仲裁器在 [身体 → 她刚决定的 → 排班 → 空闲规划 → 兜底] 之间挑,所以饿到紧急线她会**先去吃再回来接着走**,路上被叫住也能被打断。一步真生效了队列才往前走一格 |
| `world.intent(agent_id)` | 她此刻还打算做的事(队首是下一步) |
| `world.verbs(agent_id="*", surface=None)` | 她能做什么 —— `act()` 的配套目录,逐条带 `id` / `kind` / `description` / `params` / `surfaces`。给了能力却不给目录等于没给 |

#### Redis 里到底有什么(键前缀 `anima:<world_id>:`)

一个三人世界、开关全开,实测 18 类键。**每一样都有界** —— 这不是巧合,是判据:
进得了提示词的必须有界(见上)。

| 键 | 类型 | 装什么 | 界在哪 |
|---|---|---|---|
| `agent:<角色>` × N | hash | **黑板**:她在哪 / 在干嘛 / 饿不饿 / 性格 / 打算做什么 | 每人 16–17 个键 |
| `clock` | string | 世界时钟 —— **只能有一个答案** | 1 |
| `doing` | hash | 每个人此刻的动作 | 角色数 |
| `transit` | hash | 谁在路上(起点 / 终点 / 到达 tick) | 角色数 |
| `plans` / `intent` | hash | 空闲规划的步骤 / 她自己刚决定要走的几步 | 角色数 |
| `needs` | hash | energy / hunger / social 的水位 | 角色数 |
| `bt_nodes` / `bt_actions` | hash | 行为树的结构与叶子动作 | 创世后基本不动 |
| `locations` | hash | 地图 | 地点数 |
| `item_defs` / `shop_stock` | hash | 物品定义 / 货架 | 物品数 |
| `stock:<实体>` / `stock_owners` / `stock_places` | hash·set | **世界的量**(树高 / 季节 …)+ 谁有量 / 量在哪 | 有量的实体数 |
| `prompts` | hash | 提示词模板 | 模板数(31) |
| `visibility` | hash | 感知声明 —— 哪些量她看得见 | 量的种类数 |
| `reflection` | hash | 反思水位线 | 角色数 |
| `kg` | hash | 关系边 | **2×N²**(谓词是闭集) |
| `stance` / `mutes` / `refused_topics` / `followups` / `overrides` | hash | 她对谁什么姿态 / 静音了谁 / 拒谈什么 / 到点回来敲门 / 玩家教的规则 | 角色×对方 |
| `cliques` | hash | 小团体 | 角色数 |
| `lock` | string | 跨进程的世界锁 | 1 |

⚠️ **给了 `mysql=` 之后,`events` / `memories` 这两个键不该存在**。它们曾经存在过:
搬家先整个搬进 Redis、再把这几样接到 MySQL,而第二步只换 store 对象 —— 第一步写进
Redis 的那份留在原地**冻在创世**(实测 MySQL 289 条事件,Redis 那份停在 50 条,再跑
五天还是 50)。引擎自己不读它,所以全量测试一片绿;但**只有 Redis 连接的那个进程会
读到一个什么都没发生过的世界**。现在既不搬也会清掉旧的,`tests/test_mysql_state.py`
验的是末态。


`act()` 是"现在做这一件事",`intend()` 是"接下来这几步"。区别不是语法糖:一个 LLM
驱动的进程**不该一步一次网络往返地编排走路** —— 那又贵又编得烂。**队列空的世界行为
逐字不变**(意图节点此时 FAILURE),所以这一层是纯加法。

**每个动词声明它把世界改在哪儿**(`writes`,`verbs()` 里带出来):一张表名,或
`events:<类型>`。`tests/test_verb_writes.py` 逐个动词在真世界里调一遍,比对声明的地方
到底变没变 —— CLAUDE.md 那条"**她的选择必须在世界里兑现**"从一句人得记住的话,变成
一条会红的测试。空的 `writes` 不许留,除非显式登记进 `CHANGES_NOTHING` 并写明理由。

**三个面**:`chat`(玩家在跟她说话)/ `autonomy`(没人说话,她自己决定)/
`body`(过日子的动作:走、吃、干活、睡、搭话、待着)。`body` 那批**只在 `act()` 上可用,
不进任何提示词菜单** —— 把 `walk` 摆进自主菜单会改提示词,而改提示词得接真模型验过
再说(这个仓库为此付过学费:位置就是权重)。

`body` 的动词全部委托 `Scheduler.emit_action` —— **行为树走的就是它**。于是"排班让她走"
和"她自己决定走"在世界里是同一件事:一样发 `travel` / `location_join`、一样花时间、
一样在途中不可打断。`emit_action` 返回 `False` 是"世界这会儿不接"(她在赶路、要找的人
不在这儿),照实报成 `ok=False`,不假装成功。

⚠️ `bt_actions` 不是动词表,是**已经绑好参数的调用表**(`go_to_cafe` =
`walk(location="cafe")`)。`tests/test_body_verbs.py` 有一条测试盯着:表里每个 `kind`
都必须是注册表里的一个动词 —— 行为树能做而动词表里没有,割裂就还在。

**面(surface)是硬的**:`walk_away` / `end_conversation` 这些需要"对面有个人",默认面
`autonomy`(她自己决定做点什么)上没有它们;要聊天里那批就显式传 `surface="chat"`
并给 `player_id`。

### 她自己的选择(1.3.0,见 §2.9.1)

| 函数 | 说明 |
|---|---|
| `world.tools()` | 她在聊天里能调的能力清单(id / kind / description / params_schema) |
| `world.stance(agent_id, target_id)` | 她此刻对某人的关系性意图。没聊过是 None;`declared=False` 表示"兜的底,她没选" |
| `world.stances(agent_id)` | 她对所有人的意图 |
| `world.is_muted(agent_id, player_id)` | 她这会儿理这个人吗?None = 理。带 `kind` / `seconds_left` / `reason` |
| `world.mutes(agent_id=None)` | 还没过期的静音与"等会儿再说" |
| `world.unmute(agent_id, player_id)` | 作者/运维的手动解除(角色自己不会调这个) |
| `world.refused_topics(agent_id)` | 她拒绝谈的话题 |
| `world.followups(agent_id=None)` | 还没到点的"回头找你"队列 |
| `world.persona_overrides(agent_id, player_id)` | 这个玩家教给她的对话规则 |
| `world.set_persona_override(agent_id, player_id, kind, value)` / `world.clear_persona_override(...)` | 直接写/删一条规则(宿主自己做 UI 时用,不必经过分类器)。kind 是白名单 |
| `world.broadcasts(*, since_seq=0, limit=50)` | 她公开说过的话(`agent_broadcast` 事件) |
| `world.autonomy_stats()` | 定时轮次(§2.9.2)到底跑没跑:`{asked, acted, quiet, failed, last}` |

### 世界的规律与存量(1.3.0,见 §2.9.3)

| 函数 | 说明 |
|---|---|
| `world.stock(owner, key, default=0.0)` | 读一个量 |
| `world.stocks(owner)` | 这个 owner 身上所有的量 |
| `world.set_stock(owner, key, value)` / `world.set_stocks(owner, values)` | 写(种一棵树、埋一个矿)。`updated_tick` 记的是**此刻**,所以新量不会按世界年龄暴涨 |
| `world.stock_owners(kind=None)` | 有哪些量的主人;给了 kind 只看那一类 |
| `world.rules()` | 这个世界的规律(编译过的只读视图,含每条读了哪些量) |
| `world.rule_stats()` | 规律引擎跑得怎么样:`{evaluated, written, emitted, skipped, last_error}` |
| `world.perception(agent_id)` | 她此刻**感知到**什么(不是世界有什么),分 `own`/`here`/`public` 三档(§2.9.4) |
| `world.debug_prompt(agent_id, *, player_id="p1", message="在吗", display_name=None, role="player", history=None)` | 她这一刻**会收到的提示词**,逐块带来源标签(§2.9.5)。`blocks` / `order` / `absent`(哪块没出现**以及为什么**)/ `system`(并起来的整段,和真聊天逐字相同)。**看,但不碰**:不推时钟、不进 LLM、不写玩家状态,静音中的角色也照样交出来 |
| `world.declare_visibility(owner_kind, key, visibility, label=None)` | 声明某类量的可见档:`self`/`here`/`public`/`hidden` |
| `world.place_stock(owner, location, label=None)` | 这个东西在哪(`here` 档要用) |
| `world.visibility_rules()` | 现有的可见性声明 |

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
| `world.config_list(category=None, mask=True)` | 全部配置(secret 默认打码为 `前3***后4`);每行带 `source`:`默认值` / `世界文件` / `环境变量` / `机器配置 <路径>` |
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

### 4.2.1 anima-world prompt —— 看一眼她收到了什么

```bash
anima-world prompt --db-path w.db --agent 夏 --name 阿檀     # 摘要:块名、字数、占比、首行
anima-world prompt --db-path w.db --agent 夏 --full          # 连正文一起
anima-world prompt --db-path w.db --agent 夏 --json          # 给脚本
anima-world prompt --db-path w.db                            # 不给 --agent:列名册
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--agent` | - | 看谁的;不给就列名册(和 `chat` 一个规矩:不猜她是谁) |
| `--player-id` / `--name` | `cli` / `访客` | 以谁的身份、什么称呼 |
| `--message` | `在吗` | 假设这一刻你说的是哪句话(会影响"拒谈话题"那块) |
| `--full` / `--json` | - | 连正文 / JSON |

改 `prompt_templates` 的人一般不写 Python,而"改完她到底收到了什么"过去只有写 Python
塞假 LLM 才看得见。语义与 `World.debug_prompt` 完全相同(§2.9.5):**看,但不碰** ——
不推时钟、不进 LLM、不写玩家状态,静音中的角色也照样交出来。

摘要里的占比一列值得看:它会立刻告诉你**提示词的字数花在哪儿了**。

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

还报一条**不算问题但白花钱**的:`chat.intent.enabled` / `autonomy.enabled` /
`chat.loop.enabled` 开着而 `llm.background.model` 空着时,这些便宜活会退回主模型。
意图分类每轮跑一次而且**串在回复前面**,所以玩家等的是两次生成而不是一次 —— 而她
照样回话,这条永远不会自己暴露。开关全关就不唠叨(没人会读的建议等于没有建议)。

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
| `events.total` / `events.by_type` | 总数与按细分类型计数(`state_change` 按 `payload.kind` 细分)。覆盖**全部**事件 |
| `events.wall_clock_events` | (format 2 新增)其中打的是墙钟 ts、因而没有世界时间可归属的事件数 |
| `events.by_day[]` | `day` / `total` / `buckets` —— 桶:`move` `work` `sleep` `chat` `idle` `plan` `narrative` `relation` `economy` `memory` `genesis` `other`。**一个事件只进一个桶**。**稀疏**:只列真的发生过事情的天 |
| `agents[]` | `id` / `events` / `ticks_by_activity` / `share_by_activity` / **`idle_only`** |
| `encounters[]` | `a` / `b` / `meetings`(相遇次数)/ `ticks` / `minutes` / `by_location` |
| `relationships[]` | `as` / `target` / `start` / `end` / `min` / `max` / `changes` / `turning_points` |

三条口径值得写明:

- **在途不算在场**。出发即离场,到达才算到 —— 否则"作息设计的相遇窗口兑现没有"永远
  答"兑现了"。
- **一段活动持续到下一段开始**,与引擎"动作变了才发事件"的语义一致;叙事事件与关系
  判定事件不打断活动(它们是对刚才那个动作的描写与事后结算)。
  `idle_only` = 整场只有闲逛/睡觉/赶路,没有一件"发生了什么"。
- **按天的统计只覆盖世界 tick 上的事件**(format 2 起)。`events.ts` 这一列跑着两种
  时基:引擎盖的是世界时钟(从 0 开始的 tick),聊天子系统给 `conversation` 盖的是
  墙钟。把一个 Unix 时间戳当 tick 折算成"天"会得到六百多万,所以墙钟事件不进
  `by_day`、不参与 horizon 与时间分配。它们仍计入 `total` 与 `by_type`,并在
  `wall_clock_events` 里单独点名 —— 少算比算错更隐蔽。于是等式是:

  ```
  sum(by_day[*].total) + wall_clock_events == total
  ```

  format 1 的 `by_day` 是稠密的、且不做时基区分:一条聊天记录就能把它撑成六百万行
  (放不下即 MemoryError,放得下则是 `days=6198680` 的假答案)。升到 2 修的就是这个。

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

**世界文件里只存作者动过的**(1.4.0)。创世不再播默认值,读的时候按
**环境变量 → 机器配置 → 世界文件 → 引擎默认值**解析,`source` 那一栏告诉你这一次
走到了哪层。两个后果:

- 引擎改进过的默认值,**已有的世界也吃得到** —— 此前世界文件把创世那天的默认值冻死了,
  而且无声(两个世界行为不同,`config list` 看上去一模一样)
- 表里剩下的就是**作者的意见**,一眼可见

取舍是真实的:需要在两个引擎版本上行为一致的场合,把值显式写进种子的 `config` 块 ——
那本来就是作者的意见。

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `llm.api_key` | str(secret) | 空 | LLM API key,Fernet 加密存储;空 = Mock 降级 |
| `llm.base_url` | str | 空 | OpenAI 兼容端点 |
| `llm.model` | str | `gpt-4o-mini` | 模型名 |
| `llm.timeout` | float | 30.0 | 单次调用超时(秒) |
| `llm.max_retries` | int | 2 | SDK 重试次数 |
| `llm.background.model` | str | 空 | **背景槽**的模型:意图分类器与连续输出的每一步走它(便宜快模型)。空 = 用 `llm.model`。key 与端点共用主槽的 |
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
| `chat.stance.enabled` | bool | false | 她回复前显式选一个关系性意图(§2.9.1) |
| `chat.tools.enabled` | bool | false | 她能在聊天里调能力:静音/走开/等会儿/拒谈话题/广播 |
| `chat.tools.max_mute_minutes` | float | 1440.0 | 单次静音 / 拒谈话题的上限(分钟);超出按上限执行并警告 |
| `chat.tools.max_delay_minutes` | float | 720.0 | 单次"等会儿再说"的上限(分钟) |
| `chat.intent.enabled` | bool | false | 每条玩家消息先分类:对话 / 导演场景 / 改对话规则 |
| `chat.intent.min_confidence` | float | 0.6 | 低于它一律退回按对话处理(并说明原因) |
| `chat.loop.enabled` | bool | false | `chat_burst` 连着说到她自己想停 |
| `chat.loop.max_messages` | int | 8 | 一次连续输出的消息硬上限 |
| `chat.loop.max_tool_calls` | int | 15 | 一次连续输出的工具调用硬上限 |
| `autonomy.enabled` | bool | false | 没人跟她说话时,定时问她要不要自己做点什么(§2.9.2) |
| `autonomy.interval_ticks` | int | 72 | 隔多少 tick 问一次(默认 5 分钟/tick 下是 6 世界小时) |
| `autonomy.max_per_day` | int | 2 | 一个角色一天最多主动几次(只算真的选中能力的次数,不算被问的次数) |
| `memory.capacity` | int | 50 | 每角色记忆容量(anchor 不占淘汰),超出按 strength 最弱优先 |
| `memory.sentiment_threshold` | float | 0.3 | 关系变动触发记忆的阈值 |
| `memory.half_life_days` | float | 3.0 | 检索时近因子的半衰期(世界日) |
| `memory.reflection_threshold` | float | 3.0 | 累计重要度越过它就触发一次反思 |
| `needs.enabled` | bool | false | 需求曲线(energy/hunger/social)驱动行为 |
| `economy.enabled` | bool | false | 物品、金钱、店铺与价格漂移 |
| `economy.daily_wage` | float | 20.0 | 小镇金库每日发给每个角色的工资 |
| `social.enabled` | bool | false | 八卦传播与小团体检测(三轴关系不受此开关影响,常开) |

## 7. 提示词模板

`world.prompt_list()` 可见、`prompt_set` 可改、live 生效。和配置同一条规矩(1.4.0):
**世界文件里只存作者改写过的**,`prompt_list()` 每行带 `source`(`默认值` / `世界文件`),
于是引擎改进过的措辞已有的世界也吃得到 —— 此前那 31 行里作者动过的是 **0** 行,
全是创世那天的引擎快照。清单:

`chat.system_persona`(角色人设)· `chat.memory_block` / `chat.world_memory_block` /
`chat.presence_block` / `chat.relation_block`(四个 grounding 块)· `chat.session_summary`
(会话摘要)· **`chat.response_format`**(回复格式规则:动作描写用中文全角括号、
括号内以角色名开头。**英文世界或不要动作描写的世界改这里** —— 它此前是写死在
`chat_service` 里的一段中文规则,每次聊天都注入,作者在 `prompt_list()` 里看不见,
于是永远关不掉)· `narrative.describe`(叙事)· `planner.freetime`(自由时间规划)·
`judge.relationship` / `judge.user_relationship` / `judge.relabel`(关系判定三件套)·
`world.setting`(世界观,**原样使用不做 format**,可放字面量 `{}`)。

1.3.0 起还有 **chat-agent 的七块**(只在对应开关点亮时才进提示词,见 §2.9.1):
`chat.stance_block`(关系性意图菜单 + 惯性)· `chat.tools_block`(能力菜单)·
`chat.overrides_block`(玩家教过的对话规则)· `chat.refused_topic_block`(她拒绝谈的
话题又被提起)· `chat.intent_classifier`(意图分类器)· `chat.loop_continue` /
`chat.loop_interrupt`(连续输出的续说提醒与插话)。

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
| `world.db` | SQLite(WAL):事件、聊天、记忆、图谱、配置、提示词、地图、行为树、格式戳与加法修订戳。1.3.0 起还有 stance / 静音 / 拒谈话题 / 回头找你 / 玩家教的规则五类当前值(§2.9.1) |
| `world.db.key` | Fernet 密钥,**搬迁必须随行**;丢失 = secret 永久读不出(降级 Mock,但会点名) |
| `world_seed.json` | 种子,字段见下表。**内置**种子畸形时降级到硬编码默认(不阻断启动);经 `--seed`/`seed_path` 显式指定的种子畸形则当场报错 —— 种子只读进空库一次,静默降级不可挽回 |
| `beats.json` | 可选节拍脚本(见 §9) |

**内置种子是橱窗,不是毛坯。** 它替这个世界点亮了 needs / economy / social /
stance / tools / intent / autonomy,并播了关系、创世记忆、钱、随身物品、货架与目标
—— 因为**一个展示不了自己特性的内置世界没人会用**:装上包、`anima-world start`,
第一屏就该看见这个引擎能干什么,而不是一个只会走路说话的空壳。`chat.loop.enabled`
是唯一没点亮的(它把每轮的 LLM 调用乘 2~5 倍,不该替用户做一个持续烧钱的决定)。

**引擎默认值仍然全关**(`config_store._DEFAULTS`)。两者的分工是:引擎默认值 =
"没人说话时的样子",内置种子 = "这个世界的作者的意见"。自己写种子的人从素配起步,
要什么点什么;`tests/conftest.py` 的 `bare_seed` 夹具就是把橱窗剥回毛坯的那份。

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
| `config` | **这个世界开箱点亮哪些开关**(1.3.0),见下 |
| `stocks[]` | 初始存量:`{"owner": "tree:oak_01", "values": {"size": 0.5, …}}`(§2.9.3) |
| `rules[]` | **这个世界的规律**(§2.9.3)。坏规律**整体拒绝**,不逐条丢弃 |
| `stock_visibility[]` | 哪些量角色感知得到:`{"kind","key","visible"}`(§2.9.4)。**没声明 = 感知不到** |
| `stock_places[]` | 东西在哪:`{"owner","location","label"}` —— `here` 档靠它成立 |

#### `config`:种子替它的世界做的开关决定

```jsonc
{"config": {"needs.enabled": true, "economy.enabled": true, "autonomy.interval_ticks": 48}}
```

**创世时一次,空库才认** —— 和其它创世播种同一条契约。1.4.0 之后它是 `config` 表里
**唯一**的来源:引擎默认值不再播进世界文件,所以表里剩下的就是这个世界的作者决定了
什么(见 §6 开头)。已有的世界不认:那些
开关此时是**运行数据**(作者可能早就 `config set` 改过),拿今天的种子回头覆盖它们,
等于让一次重启悄悄改掉一个跑了半年的世界的行为。

值按**声明类型**强转(和 `World.config_set` 共用同一份规则),所以 JSON 里写
`"true"` / `"48"` 这种字符串也认。三类会被跳过,而且**逐条 warning 点名**(作者
以为点亮了、实际没点亮,是这个仓库最在意的那类错):

| 跳过 | 为什么 |
|---|---|
| 这个引擎版本没有的键 | 种子会比引擎活得久 —— 一个 1.4 的种子写了 1.3 没有的开关,正确的行为是开机并少点亮一项,而不是让整个世界打不开 |
| **密文键(`llm.api_key`)** | 种子是**分发物**(`.cyberworld` 里就带着它)。能携带密钥的种子等于把作者的钥匙寄给每一个拿到这个世界的人 |
| 值转不成声明类型 | 作者把值写错了 |

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

**必填字段列是机器校验的**(`tests/test_beats_doc_contract.py` 逐字比对
`beats.OP_REQUIRED_FIELDS`)—— 加载期严格只有在"照文档写就能过"的前提下才说得通。

| op | 必填字段 | 作用 |
|---|---|---|
| `memory` | `agent_id`, `summary` | 给一个角色种记忆(可选 kind/importance/anchor) |
| `broadcast_memory` | `location`, `summary` | 给**在这个地点的所有人**种同一条记忆 |
| `sentiment_delta` | `as`, `target`, `delta` | 推关系值(累加) |
| `r_type` | `as`, `target` | 改关系描述;还需 `r_type` 与 `r_type_back` 至少给一个 |
| `persona_update` | `agent_id`, `spec` | 改人设(`spec` 必须是 object) |
| `agent_join` | `agent` | 新角色入场;`agent` 是完整 bundle(形状同种子里的 agent 条目),里面的 relations/memories/goals 同样严格校验 |
| `agent_leave` | `agent_id` | 离场(无配对 `agent_return` 只警告,不阻塞) |
| `agent_return` | `agent_id`, `location` | 返场,**必须说明回到哪里** |
| `location_desc` | `location`, `description` | 改地点描述 |
| `pay` | `from`, `to`, `amount` | 转账(可选 `reason`)。持有者可以是角色、`__town__`(金库,允许负债)或 `__world__`。`amount` 必须 > 0 —— 反向转账把 `from`/`to` 调过来 |
| `grant_item` | `agent_id`, `item_id` | 给/拿走一件东西(可选 `qty`,**负数 = 拿走**;可选 `from`,缺省 `__world__`) |

后两条是**物质层**:op 曾经只能改"她怎么想",改不了"她有什么"。作者写不出"父亲的
怀表在这一幕里丢了",只能写一条"她觉得很难过"的记忆去暗示。它们展开成账本已有的
事件类型(`payment` / `item_transfer`),余额与库存本来就是那两者的投影 —— 不新增
schema,不改 db 格式。

**谓词清单**(`trigger.when`,全部 AND;必填字段列同样是机器校验的):

| pred | 必填字段 | 读的是什么 |
|---|---|---|
| `sentiment` | `as`, `target`, `op`, `value` | 关系值(`op` 取 `gte`/`lte`) |
| `co_located` | `agents` | ≥2 个角色此刻同地。**在途不算在场** |
| `r_type` | `as`, `target`, `contains` | 关系描述里含不含某个词 |
| `need` | `agent`, `need`, `op`, `value` | 需求值(`energy`/`hunger`/`social`);`needs.enabled` 关着时恒"未满足" |
| `money` | `agent`, `op`, `value` | 账本投影里的余额 |
| `has_item` | `agent`, `item` | 随身库存(可选 `min`,缺省 1) |
| `memory` | `agent`, `contains` | 这个角色的记忆里有没有提到某件事。**纯读,不加固记忆** |

⚠️ **读不到的东西一律读作"未满足"**,不是"满足"。宁可晚触发不可错触发,而且下个
tick 还会再试。`co_located` 用的是活黑板而**不是**投影 —— 理由不是投影不追落地
(1.1.1 起它追了),是投影不知道"在途":两个正在赶路的人按投影算会成了同处一室。

**不建世界就检查**:

```bash
anima-world validate seed  world_seed.json  [--json]
anima-world validate beats beats.json --seed world_seed.json  [--json]
```

**错误退出码 2,提醒退出码 0** —— 这个区分是有意的。引用完整性(角色/地点存不存在)
只能是**提醒**:一个 beat 完全可以先 `agent_join` 一个新角色、后面的 beat 再对他做事,
那时种子里当然没有他。把它升级成拒绝,等于让设计正确的脚本在一次小版本升级之后
开不了机 —— 把"照跑但给错东西"换成"本来能跑却不让跑",后者更糟。
但沉默也不行:引用错一个 id,那个 beat 会**静默作废并被永久标记已触发**
(`beat_fired` 是历史,重启不重放),剧情就这么没了,而且不可挽回。

**校验语义**:加载期一次列出**全部**错误(id 重复、未知 op/谓词、缺字段、类型错、after
成环…),坏脚本拒绝启动;运行期单个谓词求值失败读作"未满足"(下 tick 重试),坏 op
跳过并警告,beat 无论如何标记已触发 —— 坏 op 不能楔死剧本。
