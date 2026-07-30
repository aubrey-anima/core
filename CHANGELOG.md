# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Versioning is **not** plain semver: the major version *is* the db format version — which
is to say, the major version is **mountability**. A world file is pinned to the engine
version that produced it, and there is no cross-version migration — hosts install one
version and depend on that version.

- **Major** — the database format changed: existing worlds are not readable at all.
- **Minor** — new capability, same database format. Since 1.3.0 this includes
  **additive schema revisions** (new tables, new nullable columns): both directions still
  mount, the older engine simply doesn't read the new tables, so those capabilities are
  absent. `db_meta.schema_revision` records which revision wrote a file — that stamp is
  the only thing that makes such a downgrade visible instead of silent.
- **Patch** — fixes, same database format and schema revision.

`tests/test_version_contract.py` enforces this mechanically (major == db format, the hard
pin window stays closed, the revision only ever moves forward, and a higher-revision world
must still mount — if it refused, the change wasn't additive and belongs in a major), and
`db.py` enforces it again at runtime: mounting an incompatible world file is refused on the
spot rather than silently written to.

## [Unreleased]

## [1.3.0] — 2026-07-30

同 db 格式(**1**)与包格式(**1**),**schema 加法修订 1 → 3**。1.0.x / 1.1.x / 1.2.x
建的世界照常打开;这一版建的世界在 1.2 引擎上**也照样能开**,只是下面这些能力缺席。
(修订在这一版内部走过两步:2 = chat-agent 的六张表,3 = world-rules 的
`stocks` / `world_rules` 与认知层的 `stock_visibility` / `stock_places`。一个版本号
对应两个修订,只因为 1.3.0 从没发布过 —— 别当先例。第二批那次**加了表却忘了升戳**,
而当时四百多项测试一条都没红;现在 `SCHEMA_TABLES_AT_REVISION_3` 把表集合钉住了。)
主题:**让她自己做选择** —— 一次真人 dogfooding(200+ 条聊天)之后,#15/#16/#17/#18
四条一起兑现。四个开关**全部默认关闭**:每开一条都多一层"她自己的决定",也多一次
LLM 往返,所以点亮与否是世界作者的决定。

⚠️ 一处规则变更:**加法修订不再逼一次主版本跳跃**(见文件开头)。schema 一变就升
db 格式、db 格式一升就升主版本 —— 按字面读,这一版该叫 2.0.0,而那会把所有 1.x 的
世界作废,只为了一批加法。那条规则真正在保护的是"版本号能告诉你两个世界文件互不
互通",加法不影响互通,所以规则收紧到它保护的那件事上:**主版本 = 可挂载性**,加法
修订跟着次版本走并写进 `db_meta.schema_revision`(只增不减)。`contract` 与 `doctor`
都报这个数,运维台镜像要一起读 —— 一个 1.3 的世界跑在 1.2 引擎上照样跑,但 stance /
静音 / 拒谈话题整套不生效,而"照跑但给错东西"是这个仓库最在意的那类坏。

### Added

- **认知层(perception):世界的量里,她感知得到哪些。** world-rules 给了世界一堆客观
  的量,但接上角色时有个陷阱:**把量整个倒进提示词就是"无所不知的角色"** —— 她会随口
  说出矿的确切储量、别人暗中的恨意、隔着半个地图那棵树的高度。那比"她什么都不知道"
  糟得多:不知道最坏是她没注意到(玩家看得见),知道太多是**当场破戏且不可挽回**。
  所以默认值定死:**没声明 = 感知不到**。四档 `self` / `here` / `public` / `hidden`,
  按 `(owner 种类, 量名)` 声明、`*` 通配、**逐个量算**(一棵树的树高可见,不代表作者
  后来加的"内部编号"也可见)。**声明本身就是开关** —— 没有 `perception.enabled` 这种
  配置项,没声明过的世界这一层不进提示词、不花一个 token。
  感知同时进两处:聊天的 grounding(`chat.perception_block`,可热改)**和定时轮次的
  决定上下文** —— 后者是关键,否则"矿富了所以我去挖"永远不会发生(修之前 stocks 一个
  字都没进过角色的任何上下文,模拟层和角色层是两套跑在一个进程里的系统)。
  `World.perception(agent_id)` 报"她感知到什么"而不是"世界有什么",因为"我以为她知道/
  其实她不知道"是这一层最容易的错。
  ⚠️ `hidden` 目前是**绝对不可知** —— "有人告诉她"那条路(接八卦链)还没接。
- **世界的规律成为数据(world-rules):树会长、矿会枯、修炼会涨功力。** 引擎一直在
  做同一件事 —— 把硬编码变成数据(提示词 → `prompt_templates`,行为树 → `bt_nodes`,
  剧情 → `beats.json`)。**规律**是这条线上最后一段:needs 的衰减曲线、economy 的价格
  漂移都写死在 Python 里,因为"人会饿"是通用的;而"树怎么长""矿怎么再生"**因世界而
  异**,不该由引擎替所有世界决定。
  **量 = (owner, key, value)**,owner 前缀即种类(`tree:oak_01` / `agent:夏` /
  `world`)—— 不发明新的实体系统,和账本的 holder 完全同构,一个"实体"就是共用一个
  owner 的一组量。规律是一条 `{every, for_each, when, set, emit}`:
  `"set": {"size": "min(size + growth_rate * dt, max_size)"}`。
  选择器三种,其中 `{"action": "work"}` 选**此刻正在做这个动作的角色** —— 修炼/采矿/
  耕种都是这一类:投入的是时间,速率由行为者自己的量决定(同样修炼一小时,功法等级
  高的涨得多)。
  **绝不 `eval`**:表达式解析成 AST、逐节点过白名单、由引擎自己的解释器求值,属性
  访问/下标/lambda/推导式一律在**解析时**被拒 —— 这是这个包里唯一一处要执行别人写的
  字符串的地方。
  六条设计承诺各有代价换来的理由:`dt` 让节流**不产生累积漂移**(一万棵树不必每 tick
  算,代价是最多滞后一个 `every`);`dt` 从量自己的 `updated_tick` 算,所以在跑了半年
  的世界里新种的树不会暴涨;**连续变化一律不发事件**(needs 有过 19.7 倍事件量的教
  训),只有 `emit` 的门槛跨过去才发,而且是**边沿触发**(否则长满的树会每 12 tick
  喊一次"我长成了");**双缓冲**让规律之间与顺序无关;整层跑在 tick 线程上(纯算术,
  没有 LLM)。
  加载时严格(坏规律**整体拒绝**——规律是物理法则,少一条不是少一点内容,是从此算错),
  运行期降级(读到不存在的量、除零 → 只跳过那一条,计进 `World.rule_stats()`)。
- **内置世界从"毛坯"变成"橱窗":种子现在能替它的世界点亮开关(`"config": {...}`)。**
  1.3.0 加了 stance / 能力 / 意图分派 / 定时轮次一大堆东西,而**开箱一个都看不见**
  —— 开关此前只能建完世界再一条条 `config set`,种子 schema 根本不支持 config。
  装上包、`anima-world start`,看到的还是 1.0 那个"只会走路说话"的世界。做了等于
  藏起来了,而一个展示不了自己特性的引擎没人会用。
  现在内置种子点亮 needs / economy / social / stance / tools / intent / autonomy,
  并播了三个人两两之间的关系、每人的创世记忆(带锚定)、钱、随身物品、咖啡店货架
  与各自的目标。唯一没点亮的是 `chat.loop.enabled` —— 它把每轮的 LLM 调用乘 2~5 倍,
  不该替用户做一个持续烧钱的决定。
  **引擎默认值仍然全关**:两者的分工是"引擎默认值 = 没人说话时的样子,内置种子 =
  这个世界的作者的意见"。自己写种子的人照旧从素配起步。
  种子 config 的三条纪律:创世时一次(空库才认,已有世界的开关是运行数据,不许被
  今天的种子回头覆盖)、未知键跳过不拒绝(种子会比引擎活得久)、**密文键一律拒绝**
  (种子是分发物,能携带 `llm.api_key` 的种子等于把作者的钥匙寄给每个人)。跳过
  逐条 warning 点名 —— 作者以为点亮了、实际没有,是这个仓库最在意的那类错。
  裁决契约没动:`is_valid_world_seed` 只看 `agents` / `locations` 的必填键,未知
  顶层字段一律忽略,所以**运维台镜像不用跟**。
- **`autonomy.enabled` —— 没人跟她说话时,她自己决定要不要做点什么。** #15 的能力
  此前只在**玩家先开口**之后才有机会被选中:世界自己转的那一半里,角色只按行为树
  过日子,`reach_out` / `broadcast` / `mute` / `refuse_topic` 一次都用不上。现在
  `_maybe_run_autonomy` 挂在调度器的 tick 上,每隔 `autonomy.interval_ticks`
  (默认 72 tick = 6 世界小时)问一次在场的每个角色"此刻想做点什么吗",默认选项是
  **什么都不做**——一个每六小时都非干点什么的角色,比一个安静的角色假得多。
  **时钟永不等网络**这条不变量在这里最容易破:调度器只喊一声就立刻返回,快照在锁内
  取,决定与执行丢到世界自己那条事件循环(`_BridgeLoop`)上跑,一个角色的 LLM 调用
  失败/崩溃只影响她自己(`future.add_done_callback` 把异常喂回
  `World.autonomy_stats()`,不许无声消失)。`autonomy.max_per_day`(默认 2)防止
  一个话痨角色把玩家的收件箱刷满,而且"被问"不算"用掉额度"——只有真的选中一个能力
  才计数。
  新增能力面(`surface`)概念:`reach_out`(她主动走过去搭话,复用 issue #13 的
  `agent_hail` 并守住"敲门不是对话"那条边界)只在自主轮次里出现;`walk_away` /
  `end_conversation` / `delay_reply` / `wait_for_user` 只在聊天里有意义(自主轮次
  里没有"对方"这个人),两边菜单因此不同。`World.autonomy_stats()` 报
  `asked`/`acted`/`quiet`/`failed`/`last` 五个数——这条链最容易的坏法是"看似都对,
  其实一次没触发",所以要有一个地方能把"她确实没想做"和"根本没跑起来"分开。
  顺带修了 `reach_out` 最初实现里的一个真实 bug:在场判定原来是**全世界范围**的
  (`World.who_is_present()`),于是在工作室的角色能"主动走过去"找一个正在咖啡店的
  玩家——现在按同地过滤,和 #13 的 `_maybe_hail_player` 同一条规矩。
- **角色终于有可以选择的行动(issue #15)。** 此前她的"假"不在提示词、在**结构**:
  100% 响应率、零主动,说"我走了"也没真走 —— 因为她**没有别的可做**,只能用词把话
  接下去。`anima_world/tools/` 是数据化的能力注册表(`@tool`,形状对齐 function
  calling),七条:`mute`(软静音,下一条消息当场被拒)、`end_conversation`、
  `delay_reply`(**到点她真的回来敲门**)、`walk_away`(走 BT 那条路真的起程,不是嘴上
  说说)、`refuse_topic`、`broadcast`、`wait_for_user`。调用走**行内标记**
  (`〔tool:mute {"minutes": 5}〕`)而不是 OpenAI 的 `tools=` 字段:没有 key 时世界跑在
  Mock 上,而本地 ollama 与若干兼容端点的 function calling 支持参差 —— 只在原生 tools
  上可用的能力,等于在**默认状态下缺席**。散文照旧一个字一个字流给玩家,控制标记一个
  字都不会漏(`directives.py` 是流式解析器)。
  软静音抛 `AgentUnavailable`(带 `kind` / `seconds_left` / `reason`)而不是回一句空话
  —— 空回复在宿主那边和"LLM 挂了"长得一模一样,而这两件事该让玩家看到完全不同的东西。
- **stance:她这句话背后的关系性意图(issue #18)。** 八个枚举(讨好/讨坏/试探/回避/
  宣泄/挑逗/顺从/中性),回复前显式选一个,影响遣词造句。是 (角色, 对方) 的属性 ——
  她可以同时对你找茬、对别人讨好。两处刻意的设计:枚举**有限可穷举**(开放字符串一开
  就散,LLM 每次编一个新词,下游没法消费);惯性(不要一句讨好下一句找茬)做成**提示词
  压力而不是引擎摇骰子** —— 摇骰子会覆盖角色自己的选择,而且同一句话跑两次给两个结果、
  日志上看不出为什么。`World.stance()` 的 `declared=False` 把"她没选、我们兜的底"和
  "她选了中性"分开:两者在文本上一模一样。
- **意图分派:你说的话不再全被当成 in-character 对话(issue #16)。**
  `style_adjust`("以后叫我霜霜")写进 `persona_overrides`,**按 (角色, 玩家) 永久** ——
  一次教会,跨会话跨天不忘;那个 feature 的核心就是"以后",应一两轮就忘正是要修的病。
  `narrative_direction`("让林素也过来")交给 director,**不进提示词、进世界**:真把人
  挪过来,于是她下一次读 grounding 会真的看到那个人在场,而不是"她想象里的林素"。
  v1 只对已存在的角色动手,不认识的人拒绝并指出下一步(自然语言造人是 v2,那需要每日
  上限、作者 opt-in 与 `authored_by_user` 标记)。分类**往 dialogue 上偏**:低置信度、
  参数不全、分类器抽风一律退回对话并写明原因 —— 该 narrative 判成 dialogue 只是别扭,
  反过来会把玩家正说的话吞掉。
- **连续输出:她可以说完再停,而不是一句一等(issue #17)。** `World.chat_burst()` /
  `achat_burst()` 产出结构化步骤(`budget` / `text` / `message` / `stance` /
  `tool_call` / `stop`)。玩家的原话是"我发一句她回一大段然后停,我又要发才有反应,像
  completion 不像聊天"。四类停下信号缺一不可:显式让位(`〔wait〕`)、隐式让位(问句
  结尾)、预算耗尽、工具要求结束,外加硬上限兜住"模型不肯让位"。预算按性格/关系/心情/
  时间算,**并把依据一起返回**(`3 = 基准 3 + 话多 2 - 深夜 2`)—— 一个说不出理由的
  预算没法调参;性格倾向从 personality **文本**里确定性地抽,所以没有 key 也算得出来、
  跑两次一样。插话由**她自己**判(接着说 / 转向 / 先按住你),于是连续输出的破裂本身
  也是角色反应,不是引擎的硬中断。
  外加一条 **`repeated_step`**:一字不差地又说了一遍就停下。这是装上 wheel 演一遍用户
  故事时看见的 —— Mock 上她把同一句话刷了五遍。那不是"还有话要说",是卡住了(真模型上
  是低温度或提示词打结的样子),而预算会一路把它刷到底。
- **背景槽 `llm.background.model`。** 分类器与 loop 的每一步一轮要打好几次,用主模型
  既慢又贵。空着就退回主模型 —— 便宜快模型是优化,不是前置条件。
- **`anima-world play` 看得见这些。** 连续输出逐条打印,每轮末尾一行观测量
  (`· stance=试探 intent=dialogue(0.92) mute`),软静音按她的口气说("她现在不想理你")
  而不是报错。开关关着时这一行不出现 —— 和 1.2 逐字相同。
- **`contract` 与 `doctor` 报 schema 修订与 chat 能力清单。** 镜像端要知道自己对齐的是
  哪一版,而落后的镜像不报错,它只是对新格式给出旧答案。

### Changed

- `World.chat()` / `record_chat_turn()` 各多一个可选 `meta`:前者是收件盘(这一轮的
  stance / intent / tool_calls),后者把它落到消息行上(intent 落**用户**那行,stance 与
  tool_call 落她那行 —— 挂错行,运维台上的 tag 就挂在错的气泡上)。都是加法,老调用方
  逐字不变。
- 关闭会话那**一个** `conversation` 事件多带整场的 `stances` / `intents` /
  `tools_used` 分布。

### Added —— 提示词终于能被看见(`debug_prompt` / `anima-world prompt`,2026-07-30)

1.3 一路下来最贵的一课是账面上的:**四个 bug 有三个在提示词里**(stance 声明率 2/6、
能力一次没用、定时轮次 18 轮 0 动作),而每一个的诊断都需要同一件事 —— **她到底收到了
什么**。当时唯一的办法是写 Python 往 `ChatService` 的私有属性上塞一个假 LLM 去偷看。
宿主、世界作者(改的正是 `prompt_templates` 里的模板)一个都没有。这个仓库处处讲
"降级不许无声",而提示词是唯一一处**连它长什么样都看不见**的地方。

- `World.debug_prompt(agent_id, …)` 交出这一刻的提示词,逐块带来源标签:`blocks` /
  `order` / `absent` / `system`。CLI 是 `anima-world prompt --agent 夏`
  (`--full` 连正文、`--json` 给脚本、不给 `--agent` 就列名册)。
- **它不撒谎。** 块来自新的 `ChatService.prompt_blocks` —— 和真聊天**同一个函数**
  (`_prompt_for` 现在只负责把块并起来)。调试视图另写一遍拼装迟早会分叉,那时你会
  照着它去改一个不存在的问题。`tests/test_debug_prompt.py` 拿真聊天送进 LLM 的那一段
  逐字比对盯着这一条。重构本身对模型收到的字**逐字无变化**(与重构前同一个 db 抓两次
  prompt 对比,九块全部一致)。
- **它解释缺席。** `absent` 报的是**原因**而不是 "missing":少一块几乎总比多一块难查
  (世界照跑、她照说话,只是从来没提那棵树)。"没有任何量声明过可见性,用
  `declare_visibility()`" 与 "声明了 3 条但这一刻一个都感知不到" 是两种病,不报同一句。
  反过来:**永远不可能缺席的块不许在这里写理由** —— identity 那条一开始就写了,而真
  聊天会把 `display_name` 兜底成 `player-xxxx`,于是它永远在场。一段假装解释的死代码
  比没有更坏,被测试逮到后删掉了。
- **看,但不碰。** 不推时钟、不进 LLM、不写 `players.last_seen`,**静音中的角色也照样
  交出提示词**(而 `chat()` 这时抛 `AgentUnavailable`)—— 她不理人的时候恰恰是你最想
  知道她收到了什么的时候,调试入口跟着一起拒就等于没有。
- 顺带把块顺序变成一处**显式的决定**:`chat_service.PROMPT_BLOCK_ORDER` 列出十一块并
  写明三段分工(开头=她是谁 / 中间=她此刻的处境 / 末尾=她要照做的),有测试盯着实际
  顺序是它的子序列。**末尾只有一个,抢的人多了就不值钱** —— 认知层就留在中间,实测她
  在那儿照样读得到。往末尾加块之前必须回答:它是"事实"还是"要照做的"?

### Fixed —— `simulate` 悄悄不跑定时轮次(2026-07-30)

`_autonomy_hook` 全仓库只在 `World._install_autonomy` 里赋值,而 `simulate` 直接建
scheduler、从不构造 `World` —— 于是 `start` / `run` 会问她"此刻想做点什么吗",
`simulate` **从来不问**,一声不吭。而它的 docstring 写着 "Builds the **exact same
scheduler** `run` would",那句已经不成立;叙事与规划(两个都打 LLM)照旧在快进里跑,
唯独漏了这一个。

不接上是对的:快进一年 = 每个角色每 6 世界小时一次 LLM 调用,上千次网络往返,而快进
的全部意义就是不等。**但不许无声** —— 出厂种子把 `autonomy.enabled` 点亮了,用户第一
次快进看到的是 `autonomy_stats()` 全 0,分不清"她不想做"和"根本没跑起来",而那个函数
存在的唯一理由就是把这两件事分开。现在开关开着时 `simulate` 打一行说明并指向
`anima-world run`;开关关着就不提(一句和你无关的警告只会训练你忽略所有警告)。

### Fixed —— world-rules 的写入方向有个静默的错(2026-07-30)

一条规律只能写**它自己那个 owner** 的量,但两种"看起来显然能写"的写法此前被静默
接受了:

```jsonc
"set": {"world_总产量": "world_总产量 + 1"}   // 在那条规律自己的 owner 名下
"set": {"mine:north.储量": "储量 - 1"}       // 建了个带冒号的怪名字
```

世界的量一动没动,而 `rule_stats()` 报 `written: 5, skipped: 0` —— **专门用来回答
"这层跑通了吗"的仪表说的是成功**。`world_x` 尤其毒:**读**它是对的(任何表达式都能
读全局),于是作者理所当然假设写也对称。现在两种都在**加载时**当场拒,错误信息直接
给出该怎么写(要改全局量,用 `"for_each": {"owner": "world"}` 的规律写它自己的量)。

跨实体的相互作用(挖矿让矿脉减少)**v1 表达不了**,而这一点现在是明写的。不悄悄
放行的理由是双缓冲下扇入没有意义:一条作用在一百棵树上的规律,每棵读到的全局量都是
这一轮开始前的同一个值,"每棵树 +1"的结果是 +1 而不是 +100 —— 一个看起来对、算出来
错的语义,比当场报错坏得多。要它得先设计扇入语义(求和?最后一个赢?)。

### Fixed —— 有了观察窗之后立刻查出来的三处(2026-07-30)

`debug_prompt` 一上线就开始还债 —— 这三条都是它(或它带出的那次逐块核对)找出来的:

- **一段提示词读了两遍世界,而两次之间时钟会走。** 在场块和身份声明各调一次
  `world_provider`,于是同一段提示词里可以同时出现"你在咖啡店,同在这里的还有:没有
  别人"和"你当前在建筑工作室,因此对话媒介是手机私聊" —— 还顺手禁止她描写站在面前的
  玩家。LLM 会挑一边编,而且**无声**(`in_transit` 那道闸修的是同一种病的另一扇门)。
  现在 `_world_snapshot` 读一次,那一份贯穿全部块;两次读一共才 1.5ms,所以这从来
  不是性能问题,是**一致性**问题。回归测试直接数调用次数。
- **`broadcast` 从来没有在世界里兑现。** 它只发一个 `agent_broadcast` 事件,而**没有
  任何角色消费它** —— 她"当众宣布"的后果是一行日志,世界里谁也不知道;菜单却告诉她
  "世界里的人都能看到",她照着一句假话做决定。CLAUDE.md 的硬不变量是**她的选择必须在
  世界里兑现**(`walk_away` 真起程、`delay_reply` 真回来敲门、`narrative_direction`
  真挪人),只有广播落在空处。现在给每个**在场**的角色发一条 `memory_seed`,调度器
  折进他们的记忆 —— 走现成的路,不新造广播收件箱:记忆本来就是"角色知道一件事"的表示。
  听众是同一个地方的人而非全世界(一句喊话传遍地图正是 §2.9.4 立规矩要防的错);
  payload 里 `audience` 随之从 `"world"` 改成 `"here"`,并多了 `heard_by`。
  原来那条测试只断言日志里有这行,名字还叫 "visible to the whole world" —— 它验的
  恰好是被兑现的那一半。
- **`walk_away` 的描述在压制一个已经能用的能力。** 它写着"(面对面时才有意义)",而
  引擎里隔着手机它会优雅降级成挂断。一句描述就能让能力事实上缺席 —— 和 issue #15
  那次"缺的是许可不是能力"是同一件事。改成"面对面就真的离开这里,隔着手机就是挂断"。

另外 `doctor` 多报一条:`chat.intent.enabled` / `autonomy.enabled` / `chat.loop.enabled`
开着而 `llm.background.model` 空着时,点名"这些便宜活正在用主模型"。分类那次往返
**串在回复前面**,所以玩家等的是两次生成而不是一次 —— 而她照样回话,这条永远不会
自己暴露。开关全关就不唠叨。

### Fixed —— 三层接真模型验过之后改的两处(2026-07-30)

autonomy / world-rules / perception 起初只在 Mock 与脚本化替身上验过 —— 而这个仓库
刚教过一课:**单测全绿不等于真模型会照做**。接上真 LLM 各验一遍:

- **perception 一次通过,而且比预期好。** 最怕的两种坏法都没出现:她把 `树高 9.4`
  说成"目测得有九米多快十米了吧,反正挺高的"(转成人话还加了不确定的口气),把
  `雨天数 23` 说成"这都下了二十多天的雨了";问她无关的事时**一个数字都没提**
  (没有硬塞)。顺带还把感知织进了生活:"我每天擦窗户都能看见它又蹿了一截"。
- **autonomy 是反向失败:18 轮 0 次动作,这一层原本是个永远不触发的空机制。**
  提示词把"什么都不做"说了三遍("你可以什么都不做""**这也是最常见的选择**""什么都
  不想做就回无"),而"什么时候该主动"一句没写。
  这和 issue #15 那次是**同一个错的镜像**:那次是"给了能力却没给许可"。更要紧的是
  我当时**用提示词去做限流** —— 明明已经有 `autonomy.max_per_day` 这个真正的硬上限。
  两道刹车叠在一起,车就不走了。分工应该是:**硬上限管节制,提示词让她像个人。**
  改掉三重"别动"、补上"什么时候值得主动"(身边有你在意的人而你还没开口 / 有事想让
  这一带都知道 / 有人反复越界 / 就是想找人说句话)之后:**0/18 → 1/15**,而且那句
  开场很自然("嗨,你是常客吗?我怎么感觉以前没见过你?")。
  ⚠️ 一个样本,而且默认节奏(每 6 世界小时问一次、每天最多 2 次)下这个比例意味着
  她大约每三四天才主动一次 —— **偏稀**。提示词是热改的,世界作者可以自己调。

### Fixed —— 用真模型跑三局之后改的六处

四个开关点亮、接上真 LLM(LongCat-2.0)自己玩了三局:一局八轮的正常流程 + 两局
"被真的欺负"的探针。**机制是通的**(她真的会走开、会结束对话,世界真的跟着变),
但六处只有真人玩才看得见:

- **提示词位置就是权重。** stance 与能力菜单原来夹在中间,后面紧跟着全篇最响的一段
  (身份声明,标着"最高优先级事实")。结果:六轮里她只声明了两轮 stance,能力**一次
  没用**。两块移到整段 system prompt 的最后之后,声明率变成 5/6,而被逼到难听话时
  4/4 轮动用了能力。位置改动写在 `_choice_blocks` 的 docstring 里。
- **两处措辞是对撞出来的。** 回复格式那段("所有动作描写必须放在括号内""输出前逐个
  检查所有括号",正确示例以动作括号开头)和"第一行输出〔stance:…〕"直接冲突 ——
  现在明说这一行不是动作描写、不受括号规则约束,且"漏了就等于没选"。能力菜单原来
  只写了有哪些、怎么写,补上了**什么时候用是在角色里的**那句许可("你可以拒绝。不要
  因为要客气就把话顺着接下去")。缺的从来不是能力,是那句许可。
- **对一个不在你面前的人"走开"是个空动作。** 她在咖啡店被骂、`walk_away` 走去工作室
  —— 对的;但玩家还在咖啡店,下一句照旧发得到(手机私聊),于是她又走开一次、再一次,
  连着四趟真行程写进世界。现在隔着手机的 `walk_away` **降级成挂断**,并在 detail 里
  写明 `degraded_to`。在场的语义得由引擎守住,不能指望模型每次都想到自己在打电话。
- **一个只调工具、没有台词的步骤,会把她刚声明的 stance 覆盖成兜底的中性。** 她摔了
  围裙走人(provoke),紧接着那一步只有一个 tool_call —— 世界于是记着"她对你很平淡"。
  现在**没声明不许覆盖已经记着的那个**(只允许初始化)。
- **连续输出会原地绕圈,而且跨轮复读。** 第二句常常是第一句换个说法(甚至一字不差),
  第四轮里还能整段照抄第二轮说过的一段。续说提示词现在明说"往下推进、不要重复自己
  也不要再答一遍对方那句话";查重从"一字不差"改成**按句子比对**,并且覆盖宿主递进来
  的整段近期历史,不只是本轮。第一步永远照旧交出去 —— 查重不许把一轮变成沉默。
- **同步门面每调一次就新建一个事件循环,而 HTTP 客户端是被缓存复用的。** 于是一个
  属于已关闭循环的连接池被后面每一次调用继续用:每轮都在刷
  `Task was destroyed but it is pending` 与 `aclose was never awaited`,而真正的危险
  是它某天变成 `Event loop is closed`(表现是"聊天忽然全炸")。这是 1.2 就有的,
  连续输出把每轮的调用次数乘了 3~5 倍才让它显形。现在世界自带一条循环线程
  (`_BridgeLoop`,`close()` 收掉),`ConfigBackedLLMClient` 也把**循环并入缓存键**
  —— 混用 `chat`(门面)与 `achat`(宿主自己的循环)时两边各自持有客户端。顺带:
  连接与 TLS 握手能复用了。

一句实测的等待时间,给做 UI 的人:一轮 2~12 秒是常态(一次连说 2~3 句),偶尔窜到
20~30 秒,极端一次 92 秒。流式吐字能盖住大半,但**连续输出会把慢调用的暴露面乘上
步数** —— 宿主那边"她还在说"的占位符不是装饰,是必需品。

### 一处对 issue 文本的偏离(有意)

#15/#16/#18 里都写着"每轮发一个事件"(`agent_stance` / `user_intent`)。这里**没有**
那样做:每轮一个事件等于把聊天转录搬进世界的历史,而「聊天子系统与事件核解耦、整场
会话只发一个事件」是这个子系统存在的前提。取法是:逐轮观测量落在 `messages` 行上
(运维台照样能显示 tag),分布随关闭事件出去,而工具造成的**后果**(走开、广播、静音)
照旧是世界事件 —— 世界的历史仍然只记世界里发生的事。

## [1.2.0] — 2026-07-28

同 db 格式(**1**)与包格式(**1**),1.0.x / 1.1.x 建的世界照常打开。
主题:**把"有机制"变成"兑现"** —— 这一版里几乎每一条修的都是同一件事:某个能力
声明过、schema 里有位置、文档里写着,而实际路径上没有人真的读它。

⚠️ 三处行为变更(都在同一条原则上:让默认状态说真话)

- **工资按上班时长发**,不再是每天无条件一份。整天睡觉的人当天不发。
- **玩家余额以账本为准**。已经有过购买记录的老存档,账本上此前是负数,现在那是权威值。
- **`report_format_version` 已在 1.1.1 升到 2**;`by_day` 稀疏且只覆盖世界 tick 事件。

### Added

- **`World.history()` —— 全量事件历史的门,分页。** `World.events()` 背后是
  `deque(maxlen=200)`:一个内存窗口,不是历史,而返回值本身不带任何标记。宿主拿到
  一个 200 元素的列表,起始 seq 是 242,看不出前面还有 241 条,照它做统计不会有任何
  报错(1.1.1 验证报表时就是这么被坑的)。`history()` 返回
  `{"events", "next_seq", "total"}` —— 截断做成分页,结构性地没法忽略;支持
  `who` / `kind` 过滤。同时 `events(since_seq=…)` 在窗口已经滑过 `since_seq` 时打一条
  warning:那正是调用方即将拿到一段有洞的历史却以为自己追上了的时刻。
- **`World.fast_forward()` / `World.report()` —— 宿主自己也能快进和出摘要。**
  快进的等规划纪律(每个世界日一份等待预算,连续两天用光判定 planner 已死)从
  `simulate` 的命令体里提到 `Scheduler.fast_forward`,CLI 与门面共用一份实现。
  返回的不是一个 int 而是 `{"ticks", "clock", "planner_gave_up", "exhausted_days"}`
  —— **一个安静的世界和一个规划全程没跟上的世界,产物看起来一模一样**,只有
  `planner_gave_up` 能把它们分开。
- **`World.achat()` —— 原生 async 的聊天流**,以及同步三扇门在 async 宿主里不再炸。
  门面是同步的(这是设计),但"同步门面"不等于"只能从非 async 代码里调用":
  FastAPI / aiohttp 的处理函数就是 `async def`,而 README 把"嵌入到应用里"写成主要
  用法。`asyncio.run()` 与新建事件循环在**已有 running loop 的线程**上都会当场
  RuntimeError 并漏一个 never-awaited coroutine,于是 `chat` / `record_chat_turn` /
  `close_conversation` 全炸,连**开机补完孤儿会话**都会静默失败(只留一行 warning)。
  现在检测到就换个线程跑,语义逐字不变。流式经队列转发,不退化成"等全部"。
- **`anima-world play` —— 在活着的世界里说话。** `chat` 说话但时钟不走,`run` 时钟走
  但说不了话,于是"跟一个正在过日子的角色对话"在命令行上一直做不到。`play` 一边走
  时钟一边聊,`/who` 看这会儿谁在哪、`/at` 换人。每轮说话前重新定位玩家,所以判定
  是面对面还是手机私聊**会随她走动而变**。
- **`anima-world contract [--json]` —— 引擎自报它的线格式。** 本仓库是跨语言契约的
  权威,别人持有镜像;今天镜像端要知道"我对齐的是哪一版"只有读 Python 源码一条路,
  于是镜像悄悄落后 —— 而**落后的镜像不报错,它只是对新格式给出旧答案**。这条命令
  报 db / 包 / 报表三个格式版本,以及种子 schema 与节拍 op 表的形状(那两者没有
  版本号,随主版本走)。跑不了世界也能回答,不碰 db。
  顺带补了 `beats.OP_REQUIRED_FIELDS`:`agent_join` 走单独的校验器,契约面上不该
  因此缺一格。
- **导演终于看得见世界:节拍谓词从 2 个变成 7 个。** 此前只有 `sentiment` 和
  `co_located`,于是节拍脚本对世界的绝大部分状态是瞎的 —— 需求、钱、物品、关系描述、
  记忆一律观察不到,剧情只能靠"到点了"和"两个人碰上了"来推。新增 `r_type` / `need` /
  `money` / `has_item` / `memory`,读的全是投影与黑板里已有的量,不进事件日志、不改
  db 格式。读不到的东西一律读作**未满足**(宁可晚触发不可错触发);`memory` 谓词是
  **纯读**,不加固记忆 —— 观察不该改变被观察的东西。
- **节拍能改世界的物质了**:`pay` 与 `grant_item`。op 此前只能改"她怎么想",改不了
  "她有什么" —— 作者写不出"父亲的怀表在这一幕里丢了",只能写一条"她觉得很难过"的
  记忆去暗示。两条都展开成账本已有的事件类型(`payment` / `item_transfer`)。
  `amount<=0` 与 `qty==0` 会被拒绝并说明原因,因为投影对它们是 no-op —— 作者以为
  钱转了,其实没有。`qty` 为负表示拿走(调换两端,而不是发一条什么也不做的事件)。
- **§9 的谓词表也变成机器校验的**,和 op 表同一条纪律;`contract --json` 一并报出。
- **`anima-world events export` —— 事件流的格式中立导出(issue #8)。** JSONL,一行
  一个事件,不依赖 db 格式。**只做导出这一半**:`replayable` 恒为 `false`,而 header
  里逐条写明它带不走什么(图谱边、记忆强度与反思水位、静默尾部的时钟、聊天转录)。
  一份不说明自己缺什么的导出比没有更危险 —— 拿到的人会以为那就是整个世界。
  刻意不做重放端:事件日志今天还不完备,在它补齐之前把这份东西固化成第四条跨仓库
  线格式,等于把一个已知缺陷刻进契约。
- **`anima-world report` —— 对着一个已经存在的 `world.db` 出摘要,只读。**
  `simulate --report` 只在你自己跑这一趟时给得出摘要。刻意不用 `open_db`(路径打错
  会当场建一个空世界然后报告"0 事件、世界健康"),也不碰 `load_or_create_key`。
- **`World.player_leave()` / `World.who_is_present()` / `World.inbox()`** —— 见下面
  「角色会来找你」。
- **`anima-world validate seed|beats` —— 不建世界就检查作者写的东西。** CLAUDE.md
  一直写着"创作台经 CLI 委托校验",而这个入口不存在:作者唯一的检查办法是真开一次
  世界,而**种子只读进空库一次**,试错的代价是重建世界。硬错误退出码 2;引用完整性
  (角色/地点存不存在)只**提醒**、退出码 0 —— 一个 beat 完全可以先 `agent_join`
  再使用,把 advisory 升成拒绝会让设计正确的脚本在小版本升级后开不了机。
- **种子能直接写行为树**(`agents[].behavior_tree`)。`duties` 只表达得了"时间窗 →
  动作",要写条件分支、需求带、嵌套选择器就够不着,只能去手改 db。缺席时行为逐 tick
  不变;坏节点跳过并警告,绝不阻塞开机。
- **`chat.response_format` 成了可改的提示词。** 一段写死在 `chat_service` 里的中文
  排版规则(动作描写用全角括号、括号内以角色名开头)每次聊天都注入系统提示,而它
  没注册进 `prompt_store._DEFAULTS` —— 模板照样生效(读的是同一个 store),但作者在
  `World.prompt_list()` 里看不见它存在,于是一个英文世界、或一个不想要动作描写的
  世界,永远关不掉它。补了一条防漂移测试:`chat_service` 读到的每个提示词名字都必须
  在作者够得到的面上。

### Fixed

- **角色会来找你了(issue #13,按访客模型)。** 关系此前是单向发起的:玩家能影响世界
  (记忆、关系、图谱边、八卦),角色却不会决定"今天去找阿檀聊聊"。选访客模型而不是
  居民模型:居民要新表 = db 格式变更 = 下一个主版本,而访客模型在 1.x 内就交付得了
  "角色会来找你"本身。**离场语义是前置条件,不是配套改进** —— `world.players` 此前
  只有写没有删,而 CLI 每聊一轮都调一次 `player_move`,长跑宿主会攒一屋子幽灵访客;
  一旦让角色看得见在场玩家,那就变成 NPC 走去敲一个断线三小时的人的门。所以先有
  `player_leave`(幂等)+ TTL 兜底 + `who_is_present`,再有 `agent_hail` 与
  `World.inbox()`。TTL **不是心跳契约**:任何一次交互都算"我还在"。
  ⚠️ **敲门不是对话**:`agent_hail` 不产生记忆、不动关系、不开会话 —— 否则你会看到
  "她来找过我",转头问她却毫无印象。
- **演示世界里终于有人开口。** 柔 每天 15:00 走到咖啡店、15:30–17:30 待在那儿,而
  夏 08:00–18:30 一直在店里 —— 两个人每天同处一室两小时。但柔那段是 `idle_social`,
  它**不指名道姓**,所以只传八卦、不触发关系判定。于是七天跑下来 NPC 之间一条关系都
  没有、`cliques()` 恒为 `[]`,看起来像"社交机制没用",实际是"没有人真的开口"。
  种子里那一行改成带对象的 `chat`。刻意**不做掷骰式相遇**:世界要可重放。
- **规划器看得见世界了**:prompt 里带此刻的处境 —— 她在哪、需求水平、钱包、别人这会儿
  在哪在忙什么。此前它只知道"我是谁、有哪些空窗、能做什么、记得什么",于是排出来的
  一天依据比世界实际拥有的信息少得多:让一个在家的人"继续在咖啡店待着"。
- **工资按真的上过多久班发。** 此前日切时每人无条件一份,金库允许无限负债 —— 整天
  睡觉的人和开了十小时店的人到手一样多,那"经济"就只是个每天加数的计数器。
- **`item_defs.restores` 终于有人读了。** schema 里一直有这一列、创世时也写进去,而
  `RESTORE_PER_TICK["eat"]` 是个跟吃什么无关的常数 —— 作者写的"这碗面很顶饱"在世界里
  没有任何差别。
- **玩家的钱收敛到账本。** `player_topup` 此前只改内存、不发事件,而 `player_buy` 拿
  那个内存数做门禁、却把花费发成 `payment`。于是同一个玩家有两个余额:内存里是
  "充值 − 花费",账本里是"**负的花费**",而 `World.balance()` 读后者 —— 实测充 50
  买一杯 6 块的东西,一个说 44,一个说 −6。
- **降级会在世界里留下痕迹。** 叙事/规划/关系判定三处上报成败,计数进
  `state().runtime.subsystems`,档位切换落一条 `subsystem_health` 事件。此前降级只在
  stderr 刷一行 warning,而日志会滚掉 —— 一个整整三天没有 planner 的世界,和一个角色
  确实无所事事的世界,产物看起来一模一样。**只在切换时发**,不是每次都发。
- **一个正在跑的世界现在会自报。** 开世界盖 `owner_pid`/`owner_host`,关世界撤掉;
  `config set` 与 `doctor` 撞见活库时出声。最尖的一处是 `config set`:它开自己的连接
  写库、打印"已保存",而运行中那个世界的 `ConfigStore` 缓存不会重读 —— 你以为改了,
  其实要等下次重启。**只提示不拒绝**:进程崩掉标记就陈旧,拿陈旧标记去拒绝操作,
  等于在真出事那天把人挡在门外。
- **一行合法的 SQL 就能让整棵作者树静默塌掉。** `bt_nodes` 的 CHECK 放行
  `need_action`,而 `BTStore._build_node` 不认它:构造器抛 ValueError,调用方兜成
  一行 warning,**整棵作者树退回 `default_bt()`** —— 一个只会 idle_wander 的根选择器。
  世界照跑,角色什么也不干,而那行 warning 甚至不说是哪个节点惹的祸。现在
  `need_action` 能构造(带作者写的收工线),回退时的 warning 也会指名道姓。
  加了一条测试逐个类型验证:schema 放行的每一种,构造器都必须造得出来。
- **REFERENCE §9 的节拍 op 表是错的** —— 照它写的脚本开不了世界。`memory` 的必填
  是 `agent_id` 不是 `agent`,`broadcast_memory` 是 `location` 不是 `agents`,
  `persona_update`/`agent_leave` 同样是 `agent_id`,而 `agent_return` 还必须给
  `location`(文档整个漏了)。加载期严格只有在"照文档写就能过"的前提下才说得通,
  所以这张表现在是**机器校验的**:`tests/test_beats_doc_contract.py` 解析它,逐字
  比对 `beats.OP_REQUIRED_FIELDS`。
- **角色记得你,但检索不出你。** 聊天召回的三因子检索用 `interlocutor_id` 当 query,
  而那是宿主给的不透明 id(`p1`、一个 uuid);记忆文本里写的是**名字**。两者字符
  二元组交集恒空 → relevance 恒 0 → 检索静默退化成「最近 + 最重要」,与对方是谁无关。
  现在 `World.chat` 把 `display_name` 记进 `world.players`,检索优先用它,取不到才
  退回 id(NPC 之间不受影响,那边 id 就是名字)。显示名只有一个字时按「降级不许
  无声」打一条 warning —— 单字的二元组匹配不到任何记忆。
  顺带:`player_move` 改成更新而不是整条替换,否则 CLI 每轮先调它会把名字冲掉。
- **记忆检索的次序补到 id 为止**(硬化,不是修 bug)。`ORDER BY tick DESC` 分不出
  同 tick 的记忆,而创世注入的 `memory_seed` 全是 `tick=0`;余下的次序原本交给
  SQLite 的物理布局,同一个世界在两台机器上可能召回不同的记忆,并且不报错。
- **需求带加了迟滞,角色终于能吃饱一顿。** `needs.URGENT` 是单阈值:饿到 `0.15` 吃一
  个 tick 净回 `0.045`,已经高于触发线,立刻回去干活,十来个 tick 后再饿回来。角色
  永远卡在 16% 的饥饿度上(实测 300 tick 内 hunger 只有**两个**取值),而每一次切换
  都发一条 `agent_action` + 一条 `narrative` —— 12 世界日的事件量 **19.7×**、
  `narrative` **32×**、耗时 **7×**。narrative 配了真 key 就是一次 LLM 调用,所以这是
  **32 倍的账单**,换来的不是 32 倍有趣,只是抖得厉害。
  新增 `needs.RELEASE`(energy `0.85` / hunger `0.75` / social `0.50`):开始恢复就恢复
  到饱。同一场景现在是 1.7× 事件量、1.3× 耗时,饥饿度在 600 tick 里走出 528 个取值的
  锯齿。判据是黑板上的派生值 `need._restoring`,不是第二份状态,重启即自愈;
  作者树里没写收工线的 `need_action` 节点行为逐 tick 不变。

## [1.1.1] — 2026-07-28

同 db 格式(**1**)与包格式(**1**),1.0.x 与 1.1.0 建的世界照常打开。
主题:**引擎不再对自己说谎** —— 派生视图曾经
对同一段历史给出两个答案,而世界照跑、没有任何报错。这一批全是那一类:测试看得见
的东西没坏,拿到手的东西是错的。

### Fixed

- **重开世界不再把所有人传送回出生地。** 角色走路发的是 `state_change` +
  `kind=location_join`,而投影拿这条事件去*注册地点*,从不写 `agents[who].location`。
  开机名册读的正是 `projected.location`(那一侧本来就是对的),于是每次重开,世界对
  "谁在哪"的记忆就退回创世那一刻。运行中同样分叉:活黑板说柔在咖啡店、投影说她在家。
  还有一个必要条件容易漏 —— `_record_event` 里的折叠发生在 `_stream_event` **之后**,
  而后者把位移事件改写成 `agent_action{action:"walk"}`,所以折进投影的根本不是那条
  位移。两处一起改,否则"位置对不对"取决于你有没有重启过,比统一错更难查。
  老库重放即自愈,无迁移。
- **玩家动作的内容不再存成一个空对象。** `player_action` 的
  `player_id`/`role`/`action`/`details` 全在事件顶层,而只有 `payload` 落库 —— 玩家在
  世界里做过的每一件事,重放之后一个字都不剩。四个字段**复制**(不是搬走)进 payload,
  顶层形状不变;重放侧同样回填四个键,实时流与重放形状一致。
  ⚠️ 只对此后产生的事件成立,老库里的 `{}` 不补也不迁移。
- **`simulate --report` 不再被一次聊天撑爆。** `events.ts` 跑着两种时基:引擎盖世界
  tick,聊天子系统给 `conversation` 盖墙钟。报表把 Unix 时间戳当 tick 折算成"天",
  于是 `by_day` 按 `range(max_day + 1)` 稠密展开成六百多万行 —— 放不下是 MemoryError,
  放得下是 `days=6198680` 的假答案外加被 horizon 稀释成 `other≈1.0` 的时间分配。
  引擎早就知道这条界线(时钟恢复一直在过闸),只是报表没用上。
- **同处一室不再被当成手机私聊。** `chat_service` 按玩家位置在"面对面交谈"和
  "手机文字私聊,对方不在你当前场景中"两段身份声明里选一段,后者还禁止角色描写看见你
  —— 而 `World.chat` 组 `interlocutor` 时从不传位置,面对面那一支经门面**从来不可达**。
  `anima-world chat` 明明先替你走到对方跟前,角色照样只能演"在手机上收到"。
  现在读 `world.players` 里的位置;宿主没调过 `player_move` 就维持手机私聊,引擎不猜。

### Changed

- **`report_format_version` → 2**(与引擎版本分开,消费方不必升引擎)。
  `events.by_day` 改为**稀疏**(只列真的发生过事情的天),并且**只覆盖世界 tick 上的
  事件**;墙钟事件仍计入 `total` 与 `by_type`,另在新字段 `events.wall_clock_events`
  里单独点名。等式随之变成
  `sum(by_day[*].total) + wall_clock_events == total`。
  把这些事件整个剔掉会得到一份"聊了一整晚但 chat 桶为 0"的干净摘要 —— 比撑爆更坏,
  因为没人看得出自己少读了东西。
- **`World.world_context()` 的 `presence` 新增 `in_transit`**。在途不算在场:黑板的
  `loc` 要落地才改写,只比地点会让一个正在赶路的角色被判成和你面对面,同一段 prompt
  里既说"正在去建筑工作室的路上"又说"我们面对面"。

## [1.1.0] — 2026-07-27

Same db format (**1**) and package format (**1**) as the whole 1.0.x line. Worlds built
by 1.0.0–1.0.2 open unchanged. Theme: **the engine stops swallowing what it knows** —
a package says what it needs, a rejection says which thing is wrong, a fast-forward hands
back numbers, and the front door finally has a way in.

### Added

- **`anima-world world inspect <package> [--json]`** — read a `.cyberworld`'s manifest
  without being able to run it (#3). Reading the envelope no longer depends on passing
  the engine-compat gate, so a launcher managing several engine versions can ask
  "which engine does this need?" *before* it has that engine. An incompatible package
  gets an **answer** and exit code 0; only an unreadable one is refused. The JSON field
  set is documented in REFERENCE §8 as a wire contract. New public helpers:
  `read_package_manifest()`, `WorldPackageManifest.validate_structure()` /
  `validate_engine_range()` / `runs_on()` / `compatibility()`.
- **`anima-world chat --db-path <db> --agent <id>`** — talk to a character from the
  command line (#6). Everything it needs was already on the facade (`chat_reply` →
  `record_chat_turn`); what was missing was a door. Omitting `--agent` lists the cast,
  which is also the first way a world file has ever been able to say who lives in it.
  The clock does not advance while you type.
- **`anima-world simulate --report PATH`** — a machine-readable run summary (#11):
  per-world-day event density by bucket, pairwise encounter counts and durations,
  relationship curves with turning points, and per-resident time allocation with an
  explicit `idle_only` flag. Carries its own `report_format_version`, separate from the
  engine version. New module `anima_world.sim_report` (a pure function over an event
  list, so it can be recomputed offline against any `world.db`).
- **The world seed can author the material layer** (#12): top-level `items`,
  `agents[].money`, `agents[].inventory`, and `locations[].stock`. Economy/needs shipped
  with mechanisms but no genesis entry, so an authored keepsake ("she never takes her
  father's pocket watch off") could only be dropped or demoted to a memory string. An
  item id that is only referenced gets an automatic definition, so the short form just
  works. Same tolerance as every other seed field: absent = today's behaviour, bad
  entries dropped one by one, never blocks boot.
- **The world seed can author Mock narration** via `mock_narration` (#9), including
  action kinds this engine has never heard of.

### Changed

- **Template packages now travel within a major** (#4). `engine_min` for a `template`
  export is the floor of the current major instead of the exact exporting version. A
  snapshot carries a format-stamped `world.db` and keeps the exact floor; a template
  carries only `world_seed.json` — version-neutral authored data whose schema is a
  mirrored cross-repo contract precisely so it can travel. Stamping both alike turned
  "you cannot carry your save forward" (the documented, accepted trade-off) into
  "you cannot carry your **content** forward", which nobody decided.
- **Mock narration follows the world's language instead of the engine's** (#9). The
  templates moved from hardcoded English in `narrative.py` into the prompt store
  (`narrative.mock.<kind>`, `narrative.mock_memory_suffix`), read live and authorable
  per world. No API key is the *default* state, so `遥 wandered around——还记着…` —
  English verbs, Chinese name, Chinese memory suffix, all in one line — was the first
  screen, not an edge case. A failing real LLM falls back to the same world-owned
  templates. `eat` gained a template of its own instead of rendering as "did something
  custom".

### Fixed

- **Player conversations now change the world without an API key.** The chain was
  complete on paper — `conversation` event → a 0.8-importance memory → relationship
  verdict → band crossing → `relation_shift` memory + graph edge → gossip source +
  planner context — but it broke at the first link: a Mock LLM cannot produce a
  parseable verdict, so the judge returned `None` on every call. The consequence was not
  "smaller changes", it was **no relationship data at all**, for players and NPCs alike,
  while three-axis relations are documented as always-on. No key is the *default* state,
  so the screen where README promises characters who remember you was exactly the screen
  where talking to them changed nothing — announced only by one `dropping` line on
  stderr while the character replied normally. The mock tier now gets
  `DeterministicRelationshipJudge`, the same treatment the reflector already had:
  `Δ = 0.04 × (1 - |current|)` — no RNG (worlds must stay replayable), asymptotic, never
  saturating, an order below the ±0.2 verdict ceiling. It does not pretend to be
  judgement: always positive, magnitude from headroom alone. `r_type` gets no stand-in
  and keeps its authored text — a number has a sane mechanical substitute, authored prose
  does not. A configured key still gets the real judge.
- **`World.graph(agent_id)` always returned an empty list.** Edges store subjects as
  `agent:<id>` and the parameter takes a bare id, so the lookup never matched — and it
  failed by returning `[]`, which a host reads as "this character has no relationships"
  rather than as a mistake. Bare and prefixed ids are both accepted now.
- **Package rejections name which thing is wrong** (#10). Checksum mismatch, engine
  range, seed schema, and the zip guards each printed the identical
  `invalid or inaccessible package data`. The operator can only relay what the engine
  says, so its 400 carried no reason either and an author could not tell "re-export with
  a matching core" from "fix the seed". Seed problems now carry the per-entry detail
  `world_seed_errors()` was already producing and the package layer was discarding.
  Exit code is unchanged (2).

## [1.0.2] — 2026-07-23

Same db format (**1**) and package format (**1**) as 1.0.0/1.0.1. Worlds built by either
open unchanged. Theme: **the db is whole the instant a player touches the world** — no
more "close the world first to get a complete file".

### Added

- **`World.export_snapshot()` — live export.** Package a running world into a
  `.cyberworld` snapshot without stopping it: checkpoints are flushed first, the db is
  copied under the world lock via the SQLite backup API (ticks are blocked only for the
  copy itself), and packaging happens outside the lock. Secrets are stripped the moment
  the copy lands. The exported seed resolves explicit `seed_path` → the genesis seed
  recorded in `db_meta` → the bundled seed (with a warning).
- **Genesis-seed provenance.** First boot into an empty database now records the seed it
  was born from in `db_meta` (`world_seed`), so a snapshot always carries its true birth
  certificate. Empty-db-only, like every other seeding step; pre-1.0.2 databases simply
  lack the row. Additive row in an existing table — not a format change.

### Fixed

- **Interaction moments now flush the lazy checkpoints.** `record_chat_turn`,
  `player_action`, `player_buy`, and `close_conversation` write the needs / reflection
  watermark / clock checkpoints on the spot instead of waiting for day rollover or
  shutdown. A crash (or a live export) right after a player interaction no longer loses
  the quiet-tail clock or the day's needs drift for that moment.
- **Orphaned conversations are recovered at open.** A crash between
  `start_conversation` and the close inside `record_chat_turn` used to leave the
  conversation `open` forever (embedded hosts without the idle reaper never closed it,
  and its one `conversation` event was never emitted). `World.open` now sweeps all open
  conversations — messages were already durable, so the summary and event are generated
  late instead of lost.

## [1.0.1] — 2026-07-23

Same db format (**1**) and package format (**1**) as 1.0.0. Worlds built by 1.0.0 open
unchanged.

### Fixed

- **Reopening a world registered the bundled demo cast instead of its own agents**
  ([#1]). The roster was built from the seed file on every boot, and the seed file
  defaults to the bundled `world_seed.json` when `--seed` is absent. So a database that a
  host seeded and shipped came back up running 苏晚夏 / 陆知遥 / 沈亦柔 — the world's own
  agents never ticked again, while the three strangers appended `narrative`,
  `state_change`, and `agent_action` events to it permanently. Nothing warned; the output
  looked healthy. This hit the documented workflow (`simulate --seed … --ticks 0`, then
  `run`). A non-empty database is now the authority on its own cast, rebuilt from its
  genesis `agent_join` events.

  The related `--seed was NOT applied` warning was also misleading: passing `--seed` was
  the only way to get the right cast, so the one workaround that worked told you that you
  had done it wrong.

- **A db-format mismatch surfaced as an uncaught traceback** ([#5]). `DBFormatError` is
  the outcome the whole version model exists to produce, and it was the only one of the
  three user-facing precondition failures the CLI did not catch. It now prints one line
  and exits 2, like `BeatScriptError` and `WorldSeedError`. The message also names the
  engine to install (`install a 2.x engine to open this world`) rather than leaving the
  reader to derive it from the version policy.

### Added

- `anima-world --version`, reporting the engine version plus the db and package format
  versions ([#5]). For an engine whose headline contract is "the version *is* the
  compatibility promise", self-report should not have been missing.
- Event `payload` field reference in [docs/REFERENCE.md](docs/REFERENCE.md) §2.1, with a
  stability note ([#7]). Hosts are told to read the `events` table directly for full
  history; until now they had to reverse-engineer the fields.
- Tests pinning three cross-repo contracts that previously held by accident ([#2]):
  `__init__.py` / `db.py` importing only the standard library (version identification
  runs in `--no-deps` virtualenvs), the db-format constants being externally read at
  their import paths, and `simulate --ticks 0` meaning "initialize and stop". All three
  are now documented in [CONTRIBUTING.md](CONTRIBUTING.md).

### Changed

- `Development Status` classifier from Alpha to Beta ([#7]) — it contradicted the
  add-only API promise and the mechanically-enforced version contract.
- [docs/ROADMAP.md](docs/ROADMAP.md) now says up front that its v2.0–v5.0 predictions
  shipped inside 1.0.0 ([#7]). It was written before the release and read as though
  memory 2.0 were still unimplemented.

[#1]: https://github.com/aubrey-anima/core/issues/1
[#2]: https://github.com/aubrey-anima/core/issues/2
[#5]: https://github.com/aubrey-anima/core/issues/5
[#7]: https://github.com/aubrey-anima/core/issues/7

## [1.0.0] — 2026-07-23

First public release. db format **1**, package format **1**.

Everything before this release lives in git history rather than here; the engine went
through several db-format generations during development (memory 2.0, needs, economy,
social each landed as their own format bump) and they were collapsed into a single
format 1 for the first release. Those worlds never left the machines they were built on,
so there is nothing to migrate.

### The engine

- **Event-sourced world core.** An append-only event log is the only source of truth.
  Balances, relationships, locations, and the narrative log are projections of it. There
  is no snapshot table — an earlier one was removed because it wrote back drifted
  balances.
- **Tick-driven scheduler** with the system's single `RLock`, guarding the world clock,
  the projection, and the mailbox.
- **Behavior-tree agents** with an urgency band, a free-time planner, and an action table
  that lives in the database rather than in code.
- **LLM off the tick thread.** Narration, planning, and relationship judging run on
  separate thread pools; the client is injected. A world with no API key runs on
  templates instead of stalling.
- **`World` facade** (`anima_world.api`) — open, drive the clock, read state, chat,
  record turns, move players, hot-edit config. This is the interface host applications
  depend on, and it is add-only from here.
- **Chat subsystem decoupled from the event core.** A whole session emits exactly one
  world event, at close. The world receives only the current turn's bounded history; the
  full transcript stays in the host application.

### Subsystems

- **Memory 2.0** (always on) — retrieval scored on relevance × recency × importance,
  periodic reflection that writes higher-order memories, and a forgetting curve.
- **Needs** (`needs.enabled`, default off) — `energy` / `hunger` / `social` decay per tick
  and drive the behavior tree's urgency band. Checkpointed at day boundaries and on close.
- **Economy** (`economy.enabled`, default off) — items, money, shops, wages, price drift.
  The ledger is a projection of `payment` events.
- **Social** (`social.enabled`, default off) — gossip that propagates second-hand with
  per-hop confidence decay, and emergent cliques. Three-axis relationships are always on.

### Distribution

- **`.cyberworld` packages** — export a world as a template (seed only, builds itself on
  first boot) or a snapshot (a database that has already lived), and import either.
- **CLI**: `start` (guided create + run), `doctor` (health check including a real LLM
  call), `config` (encrypted secrets, masked on read), `run` (foreground host, no
  onboarding), `simulate` (headless fast-forward), `world` (package export/import).
- **Encrypted secrets at rest.** `llm.api_key` is Fernet-encrypted; the key material
  lives in `<db>.key` and must travel with the database.

### Fixed before release

- **The world clock was not persisted.** It was restored as `max(event timestamp)`, so
  every stretch of ticks that produced no event — most of the night — was silently
  discarded on close. A world reported at tick 350 reopened at 320, and the deficit was
  permanent. Now checkpointed to `db_meta` alongside the other data-plane state.
- **An explicitly named seed file that could not be read degraded silently** to the
  built-in demo world. Because a seed is read once into an empty database, a typo in
  `--seed` produced the wrong world permanently. Authored seeds now raise `WorldSeedError`
  with per-field detail; only the bundled seed still falls back.
- **`config --db-path` only worked before the subcommand**, unlike every other command,
  and failed with a bare top-level usage error. Both positions now work.
- **`doctor`'s fix hint omitted `--db-path`**, so a user with a world at a custom path who
  copied the suggested command created a second, empty world at the default path and
  wrote the key into that one.
- Two gossip bugs: a dead branch and a non-reproducible dice roll.

### Removed before release

- **The HTTP layer.** Three REST API groups and membership-claim authentication were
  removed when the engine became a pure library. Network exposure is the host
  application's job. The old protocol is in git history before `e7e3188`.
- **Authoring code.** Authoring moved to a separate desktop application, because a world
  file is pinned to the engine version that produced it and the tool has to hold several
  versions at once.
- The `story` subcommand, an M2-era leftover that no documentation mentioned.

[Unreleased]: https://github.com/aubrey-anima/core/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/aubrey-anima/core/releases/tag/v1.2.0
[1.1.1]: https://github.com/aubrey-anima/core/releases/tag/v1.1.1
[1.1.0]: https://github.com/aubrey-anima/core/releases/tag/v1.1.0
[1.0.2]: https://github.com/aubrey-anima/core/releases/tag/v1.0.2
[1.0.1]: https://github.com/aubrey-anima/core/releases/tag/v1.0.1
[1.0.0]: https://github.com/aubrey-anima/core/releases/tag/v1.0.0
