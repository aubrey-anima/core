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

⚠️ **A released heading here does not mean a released artifact.** 3.4.0 through 3.6.0 were
never tagged and never went to PyPI — they shipped as container images only. **3.7.0 is the
first version since 1.4.0 meant to actually reach the index** (2026-08-26, decision D2), and
therefore the first AGPL-3.0-or-later release: everything on PyPI up to and including 1.4.0
is Apache-2.0. ⚠️ **The tag is the trigger, and this file is written before it is pulled** —
until `v3.7.0` is pushed, the newest thing on the index is still **1.4.0** (2026-08-04).
A section under a version heading means "this version's engine, as built from this repo";
whether that build ever left the building is a separate question, answered in
`CLAUDE.md` §当前状态.
A change parked under `[Unreleased]` used to be forbidden here — the rationale being that
it is a change that already shipped while telling the reader it hadn't (see 第五轮 below,
which is where that particular lie got caught). ⚠️ **2026-08-25 更正:那句禁令和这份文件
的正文自相矛盾了整整一轮** —— 清欠账那一单在下面开了一个 `## [Unreleased]`,而这一段
还写着"这里刻意没有 `[Unreleased]`"。**留着两句打架的话,比两句里哪一句都糟**:下一个人
按这一段去找,会在版本标题下面找一个根本不在那儿的条目。现在的规矩是那一单实际在用的:
**没有版本号可挂的改动**(这一轮改的全是判据与文档,`anima_world/` 一个字节没动,
`__version__` 一格没抬)进 `[Unreleased]`,**定版时整段并进那个版本标题**
(⚠️ **2026-08-26 这条规矩第一次真的被执行了**:发 3.7.0 时那一段整个并进了
`## [3.7.0]` 底下的「发版前那一轮」,所以**此刻这份文件里没有 `[Unreleased]`**
—— 上面那句"下面开了一个"讲的是 08-25 到 08-26 之间那一段时间);
禁令原本要挡的那件事由另一句话挡住 —— 见下面那两段:**版本标题说的是这个仓库有什么,
从来不是你的镜像有什么。**

⚠️ **That lie has a mirror image, and the mirror image is the one that is live today.**
The rationale above used to end "…and everything under a version heading is in the running
image the day it's built" — false on the day it was written: `## [3.6.0]` collects review
rounds written *after* the only `anima-world:3.6.0` image was built. A version heading says
**what this repo contains**, never **what your image contains**, and the two drift silently
in both directions.

**When this file is ahead of an image, the only judge is to look inside the image — never
the version number.** Tag, LABEL and `anima_world.__version__` are all set by whoever built
it, so all three can agree while the bytes differ. Pick a symbol a round claims to have
introduced and ask the image whether it has it:

```console
$ docker run --rm --entrypoint python anima-world:3.6.0 \
    -c "import inspect, anima_world, anima_world.api as m; \
        print(anima_world.__version__, inspect.getsource(m).count('_where_unknown_line'))"
3.6.0 0     # 2026-08-20 实跑(image c8151d581dfd);同日 repo 里同一个数是 6
```

`anima-world contract --json` answers the same kind of question for the storage contract.
(Which builds ever left the building: same place as above — `CLAUDE.md` §当前状态.)

## [3.8.0] —— 插件系统第 0 期:先把今天就错着的那几件补上 (2026-08-26)

任务单 `docs/任务单/2026-08-26-插件系统-第0期.md`,设计稿 `docs/设计-插件系统.md` §10 第 0 期。
**第 0 期**做的是三件"插件系统迟早要踩、而今天就已经错着"的事,外加一张触发器要订
的表;**第 1 期**(同一节往下)落地 `type:"plugin"` 记录本身。
⚠️ 上一版这里写着"这一期不加任何新的作者记录类型" —— **那句只对第 0 期成立**,
第 1 期加了**作者层第十三个段 `plugin`**,所以带它的包 `engine_min` 必须写 3.8.0。

⚠️ **为什么抬到 3.8.0 而不是挂在 `[Unreleased]` 下面**:`3.7.0` 2026-08-26 **真的上了
PyPI**,而这一轮往 `contract --json` 里加了两段、往抹除回执里加了一格。不抬版号的话,
「3.7.0」这个名字下就有两支能力不同的引擎 —— 而消费方正是拿 `contract --json` 去做
能力探测的,两支引擎对同一个问题给不同答案,那正是 3.3.0 定版时写下的那笔账。
**已发布世界的 `engine_min` 一格没抬**(这一版没加作者层字段,老包照旧开得起来);
动的只有橱窗自己的封皮(`test_flagship_seed` 的等号闸)。

### 第二波 ⑥⑦:一道闸和一句话(2026-08-27)

**⑥ 插件族的错要说插件自己的理由** —— 两条具体的错在 ① 与上一轮 C4 已经修了,
这一条加的是**闸**:五种插件写法的错,拒绝语里不许出现作者层那两句话
(「跨实体的相互作用」/「在 kinds 里写一条」),而且要提到插件自己。
**一句拒绝语说的是哪一层的病,不是措辞问题 —— 它决定作者去改哪一行。**

**⑦ 脚本关不掉一场对话:不是缺出口,是叫错了名字。** `chat()` **有意不建会话行**
(它只流式吐字,完整转录归宿主),所以 `conversations()` 答 `[]` 是对的;
**建行 + 关行 + 发 `conversation` 事件的是 `record_chat_turn()`**,那才是无宿主那条路
的提交口,而 `close_conversation(conversation_id)` 是给自己管着会话的宿主用的。
整条链真敲过(`chat` → `record_chat_turn` → 事件 → 订它的触发器落笔)。
**`World` 门面没加方法** —— 这一条要的是一句话;而那句话写进了
`conversations()` / `close_conversation()` 两个 docstring,**不只写在回执板上**:
下一个人是在 IDE 里读到它的。

### 第二波 ⑤:`doctor` 里终于有插件这一层(2026-08-27)

二十行体检里**一行都没有**插件。而 `plugin list` 只报声明面的计数 —— 调度台撞的那个
病(一条 `when` 恒为假的触发器)**在声明面上完全正常**。

新的一节答的是「**有没有发生过**」:每个插件一行,报**写过的事实个数**
(量表那一行的 `updated_tick` —— 规律与触发器写一个数**不发事件**,日志里查不到
它们,这是唯一的凭据)与**它发出的事件条数**(`<插件>.<type>`,顺着 doctor 那趟
replay 数,零额外成本)。一条声明了规律/触发器却一次没动过世界的插件会被喊一声。

三条边界都写进了那一节:`when` 为假几次**从外面看不见**(进程内读数
`World.trigger_stats()`,最后一行指路 —— **说出来,而不是假装查过了**)· 只扫世界
与名册那几个 owner(体检不该按世界的大小收费)· **不进退出码、出厂插件不喊**
(刚建好的世界什么都还没发生过,而一条永远红的检查等于没有这条检查)。

### 第二波 ③④:契约说收而引擎不收的一格,以及一条只覆盖一半写法的 lint(2026-08-27)

**③** 规律的 `emit` 写了 `text` 没写 `importance` —— **引擎硬拒,而契约的
`emit_required_keys` 只列 `type`/`when`**:创作台判 🟢、引擎判 🔴。
「契约说收、引擎不收」比缺一格更贵:照契约做出来的包开不了机。
补一格 `emit_key_requires`(`{"text": ["importance"]}`,**和引擎判的是同一份常量**,
不是抄的第二份);⚠️ 它只管规律那一层 —— 触发器的 `emit` 根本没有 `importance`,
那儿的 `text` 是载荷里的一句话、不进记忆,套过去就是假红。契约 71 → 72。

**④** 「常数步长没用 `dt`」那句提醒**不覆盖插件的规律**:`onair.淡忘`
(`every days:1`,`人气 - 1`)和作者层那条 `梅雨` 是同一种写法,引擎对后者说了三遍、
对前者一字不提。**一条只覆盖一半写法的 lint 比没有它更难查** —— 作者会把
「引擎没说」读成「我这条没问题」。两扇门与开机三处都补上了,共用 `drift_warnings`
同一个函数;⚠️ 出厂插件滤掉(它们的写法作者改不动)。

### 第二波 ②:触发器的 `when` 读不到 `world_*`,而且它不说话(2026-08-27)

`when: ["world_雨势 > 0.3"]`,雨势 0.8,**一天十六条 `travel` 一次没响**;去掉 `when`
当场就响。`world_雨势` 在**作者层规律**里是合法写法,而触发器这条路上它既不在命名
空间里、装载期也不拒 —— **两条路对同一个写法给两个答案,而错的那一边不说话。**

修法两半,而**第二半才是真正值钱的那一半**:

- 触发器的 `when` / `set` 和规律一样读得到 `world_<量名>`。⚠️ **有人读才读**
  (和 `stocks.evaluate_due` 那一段逐字同一条判断):没有一条触发器写 `world_x` 时,
  这一趟一次 `HGETALL` 都不发,闸单独钉着。
- 🆕 **`World.trigger_stats()`**:每条触发器一行六个数(`matched` / `no_bearer` /
  `no_facts` / `when_false` / `written` / `emitted` / `errors`)。
  **一条 `when` 恒为假的触发器,和一条根本没被叫到的触发器,在屏幕上长得一模一样**
  —— 上面那个病只能靠"把 `when` 去掉再跑一遍"这种对照实验才发现,就是因为这个。
  六个数分开记,是因为它们指向**六种不同的修法**。
  ⚠️ 一条一次都没被叫到的触发器**根本不在这张表里**,那本身就是一个读数。

回执 `docs/FOR-STUDIO.md` §3.53,`docs/REFERENCE.md` §3.4 那张 `World` 方法表。

### 第二波 ①:插件的动词改不动它自己的事实(2026-08-27,调度台在真世界上试出来的)

`costs: {"tape.精神": "me_tape.精神 - 10"}` —— `tape.精神` 是这个插件**自己**声明的
事实,而两扇门与开机一起拒,拒绝语是作者层规律那句「跨实体的相互作用(挖矿让矿脉
减少)v1 还表达不了」。**那句话和插件一点关系都没有**,而下场是「施法耗灵力」这种
最基本的写法写不出来。

**修法是把闸挪到看得见的那一层**,不是把闸拆了:本体那一层对带命名空间的名字一律
放行(它手上没有插件的声明,判了就是恒为假红),真正的判在 `plugins._parse_verb`
—— 那儿两份声明都在手上。三条:只许自己的 · 必须在顶层 `facts` 里 · 挂对身子
(`costs` 扣施动者、`set` 写目标)。⚠️ **闸也钉了重开那一趟**:本体是从 `:kinds`
那一行重新解析的(`RedisOntologyStore.load`),genesis 放行而重开拒的话,
世界会在第二天打不开。

**裁决:写只写得到自己的命名空间,`costs` 也不例外。** 设计稿 §4.2 那个
`costs: {"economy.coins": "economy.coins - 500"}` 的例子写在这条边界定下来之前,
以边界为准。三条理由,第二条是硬的:三条写路必须给同一个答案 ·
🔴 **别人的事实可能是 `projected`,而 `economy.coins` 今天正是** —— 量表里那个数只是
物化视图,扣下去**重开一次就回来了,零报错**,而"别人的是不是投影"不是写它的人
管得着的 · 花钱有它自己的路(`payment` → 投影)。要拦一个买不起的人:`reads` 读 +
`requires` 挡。**读别人的可以,写别人的不行。** 同源的一条:`projected` 的事实
**一律写不得,自己的也不行**。

契约纯增量 **69 → 71**:`namespaced_write_scope` / `namespaced_write_gloss`。
回执 `docs/FOR-STUDIO.md` §3.52,`docs/REFERENCE.md` §10.8 + §10.10。

### 三视角验收排回来的九条:一个真 bug、两句写错的症状、两条没牙的闸(2026-08-27)

**A1 —— 一条在最该响的时候闭嘴的警告。** `uncreatable_edges` 把 `link` 和 `transfer`
都算成"造得出边",而 `apply_edge_effect` 的 `transfer` 那一支是 `of_src` **把已有的行
搬个家**:空表上一行都取不到、当场返回 False。**于是一个只有 `transfer` 动词的插件
边表永远是空的,而这句新警告对它恰好不响。** 现在只算 `link`。

**C1/C2 —— 一句写错的症状,和它底下一个真的静默 bug。** 我在三处(FOR-STUDIO /
REFERENCE / CHANGELOG)写着「照 `menpai.声望` 写规律,那个名字恒等于 0、规律照跑、
日志干净」。**两种情形没有一种是它说的那样**(验收 C 逐条敲的):规律**读**它
→ 三扇门当场红;规律**只写**它 → **量表里并排住下两个量**(`声望` 停在默认值没人
更新、`menpai.声望` 每 tick 在涨而没有一处读它),`validate` 说绿、零 warning、
日志零字,`rule_stats()` 报的是 written。**错的从来不是"那个名字等于 0",是那条规律
更新了一个没人读的量。**

后一半是真 bug,已收:`bad_output_name` 那一支只查了前缀(`menpai.<任何字>` 一律放行),
现在插件的规律写一个**顶层 `facts` 里没有**的事实当场拒,并把该怎么写说出来。
⚠️ **这不是加一道新闸,是把同一个插件里两种写法的两种下场抹平** —— 同一条 `set`
写在触发器里一直是当场拒的(`_parse_trigger` 早就查 `local not in facts`),
写在规律里却静默丢,而作者读不出为什么。
**第二种写法**(文档两处都补上了):要让规律改一个挂在插件种类实例身上的量,
声明成**顶层 `facts` + `"bearer": "entity:<你的种类>"`** —— 名字就带上命名空间,
规律写得动读得到,值照旧住在那个实例自己的量表里(实跑:3 tick 后 `menpai.香火 = 3.0`,
同一张表上的 `声望` 一动没动)。

**A2/A3 —— 一条自称反向闸而两头都不看的断言。** `trigger_bearer_keys` 那条用例断的是
一个**硬写的四元集合字面量**:验收 A 把 `actor`/`player` 加进引擎的受理集合、契约不动,
**它照样绿**。病根是那份受理名单是写在 `if` 里的字面量,没有名字。现在它是一份常量
(`TRIGGER_BEARER_FORMS`),用例 import 它逐格比(**试过牙**:加一个词当场红),
而 `for_each.node` 的报错也改印**真受理的那四种** —— 从前印的是 `BEARER_FORMS`
那六个,点着两个它自己会拒的取值。

**C4 —— 一句字面上为真、而把人指向错误方向的报错。** 插件坏了 → 它声明的种类一行都
没编译出来 → 实例那条 `entity` 记录收到「引用不到 —— 没有名叫 'menpai.sect' 的 kind」。
现在那一摞后面跟一句「这几条多半是连带的,先修插件那几条」。**只加一句,不去猜哪几条
是连带的** —— 猜错了比不猜更贵。

**其余三条**:C3 那条用例的 docstring 说「恒等于 0」而断言只覆盖创世那一刻 → 换成一条
**真跑若干 tick** 的红检 · A5 那条编辑包用例只断"两边一致"没断一致到哪一句上 → 补 needle
(只断一致的话,两扇门**一起**答错还是绿的)· A4 `except (PluginError, Exception)`
两处 → 收成 `except PluginError`(第一项冗余,第二项会连将来的真 bug 一起吞,
而那两处正是离线两扇门共用的那一段)。

**B1 —— 一句说过头的话**,五处同句:「契约纯增量,**一格取值都没改**」。
七格确实是纯增量,**而 `subscribable_events.travel.note` 那段老 gloss 的文字改了**
(说法更正,不是能力变更)。五处(FOR-STUDIO ×3、CHANGELOG ×1、总图 ×1)都改成
「七格纯增量,老格取值零变更,一处 `travel.note` gloss 更正」。

### `travel.parties` 那一格骗过人 —— 而错的不是代码,是引擎自己写的两句话(2026-08-27)

`subscribable_events.travel` 报 `parties: ["player_id"]`,gloss 又写着「角色与玩家
共用这一条」;而白名单那张表的说明当时写着「`parties` …… 决定触发器的 `for_each`
能不能对得上人」。**三句话并排读会推出一个结论:角色出发时触发器对不上人。**

**那个结论是错的。** 取人那条路(`Scheduler._trigger_bearer`)一个字都不读
`parties`:`agent` 取**事件顶层的 `who`**(经 `stock_owner_of`)、`location` 取顶层
`loc`、`entity:<kind>` 取载荷里的 `target`/`entity`、`world` 是常量。而 `travel`
两条路的顶层 `who` 都写(角色 `Scheduler._start_journey`,玩家 `World.player_walk`)
—— **两半都对得上**,实跑证过:一个订 `travel` 的触发器,角色出发让她那个事实 +1,
玩家出发让他那个 +1。

所以这一轮**没改行为,改的是三处说法 + 加一格**:

- `events.SUBSCRIBABLE_EVENTS` 的表头说明(`parties` 是「这条事件里还写着谁」,
  不是「落在谁头上」)、`travel` 那条的 `note`、`Scheduler._fire_trigger` 的
  docstring(它此前写着「白名单每条都标了 `parties` —— 那正是这一格存在的理由」,
  而这条路根本不读它);
- 🆕 **契约多两格**(纯增量,67 → 69):`trigger_bearer_keys`(`for_each.node` →
  从事件哪一格取人)与 `trigger_bearer_gloss`。**一格「从哪儿取」比一段「它决定
  ……」值钱得多** —— 后者要人读、要人记,而它正好被记错了一轮
  (`edge_end_prefixes` 那条先例的第二次应用)。反向闸钉着那张表是全的:
  `for_each.node` 收几种形式,这一格就得有几行。

⚠️ 顺带把一句没量过的话标准了:表头写着顶层 `who`「**每条都有**」——
拿橱窗跑 300 tick 实测,**跑到的六种全都带**(`agent_join` / `entity_interaction` /
`item_transfer` / `payment` / `state_change` / `travel`),另外四种那一趟没跑到。
现在那句话带着日期与「量过几种」,而不是一句听起来像全称的断言。

回执 `docs/FOR-STUDIO.md` §3.51,`docs/REFERENCE.md` §10.5 + §10.8。

### 作者层里种得下什么:**实例种得下,边种不下**(2026-08-27,创作台问的那两半)

创作台做「组织」模板(门派 + `member_of` + 初始成员)时问了两件事,答案不一样,
而两个答案都是**量出来的**(四条路各真开一次机,不是读代码推的):

- **(b) 插件种类的实例:种得下**,走的就是作者层已有的 `entity` 记录,
  id 里那个种类名写全名(`menpai.sect:青云门`)。没有新段、没有新语法 ——
  插件的种类编译成普普通通的本体种类之后,这件事本来就是免费的。
  🔴 **而有一格差点被漏掉**:种类上的事实,量名**不带命名空间**(`声望`,
  不是 `menpai.声望`)—— 它住在那个实例自己的量表里,顶层 `facts` 那一族才带
  (那些跨插件共用一张表)。⚠️ **这里第一版写的「那个名字恒等于 0、规律照跑、
  日志干净」是错的**,当天验收 C 逐条敲掉了:读它当场三扇门全红,只写它则是
  **量表里并排住下两个量**(见下面那一节)。
- **(a) 一条边:种不下**,而这是一个**真缺口,不是一条设计判断**。作者层十三个段
  里没有边,写一条 `type: "edge"` 的记录是开不了机的硬失败;边今天只有两条来路,
  都是运行期的(动词的 `effects` 与触发器的 `effects`)。于是「门派的初始成员」
  这种**创世态**的东西写不出来 —— 而内核自己的关系图**有**作者层入口
  (`relation` 段),这处不对称记进契约等开单。
  ⚠️ 顺带量掉一条看上去可行的替代写法:**订 `agent_join` 的触发器接不到创世那一批**
  (那几条事件发生在插件装上之前;实测 tick 3 之后边表仍是 0 条)。
  写成那样对后来加入的人有效、对创世那一批无效,而两者的差别在屏幕上看不出来。

**契约纯增量,62 → 67 格**:`kind_instance_section` /
`kind_instance_id_syntax` / `kind_instance_gloss` / `edge_node_id_forms` /
`deferred_author_sections`。老引擎上是**整格缺席**(不是 `null`)。

- `deferred_author_sections` 和 `deferred_fact_shapes` **逐字同构**:这一版收不下的
  作者层段,**连理由一起报**。判据是「这一格里还有没有 `edge`」—— 哪天种得下了,
  它从这一格里消失,消费方不必去比版本号。
- `edge_node_id_forms` 是**问出来的一格**:写 `link` 的两端要拼节点 id,而整份契约
  此前一格都没说过它的形状,创作台只能照文档抄 —— 而抄来的镜像会烂
  (`visibilities` 那一格刚吃过同一种亏)。🔴 最容易写错的是玩家:
  节点 id 是 `agent:player:<id>` 而不是 `player:<id>`(玩家和角色同一个量表
  命名空间)。**闸里拿它和 `stock_owner_of` 对了一遍**,不是抄一份平行清单。

**外加一句会说话的警告**:声明了一种边而这份文件里**没有一个动词或触发器造得出
它**,三扇门一起说(两扇离线门进 `warnings`,开机进 `logger.warning`)。
**是警告不是错误** —— 开机是权威,它收得下;可**没有一处会说话**才是病:
那张表永远 0 条,而作者分不出「造不出来」和「还没有人连上」。对照组钉在同一条用例里:
给它一个带 `link` 的动词,这句话就不许再响(**一句总在响的警告等于没有警告**)。

回执 `docs/FOR-STUDIO.md` §3.50,`docs/REFERENCE.md` §10.8 + §10.16。

### 插件那一族进了「三扇门说同一句话」那份夹具(2026-08-27,创作台诉求「欠的第一条」)

创作台 `docs/引擎接口诉求-插件.md` 欠的第一条问的是:一条越界写、一条没声明的
`reads`,`validate world` 到底答什么?REFERENCE §10.2 承诺三条边界违反了**都是
开不了机** —— 而那句承诺对消费方成立,靠的是**离线那两扇门也这么说**(创作台出包
前那道闸、运维台判包的一次性容器,读的都是它们)。

**做法是把插件这一族逐条点进 `tests/test_validate_matches_boot.py`**:越界写 /
`reads` 没声明 / 依赖缺失 / 依赖成环 / 规律的 `emit` 写了裸名 / 动词多写一个键 /
`projected` 挂在插件自己的种类上 / 动词的 target 是人 / 动词的 target 指着一个
这个世界里没有的种类,外加 `strict_levels` 那**十一层**各一条,加一条对照组
(写对的插件三条路都放行)与一条**反向闸**(`STRICT_LEVELS` 加一层而这儿没跟上,
当场红)。

🔴 **写完当场逮到一条真的假绿,而它正是这一族最贵的那种错法。**
`verbs: {"施法": {"target": "swrd"}}`(种类名少了个字母):**离线两扇门答
`loadable: true`,真开机退 1**。病根是那道闸只住在开机那一侧
(`__main__._merge_plugin_kinds`),而离线那条路上根本没有它 —— §3.28 治过的
同一族,这一次是插件带进来的。**修法是把它搬成一份判断**:
`plugins.borrowed_kind_errors()`,开机与 `world_plugin_errors` 调的是同一个函数。

⚠️ **连带改对一处分工**:`authored_layer_errors` 从前把「并过插件 `kinds` 行」的
那一份喂给**所有**检查器,而 `compile_kind_rows` 会替每一个借来的名字造一行 ——
于是新加的这道闸拿到的世界里"那个种类存在",它永远查不出东西来
(**一盏什么都没查的绿灯**)。现在并过的那份只喂给本体那道闸(它问的是
「这些引用解析得开吗」,少了插件的种类会对一个开得起来的世界报假红),
检查器表拿的是**作者写的原件**。

顺带一格:`sources` 那一层的「不认识的键」报错从前**不说该去问契约的哪一格**,
而另外十层都说。改成走共用的 `unknown_keys()` —— 「每层的名单都进契约」这条纪律
的另一半,正是**让报错自己点名那一格**,否则读的人只能照文档记一份会烂的清单。

⚠️ **一条顺手量出来的事实,写给创作台**(不是这一轮改的,是它一直如此):
一份**只含插件**的编辑包,若它的动词挂在**目标世界里已有**的种类上,**开机是拒的**
—— `_merge_plugin_kinds` 手上只有这份文件的 `kinds`。替代写法是**同一份编辑包里
把那个 `kind` 再声明一遍**(作者层「只填缺不覆盖」,不会把世界跑出来的现在倒带)。
两半都真敲过,钉在
`test_编辑包里的插件_动词借世界里已有的种类_三扇门说同一句话`。回执 `docs/FOR-STUDIO.md` §3.49。

### 全量跑出来那一条红:**是老闸逮的,是真的,而且是我犯的**(2026-08-27)

`test_屏幕上不许出现裸markdown星号` 当场逮住新加的 `--clear` 那句 `help=` 里一对
裸 `**` —— 屏幕上它就是两个星号。**这正是那道闸存在的理由**,一次就还本。

⚠️ **而它逮到的只是我犯的第一处。** 那道闸自己的 docstring 写明了盲区:它只按 AST
扫 `print()` 的实参与 `help=` / `description=` / `epilog=`,**先攒进列表、走 logger、
或者走异常**的那几条路它一条都看不见。这一轮新加的用户可见文本里恰好有三处是那种
形状,手工查出来一起改了:

- `world check` / `validate world` 对纯状态层包那句新 warning(攒进 `warnings` 列表);
- D32 那句拒绝 —— 它上一版只进 `logger.warning`,**这一轮升成了印在人终端上的一句话**,
  于是它的排版规矩也跟着换了。**把一句日志升成一句人话,这一步最容易漏。**
- `set_world_setting` 的两句 `ValueError`(CLI 原样打到 stderr)。

判据也换了:**别再数源码里的星号,去问屏幕**。

🔴 **而第一版这条判据自己就漏了一格,是 B 视角验收挑出来的**(同日,当场修):
它只敲了**三条**出口(两个 `--help` + `world import` 那句拒绝)—— 而剩下的裸星号
恰好住在**没敲的那一条**上:`--edit` 的运行时 warning,不带 `--json` 跑就原样印
`**跨引用**没查`。**一条判据只覆盖我记得去敲的地方,和那道闸只覆盖它认得的语法,
是同一个毛病**;而我上面刚写完"它逮到的只是第一处",转手就把判据定窄了。

现在两头都扩了:

- **判据**从"三条出口"扩到 **`world check` / `validate world` / `world inspect` /
  `doctor` × 有没有 `--edit` × 四种包**共 **11 条**真出口,逐条 `grep -c '\*\*'` 答 0;
- **那道闸**从"只认 `print()` 实参与 `help=`"扩出两条路:`warnings.append(...)`,
  以及**名字里带 `warning` 的函数 `return` 出去的字符串**(后者是
  `redis_state.durability_warning` 逼出来的 —— `doctor` 把它塞进一个 f-string 再
  `print`,于是扫实参那条只看得见一个 `{warning}` 变量)。

🔴 **而这句账要写准,是 A 视角验收要求的更正**:上一版我写"手工查了三条路一起改",
读起来像"那三条路已经收干净了"。**不是。** 那三条路**闸本身仍然全盲**,改掉的只是
当时那几处**实例**。现在树里还剩(逐条数出来的,不是估的):

| 盲区 | 现存带 `**` 的字符串 | 闸看得见吗 |
|---|---|---|
| `logger.*(...)` | **5** | ❌ 全盲 |
| `raise XxxError(...)` | **1** | ❌ 全盲 |
| `xxx.append(...)` | **27** | ⚠️ 只认 `warn*` 那个变量名(现存命中 **0**) |

它们**绝大多数不上屏**(内部列表、日志级别低),但**没有一条被逐条过筛过**。
所以这一轮扩面的真实收益是:**下一次往 `warnings` 里写裸星号会当场红**,
而 logger / raise 那两条路**还得靠手**。

一并改掉的四句(**都是既有的,不是这一单新写的**):`world check` 与
`validate world` 的 `--edit` warning(两处)· `_edit_dropped_quantity_gap_warnings`
那句 · `durability_warning`(`doctor` 真在印它)· `ontology` 里 `agent` 那条
`OntologyError`(D30 让 `world check` 开始报状态层错误,它因此**新上了这条路**)。

⚠️ **账要说全:全仓字符串常量里带 `**` 的还有 605 处**,绝大多数是 docstring 与
注释(不上屏),但**这个数没有被逐条过筛** —— 上面那 11 条出口是干净的,
"屏幕上再也没有星号了"这句话**仍然不成立**,别那么读。

### 三视角验收的收尾四件(2026-08-27)

1. 🔴 **`_warn_if_live` 那句话的后半句按出口分岔了。** 它原先写死成 config 的语义
   (「那个进程不会重读,要下次重启才生效」),而 `world setting` 挂上来之后它就是
   **一句假话**:`:prompts` 没有进程内缓存,`ChatService` 每次拼提示词都现读,
   **热改当场生效**。同一句"有人在跑",对配置和对世界观意味着**相反**的两件事,
   写死其中一句就是对另一扇门撒谎。现在是 `_LIVE_EFFECTS` 一张表,加一扇门就加一行。
2. 一条**比判据宽的注释**收窄了:`_say()` 写着"stderr 也要干净"而断言并不在那儿
   (它在 `first_write`,且只钉第一次 —— `owner_pid` 关世界时不清)。
   **一句比判据宽的注释,和一盏假绿灯是同一件事。**
3. 🔴 **FOR-STUDIO §3.45 末尾那句给 platform 的话删掉了,因为它是我没核就下的判断。**
   我写的是"你 `100d6cc` 接的是上一版那行,请刷一次" —— C 核过之后:platform
   **从来没拿 `unchecked_layers` 当闸**,两格也早在记账。照那句去改,改的会是
   **一份没坏的代码**。上一轮我刚写完"一条推迟的理由应该在推迟那一刻就量",
   转手就在回执里下了一个没量过的判断。
4. 🆕 **`contract --json` 的 `seed` 段多一格 `visibilities`**(创作台修镜像漂移时
   立的诉求)。量声明里的 `visibility` 只认五个词,而**整份契约此前一格都没列过
   它们** —— 创作台只能手抄,而那正是 `kind_keys` 当初那条假红的形状:抄漏一个词,
   一份完全合法的声明被判红,引擎这侧一声不吭。照 `plugins.rule_selectors` 的先例,
   **和引擎读同一份常量**(`perception.VISIBILITIES`),不是一份平行清单 ——
   写死一份平行的只是把漂移搬个家。**次序承重**(从窄到宽);
   ⚠️「没声明 = 感知不到」**不在表里**,它是缺席的语义不是第六档。
   闸钉的是**逐位相等**,试过牙:改成手抄的四档当场红。创作台那份手抄可以销了。

### 世界观改得动一个**跑着的**世界了(2026-08-27,收件箱 D4)

同一张任务单的第二件。**这一格欠得比角色卡和地点图都久**,而它的代价也最重。

`world_setting` 是作者层的一个段,而它**只在创世那一刻**落进 `:prompts` 的
`world.setting`(`_seed_world_setting` 被 `if not persisted` 把着门)。于是一个
**已经建好、有人在玩**的世界改不了自己的世界观 —— 连拿一份改过的 `.cyberworld` 走
`--world-file` 都不行,**那条路对这一段不生效,而且不报错**。

所以创作台唯一的办法是 `world drop --yes` **把整个世界抹掉重建**
(`anima_studio/infra/workspace.py::_drop_world`):玩家的记忆、关系、事件日志、
跑了几十个世界日的历史,全为了改一段话陪葬。**这条路是引擎逼出来的。**

**新出口**(读写同一条命令,**什么都不给就是只读** —— 和 `world drop` 不带 `--yes`
只数同一条:一条会改东西的命令,它的默认必须是安全的那一边):

```bash
anima-world world setting --world-id w                      # 它现在是什么
anima-world world setting --world-id w --set "旧港区,常年下雨。"
anima-world world setting --world-id w --set-file setting.txt   # `-` = 标准输入
anima-world world setting --world-id w --clear              # 回落到引擎内置那份
```

Python 侧 `World.world_setting()` / `World.set_world_setting()`;
`contract --json` 的 `seed` 段多三格(`world_setting_read_command` /
`_write_command` / `_write_gloss`),**消费方按出口在不在探测,不比版本号**。

四条语义,和 `set_card` / `set_location_image` 逐格同源:**覆盖**(明示的编辑)·
**`--clear` 是回落到引擎内置那份,不是清空**(判据是"引擎声明过什么",不是"表里
有没有行")· **一段空白 / 一张表当场拒绝** · **逐字相同就一个字都不写**。

🔴 **那条拒绝值得单说,理由是代价不对称。** `None` 最常见的来源是
`row.get("setting")` 没取到值,一段全空白最常见的来源是模板里一个没展开的变量。
读成"抹掉"就是一次**静默抹掉整个世界观** —— 而世界观是她提示词里的**第一块**
(`PROMPT_BLOCK_ORDER` 的头一项),抹掉它这个世界里每个人下一句话都会变,
而回执上写着"改了"。拒一次的代价只是调用方补一个字。

✅ **热改是权威这一条没有被动过** —— `tests/test_world_setting.py` 现在两条各钉一遍
(老写口一条、新写口一条):**换了门不等于换了语义。**

🔴 **一条差点漏掉的洞,被一条用例钉住了**:`--clear` 的 `changed` 判据**不能只比
文本**。一个世界完全可能把世界观热改成和引擎默认值**逐字相同**的一段话 ——
那时 `after == before`,只比文本的话 `--clear` 会报 `changed: false` 然后一个字都
不写,于是那一行**永远删不掉**,而屏幕上写着"没有变化",看上去完全正常。
判据得是「这个世界自己还有没有那一行」。**试过牙**:把判据退回成只比文本,
`test_clear_一个和默认值逐字相同的世界观_照样删得掉` 当场红。

顺带:`world.setting` 这个槽位名从字面量升成常量
(`prompt_store.WORLD_SETTING_PROMPT`)—— 新开的那扇门和播种那一处必须指着同一个槽位。
`RedisPromptStore` 多一个 `drop()`:「世界里只存作者动过的」那条纪律的对偶 ——
能写下一个意见,就得能收回它。

### 判包那一族:三扇门说的话对上了(2026-08-27,收件箱 D30 / D31 / D32)

任务单 `docs/任务单/2026-08-27-core-判包一族与world_setting.md`。
**D30 / D31 / D32 不是三件事,是同一个病灶的三格**:`world check` / `world import` /
真开机这三扇门对同一份包给出的话不一致,而**不一致永远偏向乐观** —— 下游读到的
那一句总是最松的那一句。

⚠️ **如实记**:D30 老板 2026-08-21 曾拍「那一单不修」,本轮依 08-27「存在的问题
全解决」重启。若非本意以他一句话为准。

#### D30:`world check` 对一份**开不了机**的导出包答过绿灯

实测原文(FOR-STUDIO §3.30,2026-08-21,两支真 venv、一台真 Redis):3.7.0 跑过的
世界导出来,拿 3.5.0 `world check` **说绿**、`import` **退 0**、**真开机退 1**
(`OntologyError: 不认识的字段 ['importance']`)。

**病灶不是"说窄了",是没看。** 那种包一条 `author` 记录都没有 —— 它的种类、实例、
规律、地点、物品全在 Redis 行里,而这扇门当时只读作者层。它于是在**没看过**的
情况下答了"能装",并且人读的那一面印着「装得进去」。

摆在桌上的三条修法**全部否掉**,理由各不相同:

| 修法 | 为什么不 |
|---|---|
| `loadable` 答 `false` | 会拦下舰队上**每一份正常导出包** —— 比开机严 = 假红。本仓的判决原话:「开机是权威,比它严是假红、比它松是假绿,两种都比没有校验器更坏」 |
| 答 `null` + 退 1 | 每份包都拿到一个非零退出码,而**一条永远红的检查等于没有这条检查**,人只会把它 `\|\| true` 掉(`doctor` 那一格是同一条教训) |
| 只加一格 `checked_layers` | 方向对,但它**只把那句 warning 翻译成机器读得懂的** —— 那份 3.7.0 导出包在 3.5.0 上照样答绿 |

**做的是第四条:先真去查,再说清查到哪儿为止。**

1. 状态层里**开机会编译**的那几张表(`kinds` / `entities` / `world_rules` /
   `locations` / `item_defs`,登记在 `world_file.STATE_ONTOLOGY_TABLES`)离线也编译
   一遍 —— 翻成和作者层同一个 section 字典,喂给**开机第一秒那个函数**
   (`_precheck_ontology`)。**判断只有一份。**
   事件 / 记忆 / 转录 / 黑板**有意不查**:它们是数据,读不懂不会让世界开不了机,
   编译它们就是比开机严。`stocks` 同理(状态层的量住 `stock:*` 一族,形状和作者层
   那一段不是一回事,硬翻过去就是在写第二份判断)。
2. `world check --json` 多**四格**,全是纯增量:`present_layers` / `checked_layers` /
   `unchecked_layers` / `unchecked_state_tables`。消费方那条判据一行:
   `rc == 0 && loadable === true && unchecked_layers.length === 0`。
   **减法由引擎做** —— 各自算减法就是各持一份对层名的猜测。

⚠️ `unchecked_layers` 非空**不等于该拦**:一个正常导出的世界就带着事件与转录,
它照旧 `loadable: true` 且开得起来。这一格要的是"记一笔我没看全",不是一盏红灯。

**两扇离线门一起改** —— `validate world` 与 `world check` 是同一个判断的两个出口,
`tests/test_validate_matches_boot.py` 把它们的 `errors` 钉成相等。

#### D32:`world import` 对一份纯作者层的包不再报成成功

一份只有 `author` 记录的包走 `world import` 落 **0 个键**(作者记录不是键),世界
仍是空的,而空世界首启装的是**内置橱窗** —— 屏幕上住着夏、遥、柔,退出码全 0、
日志干净。**丢的不是节拍,是整个世界。**

那句诊断 3.7.0 就写对了,可它写在 `logger.warning` 上,而**机器读的是退出码**。
一句写在日志上的真话,和一盏假绿灯是同一件事。

**从今天起是当场拒绝(`PackageValidationError` → 退出码 2)。**
不选"让 import 编译作者层":那会造出**第二条创世路径**(半个
`build_serve_scheduler`),而`--world-file` 那条路已经是创世。
⚠️ **这是一次行为变更(rc 0 → rc 2),不是纯增量** —— 契约表与 FOR-STUDIO §3.46 都
记了。下游代价实测为零:运维台 v3 装载走 `--world-file`
(`deploy/world-image/entrypoint.sh`),创作台一次都不调 `world import`;
会被拦下的调用方今天拿到的本来就是一个空世界。
混装两层的包照旧 rc 0(它部分有效,且已有指路),但多一格
`authored_sections_skipped` 让"我没编译作者层"机器读得到。

#### D31:接缝在这儿,拦不拦归运维台

按判定程序两问拆的:「舰队收不收一份包」不是"世界里发生了什么"(问一 ✗),
而它要长期进程、要例外口、要跨世界的账(问二 ✓)—— **不可能是 core**。
core 只欠那一格判据,已经交付(见上)。platform 那一半的做法与判据写进了总图
契约表与 FOR-STUDIO §3.45。

#### 一条连带的闸:0 行的假绿

`STATE_ONTOLOGY_TABLES` 按 **Redis 键名**匹配。哪天有人把 `:kinds` 改名,这张表
一条都匹配不上 —— 那道新闸悄悄回到"什么都没查",而 `errors` 是空的、`loadable`
是 `true`、**一条测试都不会红**。所以加了
`test_状态层那张表里的键_必须真的是这个世界里的键`:拿一个真跑过的世界对着这张表,
对不上当场红。**试过牙**:把 `kinds` 改成 `kindz`,四条用例一起红,其中就有 D30 那条。

### 第 1 期:`plugin` 记录 —— 机制从此是数据

任务单 `docs/任务单/2026-08-26-插件系统-第1期.md` 的 1a。老板原话:「怎么做机制我们都是
做不完机制的」·「最关键的是,把我们目前的所有系统搞成出厂内置插件」。

**作者层第十三个段 `plugin`**:`{id, version, engine_min?, label?, reads?, facts?, rules?, triggers?}`。
🔴 **带它的包 `engine_min` 必须写 3.8.0** —— 不认识的作者层 type 在 3.7.0 上是
**开不了机的硬失败**,和 `beat` / `importance` 逐字同一种。

#### 一个字节的新存储都不造

事实的存储键是 **`<id>.<key>`**,住在**今天的量表**(`stock:{owner}` 那个 hash)里。
于是规律层、感知层、`stocks.evaluate_due` 一行都不用改 —— 它们本来就是按字符串键查的;
可见性走同一把尺,行落在 `(bearer种类, <id>.<key>)` 上。新增的只有一个**持久**键
`anima:{world_id}:plugins`(声明原文存库、编译在读取侧,和 `RedisRulesStore` /
`RedisOntologyStore` 逐字同一条 —— **世界住在键前缀里,不住在世界文件里**:
一个从 `--world-file` 建起来的世界,下次开机手上没有那份文件,而它的插件得照旧跑)。
它**不是易失键**,所以 platform 那条 deepEqual 不会红。

#### 三条边界,每一条的违反都是开不了机

**只写自己的命名空间**(越界 = `rules.bad_output_name` 的 `namespace=` 那道门)·
**读别人的要 `reads` 声明** · **依赖图定装载顺序**(缺依赖、成环当场报)。
三条都选了"开不了机"而不是警告,理由同一句:放行的样子全是安静的 ——
它会在别处建一个量 / 它的规律每一轮都跳过一次,而作者看到的是「我的机制不生效」。

#### 表达式里的命名空间:一层点号,而它是安全边界

`ast` 把 `qi.灵力` 解析成 `Attribute`,这一层把 **整串**当一个自由变量去查命名空间
字典 —— **不碰任何对象的 `__getattr__`**。只收一层(`a.b.c` 拒),两端都不许以
下划线开头。⚠️ **后一条是被这个仓库自己的判据逼出来的**:不加它,
`tests/test_world_rules.py::test_dangerous_expressions_are_refused` 里那条
`self.__class__` 当场变绿 —— 它在语法树上和 `needs.energy` 是同一种节点。
求值器其实碰不到任何对象,**但一条读起来像属性访问的语法迟早会被某个人实现成
属性访问**,而事实名本来也不许以下划线开头,所以这里一个字都不损失。

🔴 **`state` 在表达式里是序号**(按 `values` 顺序从 0 起),不是那个词。
`sect.rank == "内门"` **开不了机**并告诉作者该写几 —— 放行的话它是运行期 `TypeError`,
而那的样子是**这条规律安静地跳过了**。

#### 触发器:队列快照 + drain 一遍

`on:{event}` 订第 0 期那张白名单,或任何插件的 `<id>.<type>`;当事人**从事件上取**
(白名单每条标了 `parties`),不从"此刻在场的人"猜。
🔴 **队列在 tick 开头快照、drain 一遍,自己 emit 的落进下一 tick。**
同轮递归的下场不是算错,是**把 tick 线程转死** —— 而时钟卡住的样子是整个世界停了,
日志不长、健康检查照旧说 ok。代价是滞后一轮(和规律的双缓冲同一笔账)。
效果这一版两个:`set` / `emit`(只发得出自己命名空间的事件)。

#### 装 / 升 / 卸,以及**裁剪**

`plugin list` / `plugin remove <id> --yes`(⚠️ **默认预演**,和 `world drop` /
`player erase` 同一个习惯 —— 任务单里写的是 `--dry-run`,而同一把 CLI 上两种约定
比哪一种都糟)。升级 = 同 id 更高 version,**声明里没了的事实裁剪掉**并印
`dropped_facts`;**低于已装版本当场拒绝**(降级不是回退,是拿旧声明去盖新数据)。

⚠️ **裁剪是插件和本体层最大的一处不同**:本体层撤掉的量**不裁剪**(收严会让写过
额外键的已发布世界开不了机),而插件在**自己的命名空间**里裁自己的 —— 谁都不会被
误伤,所以裁得起。这正是设计稿 §7 那句「今天从不裁剪」要治的病。

#### 描述进提示词(老板那句「95 是亲密无间,然后加入描述」)

`bands` 第三项与 `values[].description`。**档词回答「多少」,描述回答「那是什么滋味」**:
自己身上那几句单占一节紧跟感知块,别人身上那几句跟在档词后面用括号收着
(那是她看出来的,不是她体会到的)。分过档的量**数字仍然永远不上屏**。
🔴 **没写描述的世界提示词逐字节不变**,而没有插件的世界这一层整个缺席 ——
两条各有一条测试钉着。

#### 契约:`contract --json` 的 `plugins` 段扩到 17 格

`author_type`(**缺席 = 这支引擎不认插件**)· `fact_shapes` · `deferred_fact_shapes`
(**连理由一起报** —— 光秃秃的"不支持"会让作者以为自己写错了字)· `bearer_forms` ·
`effects` · `id_pattern` · `reserved_ids` · `namespace_syntax` ·
`state_in_expressions` · `read_command` / `remove_command` · `storage_key` ·
`subscribable_events`(第 0 期那一格,只加不改)。
🔴 **`fact_shapes` 这一版只有 `number` 与 `state`** —— `timer` 与 `text` 写了开不了机。
**设计稿说的是这套架构装得下什么,契约说的是这一版引擎收不收。**
回执 `docs/FOR-STUDIO.md` §3.37,REFERENCE §10。

### 第 1 期验收三份挑出来的六条(2026-08-26,A/B/C 并行)

**每一条都是"承诺了没交、或者交了但不生效",而它们全都不报错。**

- 🔴 **A-P1(真回归)**:`_apply_item_restores` 只写黑板,而 3.8.0 起黑板是**派生值**
  —— 一碗回 0.5 的面吃下去,**下一 tick 就没了**(A 同世界实测:旧路走一 tick 还有
  0.5954,新路 0.2454)。吃完那一刻**黑板 0.6、量表 0.1**:同一件事两个答案,
  正是 `needs.py` 自己点名的「第二真相源」。改成写量表(黑板同刻也刷,两处同一个值)。
  ⚠️ **那条用例从前照绿,是因为它在事件落库之后一个 tick 都不走就断言** ——
  一条这样的用例验的是"写下去了",不是"留下来了"。同轮补了 `tick(1)`。
- 🔴 **C-A(白名单里五种事件是死的)**:入队挂在 `_record_and_deliver` 上,而
  `entity_interaction` / `entity_spawn` / `entity_destroy` / `item_consume` /
  `payment` 走的是 `_record_event`,`state_change` 一半一半。C 实测:订
  `entity_interaction` 的触发器 288 tick 里事件真发 4 次、触发 **0** 次、
  **一处不报错**,而 `plugin list` 照旧印「触发器 1」——**那正是 FOR-STUDIO §3.37
  与 REFERENCE §10.1 唯一的例子**。挪到**落库**那一处(「发生过」在这个引擎里的
  定义)。判据 `test_白名单里每一种事件都真的到得了触发器` **逐条**走完十种 ——
  往白名单里加一种就必须跟着响一次,否则下一期加的又是死的。
- 🔴 **C-B(卸出厂插件是一次会掉数据的空操作)**:`plugin remove needs --yes` 答
  「卸了」、redis 里真没了,**而下一次开机它装回来、三个值变回 1.0**。
  改成**拒**并指向 `config set needs.enabled false`——为什么不是"真卸且下次不装回":
  那要记一个"作者卸过"的标记,而它和 `needs.enabled` 就是同一件事的第二份答案。
- 🔴 **B-1(`plugin.removed` 从没发出去)**:设计 §7 与任务单明写要发。REFERENCE
  用「历史一个字不动」把这句换掉了 ——**那两句话看着像同一件事,其实差着方向:
  追加一条事件不是改历史,它正是这个引擎记事情的唯一方式**。补发了;
  **别的插件要订它**(一个插件的卸载是另一个插件的输入)。
- 🔴 **B-2(`contract.erasure.receipt_count_gloss` 未交付)**:player 那一格明着挂着账
  (`Account.vue` 逐字写着「在它给之前站点一个字不译」)——**全链上唯一一处下游明说
  在等的诉求**。补了,逐键一句人话。
- 🟠 **B-3(同一个 commit 里两句互斥的权威话)**:`RedisPluginStore` 与 `plugins.py`
  都写「行里存的不是声明本身」,而存的正是 `body`(声明原文)。**错的那句正好在
  读者最会去查行形状的地方** —— 照它去掉 `body`,重开机会丢掉全部插件。两处都改了。

**顺手改准的几处**:`for_each.not_action` 收列表后 `rules()` 只读视图把它报成
`"chat idle_social"`(那格是给 tool/player 看的)· `min.x` / `clamp.x` 当命名空间
解析得过(同一类只挡了 `rand`)· 降级被拒甩整段 Python 堆栈(`PluginError` 包成
`WorldSeedError`,和 `OntologyError` 同一类)· `dropped_facts` 只走 `logger.info` ——
**吵的那个不删数据,删数据的那个不吭声**,提到 warning · needs 是**七**条规律不是六条
(`social` 拆三条,`plugin list` 印 7 而四处文档写六)· `test_无插件的世界_提示词逐字节不变`
**名不副实**(只查子串没比 sha256),改名成它真的在查的那件事。

#### 那道逐位闸自己也校准了两次

**第一版(我)说得太宽**:「事件日志逐字节相同对任何代码都不成立」。
**第二版(验收 A)收窄**:差的只是 `narrative` 那一支,滤掉它之后 old/new 逐条相同。
**第三版(我,照 A 的方法多跑几趟)**:A 那句"逐条相同"**在次序上是运气** ——
同一份代码连跑两趟,103 条一条不差、类型计数一样,而第 59 条上 `state_change` 和
`travel` 换了位置(关系判定和叙事一样跑在线程池上);三趟连着跑又全同。
**一条时好时坏的闸比没有更贵:它红的时候没人知道该不该信。**
所以这道闸比的是**多重集**(排过序)——**仍然是逐条精确相等**,少一条、多一条、
payload 差一个字节都会红,被排除的只有"次序",而次序由线程池决定,不由这份代码决定。
同轮按 A 的指点补了两条:`stocks` 的交叉校验从"有才比"改成**硬断言**;
**中途真吃一次再走一 tick**(A-P1 正是从"288 tick 末态比不出吃这个动作"这条缝里溜过去的)。

### 第 2 期 2a 的地基:**边**

`plugin.edges`:有类型、有方向、身上挂事实。存储是**一个类型一个 hash**
(`anima:{w}:edge:{type}`,field 是 `起点\x00终点`)—— 不是设计稿建议的
`edge:{type}:{from}` + 反向索引,理由是 `for_each:{edge:…}` 是**每 tick** 的事,
而那种形状下"这个类型有哪些边"只能靠 `SCAN`,而 `SCAN` 是 O(整个 keyspace)。
🟢 **它不是易失键**(边是世界的内容,和 `:kinds` 同一类)→ `storage.volatile_keys`
**一个字没动**,platform 那条 deepEqual 照旧绿。

- 三个原语 `link` / `unlink` / `transfer`;`exclusive` / `exclusive_to` / `symmetric`
  的约束**在 `link` 那一刻查**,不是声明里劝一句。
- **可见性阶梯多一档 `connected`**:有这条边连着的人看得见(门规对弟子可见、
  秘密对知情人可见,设计 §5.1 那句"全是这一档")。边进提示词单占一节。
- 🔴 **`text` 只在边上收得下**,而这不是偏心,是存储的形状:节点事实住在量表里
  (`[float, tick]`),而边自己那一行本来就是一份 JSON。`contract` 因此多一格
  `edge_fact_shapes`。**`timer` 两边都不收**,而第 2 期的理由和第 1 期不同了 ——
  它不缺地方住,也**不缺能力**:一个存着 tick 的 `number` 加上内核的 `now`,
  `now - qi.中毒起始 < 100` 就是 `.active`。**为一层语法糖开一道语法不划算。**
- 🔴 **表达式里的边前缀是 `src` / `dst` / `edge`,不是设计稿写的 `from` / `to`** ——
  `from` 是 **Python 关键字**,而表达式是 `ast.parse` 解析的:`from.x` 连语法都过不去
  (实测 `invalid syntax`)。**一个解析不出来的名字不是名字。**
  ⚠️ **声明里那两个键仍然叫 `from` / `to`**(那是 JSON,不受这条限制),
  两套词只在这一处分岔。两层点号也只在这三个前缀下面开(`src.qi.灵力`)——
  别处 `a.b.c` 照旧当场拒,第 1 期那条理由一个字没变。
- **`bearer` 三个词**(2026-08-26 老板自判):`actor`(角色+玩家,**今天的语义**)/
  `agent`(只角色)/ `player`(只玩家);出厂 needs 声明 `actor`,行为不变。
  ⚠️ **`agent` 是第 1 期刚公布的词**,那时它是"两种人" —— 装载时读成 `actor`
  并把这件事写进 `contract.plugins.bearer_aliases`:收紧一个刚发出去的词而不留兼容,
  下场是第 1 期写的插件安静地少覆盖一半人。
- **抹除**:任何一端是他的边一条不留,回执多一格 `edges`(**三档和 `facts` 逐字同构**)。

### 第 2 期 2a:**插件自己的种类、动词、边上的规律** —— 图这一半合拢了

任务单 `docs/任务单/2026-08-26-插件系统-第2期.md` §2a。上一节交的是"边存得下",
这一节交的是**边被谁建起来** —— 而少了它,`plugin.edges` 只有触发器一条进得去的路,
可设计稿 §4.2 那四个例子(开宗立派 / 逐出 / 提拔 / 给东西)**没有一个**是"等某件事
发生",它们全是「他按一下」。

- **`plugin.kinds`**:`entity:<名>` / `group:<名>`。🔴 **它编译成一个普普通通的本体
  种类**(id 是 `<插件>.<名>`),不是引擎里另开的一族 —— 于是**出生自检、
  「生成必须要代价」、`prompt.budget`、可见性、拒绝语、`resolve` 的跨引用闸
  一件都不用重写**。另建一套的下场是那几件"要么重写一遍、要么悄悄不生效"。
  ⚠️ 挂在插件自己种类上的事实**不带 `<插件>.` 前缀**(种类 id 本身就是命名空间);
  挂在共用载体上的照旧带。⚠️ `group` 与 `entity` 这一版**只差一个记号**:
  只属于 group 的行为一件都没做,现在分家是在猜。
- **`plugin.verbs`**,按 **tool-calling 的 JSON schema** 声明(设计 §12.3)——
  NPC 挑动词和玩家点按钮读的是同一份定义,它们从前是两份。它编译成 target 那个
  种类上的一个 affordance,`requires`/`costs`/`consumes`/`duration`/`occupies`/
  `spawn`/`destroys_target` 语义一个字没重新定义。
  - **`effects` 收 `link`/`unlink`/`transfer`**,和触发器**共用同一个
    `_parse_link_effect` 与 `apply_edge_effect`** —— 各写一份的话,同一条 `link`
    写在触发器里查约束、写在动词里不查,而"没查"的样子是安静的。
  - 🔴 **没有 `target` 的动词开不了机**。「开宗立派」今天没有一条调用路
    (能力调用一律是 `act(她, interact, {target, verb})`)——
    **装上去让谁也点不动,比开不了机坏**:后者作者当场知道。
  - 🔴 **`target` 还不能是 `agent`,而这一格是这一期欠得最明白的一件**:
    「拜某人为师」「把东西给某人」「提拔某人」写不出来。不是漏了 ——
    内置种类只准声明量(`ontology.DECLARABLE_BUILTINS`),角色也不在
    `ontology.entities` 里。开这一格 = 开内核的一道闸,连带
    `validate world` / `world check` 要同轮跟上。**契约里明说了**
    (`plugins.verb_target_forms`),免得创作台先把那种界面画出来。
    ⚠️ **2d 的 `give`(玩家→NPC)正卡在这一格上。**
- **`for_each: {"edge": …}`**:规律作用在一条边上,表达式里 `edge.*` / `src.*` /
  `dst.*` 三个前缀,`set` **只写得到边自己的事实**(写两端是扇入,和量那一层
  `bad_output_name` 挡的那件事逐字同一种)。选择器指着没声明过的边类型 **开不了机**。
  - 🔴 **它和量上的规律分两趟跑,而这不是"顺手分个类"**:两条路共用
    `_rule_last_run` 那张水位表,`evaluate_due` 会替每一条到点的规律盖戳
    (包括它自己一条都算不动的边规律),于是紧跟其后的 `_evaluate_edge_rules`
    每一轮都读到"这一 tick 已经跑过了"而整个跳过 —— **边上的规律一辈子不跑,
    而 `rule_stats()` 每一轮都在涨**(那个数是 `evaluate_due` 数的)。
    写这一条时它真的这么错过一次,是那道闸红出来的。
- **`destroy` 连带 `unlink`**:`Scheduler._unmake` 从三张表变成**四**张。留着的下场
  和另外三样一样安静 —— 规律在一条指向坟墓的边上求值 → 跳过 → `rule_stats()` 报
  skipped;`connected` 那一档会把一个不存在的门派的门规继续念给她听。
- **`contract --json` 的 `plugins` 段多八格**(纯增量,老引擎整格缺席):
  `kind_prefixes` / `kind_id_syntax` / `verb_declaration` / `verb_keys` /
  `verb_effects` / `verb_requires_target` / `verb_target_forms` / `rule_selectors`。
  ⚠️ **这句原先写的是「七格」而下面列了八个**(2026-08-26 验收挑出来的)——
  一个数和它旁边那张表对不上,而**没有一处会红**:这正是本仓那条
  「能点名就别数数」的标本,数字是我手敲的,清单是真的。
- **存储契约一个字没动**:边那个 hash 是**持久**世界内容(和 `:kinds` 同一类),
  不进 `volatile_keys` → platform 那条 deepEqual 照旧绿。
- 回执 `docs/FOR-STUDIO.md` §3.39,`docs/REFERENCE.md` §10.10。

### 第 2 期 2b:投影式事实 `mode: "projected"` —— 一个数说得出自己是怎么来的

任务单同上 §2b,设计稿 §9.3(2026-08-26 侦察后从第 5 期提前到第 2 期)。

**这不是"更严谨"那种选项,是引擎里钱包与随身库存今天本来的样子**:它们是
`payment` / `item_transfer` 事件折出来的(**没有 `balances` 表**)。搬成一个直接写
的事实就丢掉了「可重放」—— 而「你为什么只剩三块钱」的唯一答案正是那一串事件,
**一个直接写的余额答不出这个问题,而且它答不出来的时候不报错。**

- 声明了 `mode:"projected"` 的事实:量表里那个数是**物化视图**,真相是日志里那一串
  `<插件>.<事实>.delta`(载荷 `{owner, fact, delta, cause}`;**0 不发**)。
  `cause` 是「哪一条规律/触发器让它变的」—— 没有它,一串 delta 只是一串数字。
- **折叠端只有一个处理器**(认 `.delta` 后缀),**不是每个插件一个**:一个**卸掉的**
  插件留下的那串 delta 在下一次重放时会无人认领,而无人认领的样子是"这个数悄悄
  回到 0",且只在重开的那一刻发生。
- **物化视图开机重建一次**,排在 `reset_projection` / `load_persisted_events` 之后。
  少了这一趟,跑着的世界照旧对(运行期写视图),**只有重开那一刻悄悄倒带**。
- **`forget_player` 折掉他那一行**,和 `_apply_player_departed` 折关系逐字同一条:
  追加一条事实、折叠端认它。直接去量表里删是没用的 —— 下一次重放原样折回来。
  ⚠️ 按整个 owner 比不按子串;⚠️ 历史一个字没删。
- 🔴 **`projected` 只收 `number`,而这不是"还没做"**:delta 是一个**差值**,
  而「从『外门』变成『内门』」折不成一个可以相加的数。
- 🔴 **挂在插件自己种类上的事实做不了 `projected`**:那样东西会被 `destroy` 抹掉,
  一串折向不存在的主人的 delta 重放出来是一个没有主人的数。
- `stocks.evaluate_due` 多一个可选的 `on_round(pending, snapshots, causes)` 钩子
  (**落库之前**叫一次)—— 差值只有在这一刻才同时拿得到"写之前"和"要写的";
  放在调用方那边算的话,它得再查一遍量表,而那一遍读到的已经是写完的值。
  **空表时它一次都不会被调用**,和「声明本身就是开关」逐字同构。
- **契约五格**(纯增量):`fact_modes` / `projected_shapes` /
  `projected_delta_event` / `projected_delta_payload` / `projected_bearers`。
- **存储契约一个字没动**(delta 是事件,视图住在今天的量表里)。
- 两道闸各自试过牙:删掉折叠端那一支 → 重开之后值变 0;删掉 `forget` 那一支 →
  他走了而钱包还在。回执 `docs/FOR-STUDIO.md` §3.40,`docs/REFERENCE.md` §10.11。

### 第 2 期 2c:先堵 together 那个口 —— 四轴从今天起不是它自己写的

任务单同上 §2c。老板 2026-08-26 拍的 D40 ③:**插件读得到、emit 得出内置关系四轴,
写不进** —— 四轴是 `state_change{kind:"sentiment_delta"}` 的投影,直写等于把关系
从「可重放」变成「直接写」。而 `Scheduler._settle_invitation_declined` **今天就在
直写**,它是 `together` 这一块马上要搬成出厂插件的那一部分。

🔴 **搬之前先改,不是搬完再改。** 搬完再改的话,中间那一版是一个明着违反自己刚立
的边界的出厂插件 —— 而出厂插件正是给作者看的范例。

- 这一下分两半:**发生了什么** = 新事件 `invitation.declined`(`together`
  模块的 `INVITATION_DECLINED`,载荷八格)· **她因此怎么想** = 那条
  `state_change`,由 `Scheduler.KERNEL_RELATION_CAUSES` 这张**内核保留**的表写。
- **反应是同步的,不走触发器队列。** 队列在 tick 开头快照、drain 一遍,自己 emit
  的落进下一 tick —— 对递归那是对的,对这一件不是:这一下的因果是同一个瞬间的
  两半,隔一个 tick 的话,中间任何一次 `state()` 都会看到一个「他已经拒绝了而她
  还没反应」的世界,**而那个世界会被写进日志、进重放、进提示词**。
- **逐字节闸两条**:那条 `state_change` 的载荷**八格逐字钉死**;这一趟的事件日志
  差集**只有** `invitation.declined` 一条(`invitation_settled` / `memory_seed` /
  `state_change` 原样)。⚠️ `cause` 的取值一个字都不许改 —— `test_没人答_到点就过期`
  那一族按 `cause == "invitation_declined"` 挑事件,改名之后那几条断言会变成
  **永远成立**,于是「过期不记」这条老板拍的纪律从此没人守着,而它照绿。
- **新事件真的到得了触发器**(第 1 期验收 C 逮到五种死事件之后立的规矩):
  判据 `::test_订invitation_declined的触发器_真的响一次`。它是插件命名空间形状的
  事件(带点号),所以**不必进 `SUBSCRIBABLE_EVENTS`**。
- **三仓一件不用改**:邀请那三扇门的形状、`INVITE_OUTCOMES` 的取值、`outcome`
  的五支、`state_change` 的载荷,一个字都没动。
  回执 `docs/FOR-STUDIO.md` §3.41,`docs/REFERENCE.md` §10.12。

### 第 2 期 2d 的地基:**economy 的旧路基线,搬家之前先落盘**

`tests/data/economy_legacy_golden.json` + `tests/test_economy_plugin_parity.py`。
**搬家还没开始 —— 这一份此刻验的是旧路对自己。**

🔴 **为什么必须在动它之前采**:和 `needs_legacy_golden.json` 同一条 —— 旧路删掉
之后,这份文件是「从前那个世界长什么样」唯一的证据。等插件路写好了再采,
采到的就是**插件路自己**:一道拿被测对象当标准答案的闸,它永远绿,
而且它绿的时候什么也没说。

- 场景是**三段**(第 1 期验收 A 那条 P1 的教训:末态相同 ≠ 中途相同):
  跑 144 tick → 玩家进城、看货架、真买一件、再看一眼 → 再跑 145 tick。
  这几下的**回执逐格**进闸。
- 比:钱包 / 随身库存(事件投影)· 货架 · 玩家那一屏 · 量表 · 三份提示词 sha ·
  `state()` sha · 非叙事事件多重集。
- 🔴 **采基线时撞出两件,两件都值得记**:
  1. **`state().players[*].last_seen` 是一个墙钟**(两趟差 2.0027 秒)——
     剥它,不是放宽 sha:放宽的话整个 `players` 段跟着豁免,而里头还有
     `location` / `in_transit`,恰恰是这道闸最该盯的。
  2. **任务单 §1 的侦察把 `give_item` 的 owner 说错了一层**:实测它是
     `_ToolRuntime.give_item`(`api.py:1104`),**`World` 门面上根本没有它** ——
     照那句话敲是 `AttributeError`。后果不在这道闸上,在 2d 上:`give` 今天的
     调用路是「聊天里用了一个工具」,要给它 parity 闸得先能确定性地驱动那条路。
     **所以这一下有意不比,并说出来** —— 用一个 `AttributeError` 冒充"我验过了"
     比不验更坏。
- **线程池上的两样都关掉**(叙事 + 关系判定)。needs 那道闸只关了叙事,于是它在
  机器忙的时候会红,而红的那句话指控的是被测的东西(同日实测:同一份代码连采
  两趟,提示词差 12 个字符而记忆一条不差)。economy 那一摞全跑在 tick 线程上,
  关掉不放宽这道闸;**代价是它验不了「搬家有没有改到关系」** —— economy 不碰四轴,
  这一格今天是空的。
- 🔴 **试过牙,而第一次试的两下都是假的**:改 `scheduler.py` 里那句 `wage = 20.0`
  和 `drift_price` 的默认参数 `k` —— **全绿**,因为两处都是**死掉的默认值**
  (下一句就是 `config_store.get(..., default=wage)`,而 `_DEFAULTS` 里有这个键)。
  改到真的活着的那两处(`_DEFAULTS["economy.daily_wage"]` / `drift_price` 的返回值)
  才红,分别红 2 条与 4 条,差在 `3.26` vs `3.27`、以及一分钱的货架价。
  **「新加的闸也会假绿」的另一半:试牙也要试对地方** —— 我以为在试这道闸,
  其实在试一段死代码,而屏幕上那个信号和"这道闸没牙"一模一样。

### ✅ 那道时好时坏的闸修完了 —— 而我当时报的两条出路,建立在一个假前提上

**假前提是「旧路已经删了,所以重采只能从插件路采」。旧路没被删掉,它在 git 里**
(`fa1507b^` = `a6b3da3`,`git worktree` 一敲就在)。裁决 ② 指出这一点之后,
修法就只剩一条,而且不损失任何证据:**两池全关 + 从旧路那棵树重采基线**,
范式照同一天新加的 `test_economy_plugin_parity.py`(**先决定比什么,再采基线**)。

新基线的出处、怎么跑的、和上一份逐格差在哪,全写进了
`tests/data/needs_legacy_golden.json` 的 `_采自`,每一条都可复核(实测):

🔴 **这张表第一版把两份不同的 golden 写成了一份**(同日复核评审逮的,已改)。
这个文件历史上有**三版**,而底下那些数分别对着前两版 —— 只点一份文件、
却拿另一份比,正是本轮自己刚立的那条「**能点名就别数数**」栽在了"点名"这一半上。

| 拿谁比 | ① `fa1507b`(最初那份:**旧路** + 判定**开**) | ② `13d5078`(重采前躺在树上那份:**插件路** + 判定**开**) |
|---|---|---|
| `needs` / `prompt_sha` | **逐字节相同** | **逐字节相同** |
| `stocks` | **逐字节相同**(两边 sha 都是 `09307c59ad5d…`)—— 它也是旧路采的 | 只差三个 `needs.*` 键 = **搬家本身** |
| `events` | **没得比**:①顶层根本没有这个键 | 103 → 99,少的 4 条**点得出名字**(2 `memory_seed` + 2 `state_change`,关系判定那一次的产物) |
| `state_sha` | 变了 | 变了 |

| | |
|---|---|
| 凭什么说它出自旧路 | **判据是正的**:`hasattr(anima_world.needs, "factory_plugin")` 在那棵树上是 `False` |
| 两份旧的都没被抹掉 | `fa1507b:…`(sha `72802224…`)与 `13d5078:…`(sha `03921439…`),都记在新文件头 `_采自` 里 |

### 第 2 期 2d-①:**economy 只搬钱包那一格**(补裁 ⑤)

🔴 **别读成「economy 已经是插件了」。** 搬过来的是**一个事实** ——
`economy.coins`(`projected`,靠 §10.13 那格 `sources` 认领引擎已经在发的 `payment`)。
**其余三样一格没动**:`buy`/`eat`/`give` 仍是内核路 · 货架仍住 `shop_stock` 那个真
hash · `economy.enabled` 语义一个字没动。理由逐条在 FOR-STUDIO §3.43,
一句话版:**换调用路 ⇒ 逐字节从原理上不成立**(被比的两样东西不是同一件事);
而货架是**换掉一个真键**,老包装进去会原样落键、没有一处再读它。

- 🔴 **`round: 2` 是承重的**:账本 `_apply_payment` 每一步折到两位(二进制浮点存不下
  0.1,而门禁读的是这个数)。折到六位就是第二个钱包,只在小数第三位往后分家。
  为此 `sources` 多一格 `round`(纯增量)。⚠️ **economy 那道 parity 闸对这一格没有牙**
  (那 8 笔钱都是干净的两位小数,改成 6 位照样绿)—— 有牙的是那条单测。
  **试牙也要试对地方**,这一轮第二次记它。
- 🔴 **物化视图只写给真的有量表的 owner**:折出来的账按**任意持有者**记
  (`__town__` / `shop:cafe`),而事实住在**有类型的载体**上。不滤的话
  `agent:__town__` 会被凭空建出一行量表,进 `stock_owners()`、进打包、进 `state()`
  —— **而世界里没有这个人**。
- 🔴 **一样"搬不动"而不是"没搬"**:`Projection.balances` 记的是任意持有者,
  **镇上的金库和店铺是持有者、不是载体** —— 所以 `balance()` 这一轮照旧读账本。
- **契约两格纯增量**:`plugins.factory`(id → 开关键)与 `plugins.factory_scope`
  (**每个搬了哪几格**)。🔴 **不按"搬完几个系统"计数** —— 按系统计数正是把人推向
  换皮的那把尺子(补裁 ⑤b)。
- 闸照 economy 那道地基的范式(先决定比什么、两池全关):`balance()` / `player_shop` /
  `shop` / 三份提示词 / `state()` 刨三格 / 事件日志多重集**逐字节相同**,量表白名单
  **恰好多一个**,外加 projected 那道牙。**文件头逐条写着它有意不比什么。**
  回执 FOR-STUDIO §3.43,REFERENCE §10.14。

### 复核评审排回来的 5 条残余(并进这一轮)

1. **`_verb_edge_gate` 只预检了 `link`** —— 「没入门就退出」体力照扣、`ok: true`、
   边一条没断。**留半截比不查更难读**:同一件事在两个动词上两种下场。
   已补 `unlink` / `transfer` 两支,和 `apply_edge_effect` 读**同一组函数**。
2. **golden 的 `_采自` 把两份不同的 golden 写成了一份** —— 这个文件历史上有**三版**,
   而那张表的数分别对着前两版(`stocks` 和 `fa1507b` 是**逐字节相同**,
   `events` 那一栏在它身上**根本没得比**:①顶层没有这个键)。已拆成两张表、两个 sha。
   **正是本轮自己立的「能点名就别数数」,而这次错在"点名"那一半。**
3. **契约里一句陈注释**说 `agent` 是「这一期还写不出来」,和它下面三行
   `verb_target_never`(**永远不收**)正面矛盾 → 改。**两句打架的话比两句里哪一句都糟。**
4. **`edge_ends` 那盏假红灯只治了一半**:两个模板混进一列字面值,而**没有一格说
   它们是模板** → 补 `edge_end_prefixes` 与 `edge_ends_gloss`,判据敲得动。
5. `plugin list --help` / FOR-STUDIO 那个 `--json` 例子 / 人眼行**数得出「边 1」
   却印不出它叫什么** —— 三处补齐。

### 第 2 期 2e:邀请的**存储与过期规律**搬成出厂插件(裁决 ③)

🔴 **别读成「邀请这条机制变成插件了」。** 搬的是**两样**:边 `invitation.invites`
+ 那条比 `now` 的过期规律。**三扇门留在内核,签名一格不变**(冻结面);
`settle_invitation`、`INVITE_OUTCOMES` 四态、2c 那条四轴路,一个字没动。

- ⚠️ **`timer` 形状仍然不收**,过期写成**存着 tick 的 `number` + 内核的 `now`** ——
  那正是当初判「为一层语法糖开一道语法不划算」时说的写法,现在它有了第一个使用者。
- 🔴 **落在哪一拍一格没挪**:规律在 tick 的 3.61 标记,`_settle_invitations` 在 3.9
  收尾 —— **同一个 tick**。那一拍是玩家屏上「你没来得及答」的时刻。
  判据是**既有那条** `test_没人答_到点就过期`:把规律改坏它当场红(实测)。
- 🔴 **边是投影的物化视图,开机重建** —— 而这一条是**被既有那条
  `test_邀请是事件不是易失态` 逮出来的**:边是直接写的,投影是折出来的,
  少了重建那一趟,一个丢了边(或从别的前缀重放出来)的世界里那几份邀请
  **永远不会过期**,清单上一直挂着、「还剩几拍」一直数下去。
  那条用例的措辞也跟着改准了:它原先断言「不开新的 Redis 键」,而现在钉的是
  它真正要守的三件(不进 `volatile_keys` / 它是派生的 / 投影仍是清单的权威)。
- **`edge_rule_effects` 从 `["set"]` 扩成 `["set","emit"]`**(纯加法)。上一轮那道
  加载期拒绝这一轮放行,**而顺序是承重的:先有使用者,再开口子。**
  边沿触发照抄节点那一层;载荷里两端都说得出;⚠️ **没人订它也照发** ——
  "发生了"在这个引擎里的定义就是**进了日志**,没订户 ≠ 被丢掉。
- **它没有开关**(`plugins.factory` 那一格是空串):**它搬的那件事今天也没有开关**
  (邀请不受 `social.enabled` 管)。给它新造一个,就是给同一件事第二个答案。
- **存储契约**:新增一个**持久**键 `anima:{w}:edge:invitation.invites`,
  🟢 **不进 `volatile_keys`** → platform 那条 deepEqual 照旧绿。
- ✅ **橱窗第一次真的敲出来了**:邀请门是 3.6.0 交的,而它**一次都没在内置那份
  世界上出现过** —— 三道闸里「橱窗里展示它」这一道到今天才补上
  (`test_橱窗里她真的约得动人_而且到点会过期`)。
- ⚠️ **一笔该收而这一轮没收的账**:规律那一层的 `emit.type` **不自动加命名空间**
  (触发器那一层会查、会拒)。收它会改掉已经发出去的写法 = 破坏消费方,
  所以出厂插件自己写全名。记在 FOR-STUDIO §3.44。
  回执 FOR-STUDIO §3.44,REFERENCE §10.15。

### 全量跑出来的三条,全是**既有的闸**逮的(记在这儿,因为它们各说明一件事)

1. 🔴 **源事件的物化视图第一版是"每来一条就逐个 owner 开一次门"** ——
   而 `test_the_engine_scales_to_many_entities` 盯的正是「tick 里出现了逐个 owner 的
   `set_many`」。它不是风格问题:一千个人的世界里每 tick 逐个往返一次,
   就是这个引擎明说过的那种 72ms/tick 的来路。
   改成**tick 里只攒、收尾一次 `write_round`**(现值走 `snapshot_many` 一次问完);
   tick 外(宿主调 `player_topup` 那种)当场写,因为下一句读它的可能就是同一个调用方。
2. **`plugin list --help` 里那对 `**`** 被 `test_屏幕上不许出现裸markdown星号` 逮住 ——
   帮助文本是**印到终端上的**,而这个仓库有一道闸专管这件事。
3. **`test_重开一个世界不会被塞进别人的物理法则`**:它断言「作者没写规律,
   世界就不该有规律」,而 `invitation` 那条出厂规律没有开关、永远装。
   改成比**作者的**规律(按 `scheduler.plugin_rule_ids` 分)——
   那张表本来就是为这条分界存在的;**这条用例真正怕的那件事一格没松**:
   重开时橱窗那条引用 `tree` 的生长规律照旧不许进来。

**三条都不是新写的闸逮的,是老闸逮的** —— 而这正是「每个仓库的测试就是它的边界」
那句话在这一轮的样子。

### 第三轮两路复核的 8 条:3 条 P1 + 2 条假绿 + 3 条契约小账

**P1 那三条有一个共同的形状:我上一轮写的修法本身错了,而错法都不报错。**

1. 🔴 **`transfer` 从此一次也成功不了。** 上一轮给 `unlink`/`transfer` 补预检时,
   我说"判据和 `apply_edge_effect` 读同一组函数" —— **那句话说对了、抄错了**:
   预检问的是 `get(type, src, dst)`(**转移之后**才会有的那条边),而执行找的是
   **要搬走的那条**(`of_dst(dst) if by_dst else of_src(src)`)。两个相反的条件,
   闸先说话。⚠️ **它比上一轮那次更难查**:那次是"该拒没拒",这次是
   **"该成的永远成不了"**,而回执那句「你身上没有这条门籍」听起来完全合理。
   而且**零覆盖** —— 删掉预检里那个 `"transfer"` 字样,全仓照样绿。已修 + 补两条用例。
2. 🔴 **两份钱包。** `fact_source_updates` 把**每一笔 delta 先折了一次**,而
   `_apply_payment` 只折**累加结果**。实测 `balance()=63.13` 而量表 `63.12` ——
   **而它们分家的那一位,正是"一笔正好够的交易会不会被门禁拒掉"那一位**。
   修法:delta 不预折,位数只用在累加那一处。
3. 🔴 **运行中 `config_set("economy.enabled", True)` 之后,量表那一格停在 0 直到重开。**
   `refresh_plugins` 换了声明与注册表,却没重建物化视图 —— **"重开才对上"正是
   2b 里我自己防过的那一族**(「只有重开那一刻才错」),而这是同一个洞的第二个入口:
   开机那条路补过了,热更新这条没补。已补(连带邀请那几条边)。

**两条假绿/零覆盖 —— 而它们都是我上一轮亲口点名"有牙"的那几条:**

4. **`sources.round` 全仓无牙。** 我点名的那条单测取 `0.1 + 0.2`,而那个数在
   round 2 和 round 6 下答案相同 —— **把 `round` 改成 6,全仓照样全绿**。
   改成拿两份真的折叠端**逐笔对**,数字是**穷举出来确实分家的那一对**
   (`1.005 / 0.055` → 1.05 vs 1.06)。⚠️ 顺带发现:**一串长一点的钱反而可能又对上**
   (四笔那串两边都是 1.19),所以那一对写死在常量上,别顺手往里加数。
   **「试牙也要试对地方」这一轮记第三次,而这三次都是我自己。**
   🔴 **而这一条第一次并没有修完**(重放评审第二次逮的):换上来的那条闸
   **在自己的 specs 里写死了 `round: 2`** —— 它验的是"折叠机制认不认声明"
   (这半确实有牙),**不是"出厂那一格声明的是几"**。把 `economy.py` 那份**真声明**
   改成 6,全仓照样绿(86 passed),而真世界里 `stocks=63.1305` 对 `balance()=63.13`。
   **一个自己造判据的闸,验的是它自己。** 现在那条闸**不造 specs**:直接拿
   `economy.factory_plugin()` 那份真声明去折、和账本逐笔对,并把注册表那一行
   照 `_install_plugins` 拼(各拼一遍的话,验的又是它自己拼的那份)。
   改那一格当场红 —— 试过牙。

### 三处「契约说不收、引擎照收」收口 —— **这是一次收紧,而它成立的理由是量出来的**

创作台接第 2 期时测出来的两条,加上它顺手补的那一问。三条都是同一个形状:
**契约里写着不收,而 `parse_plugins` 照收**,下场比"不支持"更坏 ——
**作者写下的那一格根本不在,而退出码 0、日志干净**。创作台因此两头都没法判
(照契约判是假红、照行为判是帮着引擎丢),只好加了第三档 `DROPPED` 🟡。
**判决:把两句话变成一句,引擎照契约拒** —— 和这一单一路的口径一致
(「静默不支持是撒谎」/「装上去让谁也点不动,比开不了机坏」)。

1. **`bearer: "entity:<种类>"` + `mode: "projected"`**:2b 就写明做不了,而闸只拦住了
   写在 `kinds` 里那一种,**顶层 `facts` 那种写法照收**。两种写法是同一件事。
2. **动词上多写一个键**:`verb_keys` 报十五个,多写的照收然后**静默丢**。
   和 §2a 那条「target 写错字静默长出空种类」同族;对照组就在同一份文件里 ——
   `sources` 那一层早就逐键查了不认识的键。
3. **规律的 `emit.type` 不带命名空间**:触发器那一层一直会拒,规律这一层从前不查 ——
   **同一个插件里两种写法两种下场**。

🔴 **第 3 条上一轮被我用「收它=破坏消费方」推迟了,而那句话量过之后不成立**
(创作台补的那一问问得对):四个仓库里 `plugin` 记录 **0 条**、`demo.cyberworld` **0 条**、
`3.8.0` **没打 tag**、PyPI 停在 `3.7.0`、线上镜像是 `anima-world:3.7.0`、
出厂那三个里唯一发事件的写的就是全名。**消费方为零,所以现在收最便宜 ——
再晚就真的会破坏谁了。** 一条推迟的理由,应该在推迟的那一刻就量,而不是等人来问。

⚠️ **收紧的代价说清**:契约那三格(`projected_bearers` / `verb_keys` / 命名空间语法)
**一个取值都没改** —— 变的是引擎从"说一套做一套"变成"说到做到",所以**对照契约写的
插件一个都不会因此开不了机**;会红的只有那些**本来就在被静默丢弃**的写法。
author 层的 `rules`(不是插件的)**一个字没动** —— 命名空间那条只作用在插件的规律上
(`tests/test_rules_emit_and_dice.py` 全绿钉着)。
**给创作台的回执**:第三档 `DROPPED` 可以拆了,钉着 `1f86872` 的 24 条判决用例会红,
**那正是它们存在的理由**;判据从"警告"改回"拒绝",并补上规律 `emit.type` 那一条。
回执 `docs/FOR-STUDIO.md` §3.44。

### 最后一个静默住户,以及给创作台的六格盲区(2026-08-27)

创作台换钉 `a3b5fca` 重量 66 条样例(61 条逐条一致)之后交回来的两件。

1. **插件规律里不认识的键照收然后丢掉。** 它报的是「边规律里写 `link`」,
   而实测**比那更宽**:`link` / `effects` / `cooldown` 都一样 —— 这是插件这一层
   **最后一个静默住户**,和同一天收掉的另外三格同种。规律、`emit`、触发器三层都补了。
   ⚠️ **作者层的 `rules` 一个字没动**:那是一个早就发出去的面(线上两个世界我够不着),
   而**「没量过就别收」是这一单一路的口径** —— 上一轮我正是靠量掉一个假前提才敢收的。
2. **契约补六格**(创作台诉求第六条):`version_required` / `rule_keys` /
   `rule_every_keys` / `emit_keys` / `emit_required_keys` / `trigger_keys` /
   `trigger_required_keys` / `kind_local_pattern`。
   🔴 **这六格全是"引擎本来就拒、而契约不说"** —— 所以是**纯增量,一格取值都没改**;
   少了它们,创作台判不了,只能眼看作者出包之后被引擎打回。
   形状照 `id_pattern` 那条先例(**给每种名字一格正则**)。
   **和引擎读的是同一份常量**(`rules.RULE_KEYS` 等,契约只是转出来)——
   抄第二遍就是「契约说六个、引擎认七个」那种漂移的来路。
   ⚠️ `link.type` **有意没给一格**:边类型是每个插件自己声明的,不是一张全局表。

**给创作台的回执**:`DROPPED` 档从此应当是空的;那条「盲区不许变多」的反向闸
**这一轮可以收窄六格**。回执 `docs/FOR-STUDIO.md` §3.44。

### 收尾全扫:`plugin` 记录**每一个层级**都不许静默丢键(2026-08-27)

创作台换钉重量后又交回六条,**全是同一种病在别的器官**:规律缺
`rule_required_keys`;边 / 事实 / 种类 / 顶层 / 触发器效果里的 `emit` ——
这五层上不认识的键**引擎照收不生效**、契约不报名单。

🔴 **一层一层收本身就是这个 bug 的形状。** 前四轮我每次只收被点到的那一层,
而每换一次钉就量出新的一层 —— 所以这一轮**一次过完**:实测八层全部照收
(顶层 / 事实 / 边 / 边上的事实 / 种类 / 种类上的事实 / 触发器的 `emit` /
边效果体),现已全部当场拒并**点名**列出多的那几个键。

- **一份判断,十一个层级共用**(`plugins.unknown_keys`),每层一格键名单进契约,
  **全部和引擎读同一份常量** —— 抄第二遍就是「契约说六个、引擎认七个」的来路。
- 🆕 契约多 `strict_levels` / `plugin_keys` / `fact_keys` / `edge_keys` /
  `kind_keys` / `trigger_emit_keys` / `edge_effect_keys` / `rule_required_keys`。
  `strict_levels` 是给创作台那条**「盲区不许变多」反向闸**用的:照它遍历,
  每格都得有名单;引擎这侧有一条同名的闸逐格点名,**加一层却忘了报,两边同时红**。
- 🔴 **`trigger_emit_keys` 与 `emit_keys` 是两份,有意不合并**(创作台量到并提醒的):
  规律的 `emit` 有 `when`/`on`/`importance`(门槛与边沿是**规律**那一层的概念),
  触发器的 `emit` 已经"因一件事而发"了。**合成一格,创作台那边对着一份合法声明
  就是假红。**
- ⚠️ **作者层照旧一个字不动** —— 理由没变(早就发出去的面,而「没量过就别收」)。
5. **开机那趟 `rebuild_invitation_edges()` 注掉,65 条全绿** —— 仓内那条用例
   **直接调方法,不走开机路**。补了一条真的走开机的:抹掉整张边表 → 关世界 →
   重开 → 到点照样过期(试过牙,注掉当场红)。

**三条契约小账**:⑥ `FACTORY_SCOPE` 是 `FACTORY_PLUGINS` 的第二份键集且没有闸 →
补一条比**键集**的用例(**能点名就别数数**),并连带钉住契约那两格报的就是这两张表 ·
⑦ FOR-STUDIO §3.44 补一句:`plugins.factory` / `factory_scope` **整格缺席 = 这支引擎
没有出厂插件表**,一律 `.get` 到底(和 `outcomes_api` 那格同待遇)·
⑧ 补「订 `invitation.expired` 的触发器真响一次」—— §2a 我自己立的新事件规矩。

⚠️ **采样那一步有个坑**:`PYTHONPATH` 单独放是不够的 —— 本仓 editable 装的是一个
`__editable__` 的 meta_path finder,它比 `sys.path` **先说话**,于是
`anima_world.__file__` 会照旧指回工作树,**而采基线的人以为自己在旧树上跑**。

**判据不是"跑一趟绿了":连跑 9 趟全绿**(2026-08-26 实测,起跑 load 1.13)。

<details><summary>下面这一段是修之前那一轮的记录,留着当"两条出路都建立在假前提上"的标本</summary>

### 🔴 一条**当时没修**的:`test_needs_plugin_parity` 时好时坏,病根找到了,而修它有代价

**先说清它不是这一轮弄坏的** —— 交叉重跑量过,不是推出来的:

    base(fd1f20d,本轮开工前那个提交) 9 趟 → 红 5
    head(本轮全部提交之后)          9 趟 → 红 9
    (先各连跑 5 趟,再 base / head 交替跑 4 对,控住"机器此刻忙不忙")

两边都在红。head 更稳定地红,**而这两个数之间的差别落在这道闸自己的抖动幅度里**
(同一个 base 早一轮是 3 绿 / 5,晚一轮是 3 红 / 4)—— 别拿它当"这一轮改坏了"。
⚠️ 红的那两条是 `test_提示词逐字节相同` 与 **`test_基线自己是可复现的`**,
而**后者比的是同一份代码对它自己**:它红的时候说的一定不是"谁改坏了什么"。

**病根**:那道闸只关了**叙事**,而**关系判定跑在另一个线程池上**
(`scheduler._judge_pool`),它的产物落在第几 tick 同样由这台机器决定。
`fd1f20d` 那次修的是"采样早了一步"(`_quiesce`),**只是这个病的一半**。
实测把 `scheduler.relationship_judge` 也设成 `None`,那两条 **4/4 全绿**。

🔴 **而这一轮有意没改。** 同一刻**另外两条稳定变红**(`state刨掉…` / `非叙事事件…`)
—— 关掉判定池**改变了这个世界**,而 `needs_legacy_golden.json` 是**开着它**采的。
要让它们重新绿只能重采基线,**而重采是从插件路采一份「旧路基线」** ——
那正好毁掉这份文件唯一的用处:旧路删掉之后,它是"从前那个世界长什么样"唯一的证据。
两条出路(① 重采并接受它降级成"今天的快照" · ② 把判定池钉成确定性的再重采)
各有代价,**不是一个写者该单方面拍的,已上报**。

✅ 同一天新加的 `test_economy_plugin_parity.py` **没有这个病**:它采基线之前就把
两个池都关了,并把"关掉之后这道闸验不了什么"一并写在文件里。
**那才是这一族闸该有的顺序 —— 先决定比什么,再采基线,不是反过来。**

</details>

### 三视角验收挑出来的十五条:P1 七条各带一道牙,P2 三条,P3 五条账面

**七条 P1 有一个共同的形状:说过的话和跑出来的事对不上,而对不上时不报错。**

1. **边连不上时,代价照收、`ok: true`、边没建**(A/C 独立双复现)。`_apply_verb_edges`
   把 `apply_edge_effect` 的返回值扔了 —— 而那个返回值的 docstring 自己写着
   「它是承重的」。修法照本仓既有的纪律:**拦在收费之前**(`_verb_edge_gate`,
   和 `spawn` 那句「收了钱再发现生不出来」逐字同一条),新拒绝理由 `edge_blocked`;
   查不动的那一支(指着 `spawned`)事后如实报进 `act()` 回执的 `edges` 格。
2. 🔴 **插件伪造得了别家的投影**(A):`_apply_fact_delta` 只看 `payload.fact`,
   不看**这条事件是谁发的** —— 一个 `thief` 插件 emit
   `thief.伪造.delta{fact:"bank.存款"}` 就改得动别家钱包。**运行期不显形**
   (物化视图没动),**重开那一刻 999999**,零报错。修法是**同一性**而不是白名单:
   事件类型必须**恰好**是 `<那个事实>.delta` —— 于是"谁发的"和"改谁的"是同一个名字,
   插件命名空间那道加载期的闸自动把这道门也关上,**一份判断,不是第二道闸**。
3. **离线两扇门不编译 `plugin.kinds`**(B/C 双复现):一份**开得起来**的世界被
   `validate world` / `world check` 答成「引用不到 kind」退 2,而 tool 把退 2 当红灯
   —— **第一个照着 FOR-STUDIO 写插件的作者,先看到的是一盏假红灯**。
   开机是权威:比它严是假红、比它松是假绿,两种都比没有校验器更坏。
4. **动词的裸串 target 写错字**(`"swrd"`,C):两扇门全绿、开机全绿,然后**静默
   长出一个空种类,永远不会有实例**。判在 `_merge_plugin_kinds`(**只有那儿两份
   种类都在手上**),当场拒并列出这个世界声明过哪些种类。
5. **`target: "agent"` 是 29 行 Python 栈,末行怪 `kinds` 不怪插件**(C)→ 加载期
   一句中文,并引用 `verb_target_forms` 与裁决 ①。
6. **`contract.plugins.edge_ends` 漏报**(C 实跑):只列四个裸词,而 `_parse_edge`
   真收 `entity:` / `group:` 前缀 —— **照契约判的 tool 会拒掉一个引擎跑得起来的世界**。
   已补两个形状格 + `EDGE_END_PREFIXES`。
7. **边规律写 `emit` 静默无效**(B):契约 `effects` 含 emit、`rule_selectors` 含 edge,
   **没有一格说这个组合不成立**,开机不拦零 warning。→ 加载期拒 + 新契约格
   `edge_rule_effects: ["set"]`(将来收 emit 是加法)。对照组是同一份文件里
   `projected` 那两条限制:**加载期拒 + 说得出为什么**。

**P2 三条**:边规律一格都不进 `rule_stats()`(而三处注释拿它当信号 ——
**一个不存在的信号比没有信号更贵**),连带它的节流水位在只有边规律的世界里
每次重开多烧一轮 → 两件同源,一起修 · 边进提示词印裸节点 id(「你和
menpai.sect:青云门」)→ `_node_name` 补实例与地点两支 · 只 `link` 的动词
`changes_world: false` 被印成「只是看看」→ 补在**知道这件事的那一层**
(`World.kinds()`),不是把边塞回本体;同轮给 `Verb.schema()` 接上第一个生产路径的
消费者:`plugin list` 从此报 kinds / edges / verbs(它从前对这三样**一字不报**,
人眼那一行是「挂在 」加一片空白)。

**P3 五条账面**:`bearer` 三个词实落两个(`agent` 被读成 `actor`,「只角色」今天
没有写法)—— **这一轮不改判,只把账说清**,并给出「照 `bearer_aliases` 判,别照
`bearer_forms` 维护清单」· `invitation.declined` 与 `invitation_settled` 共用那扇
**没有游标**的窗,每次拒绝多写一条 → 写进 FOR-STUDIO §3.41 · CHANGELOG 里
「多七格」而下面列了八个(**能点名就别数数**)· fixture 过期提醒只点了 player 前端,
补上后端六处(**只点一半的提醒和不提醒一样**)· `plugin list` 那一行见 P2。

### 裁决 ④ 落地:`projected` 多一格 `sources` —— **2d 真正的第五块拦路石**

折叠端只认 `.delta` 后缀,而设计 §9.3 说「`payment` 事件照旧是 `economy.coins`
的 delta」—— **这两句对不上,而没有一处会红**。三条路里两条是坏的:改发
`economy.coins.delta`(`payment` 在 `subscribable_events` 上,**改名 = 破坏消费方**)·
两条都发(**同一笔钱记两遍账**)。只有"多一格声明"这条不破坏任何人。

- 声明 `{event, amount, credit, debit, owner_form}`;**`credit` 加、`debit` 减,
  两个键名写死** —— 给一个 `sign` 让作者自己填的话,写反了不报错,
  而一个反着记的账让「对账即重放」成了一句空话。
- 🔴 **只收内核白名单上的事件**,而这一条和上面那条「插件伪造不了别家的投影」
  是**同一道边界的两面**:折叠端那道闸靠同一性关上了「谁发的 ≠ 改谁的」,
  而 `sources` 是一张**作者写的**表 —— 认得了 `<别家>.<事实>.delta` 的话,
  刚关上的门就从这儿又开了,**只是这次是声明式地开**。
- **只有一份算法**(`projection.fact_source_updates`):重放与运行期写物化视图读
  同一个函数。各写一遍 = 跑着的世界一个数、重开之后另一个数,两边都不报错。
- **注册表要在折之前就位**(`Projection.fact_sources`,`reset_projection` 带过去);
  有 `sources` 的世界**开机重折一遍** —— `Scheduler.__init__` 那次重放跑在插件装上
  之前,那时注册表还是空的。这是设计 §9.3 写死的代价之一。
- ⚠️ **一条事件的两头形状不同时要写成两条声明**(`payment` 的 `to` 可能是个人而
  `from` 是 `__town__`)—— 让一个 `owner_form` 管两头,是让作者在一个格子里说两件事。
- 契约四格纯增量:`projected_source_keys` / `projected_source_events` /
  `projected_owner_forms` / `projected_source_gloss`。
  回执 `docs/FOR-STUDIO.md` §3.42,`docs/REFERENCE.md` §10.13。

🔴 **同日裁决:上面那两条出路都建立在一个假前提上 —— 「旧路已经删了」。旧路没删,
它在 git 里。** `fa1507b^` = **`a6b3da3`**(第 1 期地基已在、needs 还没搬的那一棵),
`git worktree` 一敲就在,一趟采样 **3.8 秒**。实测(2026-08-26,`6d0d0bc` 工作树):

| 量的什么 | 结果 |
|---|---|
| 旧路(`a6b3da3`,叙事 + 判定**两池全关**)连采两趟 | **六格全同** |
| 插件路(HEAD,同样两池全关)连采两趟 | **六格全同** |
| 旧路 vs 插件路(都两池关) | **五格逐字节相同**;唯一的差是量表多三个 `needs.*` 键 = **搬家本身**(现有闸已逐键白名单) |
| 今天这份 golden(判定**开**着采的)vs 新基线 | `needs` / `prompt_sha` **逐字节相同**;差 `events`(103→99)、`state_sha`、`stocks` |
| 那 4 条事件差 | 点得出名字:**2 条 `memory_seed` + 2 条 `state_change`** = 关系判定那一次的产物 |

**判决:按 economy 那套范式重采一份(两池全关,先决定比什么再采),而"证据没被抹掉"
靠三件可复核的东西** —— ① 新基线由**一棵没有插件路的树**产出(判据是正的:
`git show a6b3da3:anima_world/needs.py` 里是那段命令式衰减,`factory_plugin` 不在
`dir(anima_world.needs)` 里);② 六格里 `needs` 与 `prompt_sha` 与今天这份逐字节相同,
变的三格逐条点得出名字(照 `fd1f20d` 那次"六格逐字节相同才敢说"的先例);
③ 旧那份留在 `fa1507b:tests/data/needs_legacy_golden.json`,sha256 与上表一起进新文件头。
⚠️ 采样有个坑:**`PYTHONPATH` 单独放不够** —— 本仓 editable 装的是一个 `__editable__`
的 meta_path finder,它比 `sys.path` 先说话,`anima_world.__file__` 会照旧指回工作树;
摘掉那个 finder(三行 `sitecustomize.py`)才真的跑在旧树上。**这一条没实现,只裁了**
(裁决轮不改业务代码);修完的判据是**连跑 9 趟全绿**,别只跑一趟。

### 第 2 期 2d / 2e 的另外两条裁决(2026-08-26,不改代码,只定去向)

**① 动词的 `target` 永远不会是 `agent`,而这不是"还没做",是这条路不从 affordance 走。**
`ontology.DECLARABLE_BUILTINS` 改一行就开得了,真代价在别处:**同意没有位置**。
affordance 的形状是「一个人、一样东西、一个瞬间」,target 格里放进一个人,`拜师` 就是
A 单方面把 B 变成师父、`提拔` 是单方面改别人的名分 —— 而这个引擎为「他肯不肯」建过
一整套东西(邀请三扇门 + `joint_gate` + `INVITE_OUTCOMES`),老板拍的第一条纪律正是
**拒绝必须是一等公民**。**放开的代价不是多一种开机失败,是把同意重新变成不可拒绝的**,
而它的样子是安静的:世界照跑、日志干净、边真的连上了。连带:affordance 一旦挂得上
`agent`,`spawn` / `destroys_target` 就自动对人成立 —— 而"造人"老板拍过走
`create_agent` 工具路,"抹掉一个人"在这个引擎里根本没有语义。
**去向**:人对人的动词走**工具路**(设计 §12.3 老板拍过的那句),共用
`ontology.apply_affordance` 这一个求值器(纯函数、只吃两边量表、不需要 `Entity` ——
**一份判断两种寻址**,不是第二份 requires/costs)+ 一道默认要点头的同意门,**排第 3 期**
(「她肯不肯」天然是一次判定)。**`give` 因此不搬**:它今天住在聊天工具那条路上
(意图分派 → `_ToolRuntime.give_item`),复刻成 affordance = 换一条调用路,而换了路的
"逐字节相同"从原理上不成立。回执 `docs/FOR-STUDIO.md` §3.39 ③.1。

**② contact 改判:不搬(第 2 期出厂插件 ×3 → ×2),三个表达式函数不从第 4 期提前。**
`relation()` / `co_located()` / `memory_contains()` 提前只买得到 contact 的三分之一
(`readiness` 还要读 blockers / mood / initiative / stance,一样不可表达);而
`closeness/urge/readiness` 答的正是「她想不想找你」= 设计 §9.1 自己那条判据里的**内核**。
`memory_contains()` 另有更硬的一条:「她记得没记得」该由判定回答,不是子串匹配。
**together 那一半可以搬,但它搬的是存储与过期规律**(三扇门留内核、签名一格不动),
还欠两件小的:边规律今天**只认 `set`、一条 `emit` 都不发**;`timer` 形状这一版不收,
过期得写成 number 事实 `expires_tick` + 规律比 `now`(`now` 在边规律命名空间里,实测在)。

**③ 顺带查出 2d 真正的拦路石是第五块**:`mode:"projected"` 的折叠端只认 `.delta` 后缀,
而设计 §9.3 说的是「`payment` 事件照旧是 `economy.coins` 的 delta」——**两句今天对不上**。
三条路里只有一条不破坏消费方:**`mode:"projected"` 多一格 `sources`**(声明哪些既有事件
是自己的 delta、从载荷哪一格取数、`transfer` 双端各记什么符号)。改发
`economy.coins.delta` 是给白名单事件改名(破坏消费方);两条都发是同一笔钱记两遍账。
回执 `docs/FOR-STUDIO.md` §3.40 末尾。

### 顺带:法务抹除回执那七句 gloss 是**玩家可见文案**,而第一版把它写成了开发笔记

两条都是 player 那侧的验收逮的,记在这儿是因为**回执板只有一块**:

1. **`receipt_count_gloss` 从没进过回执板**(视角 B)—— 它是第 1 期加的,而
   `git grep receipt_count_gloss docs/` 答空,REFERENCE 那一行还停在**六**格
   (第七格 `edges` 也没写)。**库里有而对方看不见,等于没有**,这一条这次咬的
   是我们自己。已补 `docs/FOR-STUDIO.md` §3.33 与 `docs/REFERENCE.md` §2.9.10.1。
2. **那七句的口吻错了**(视角 A)—— `facts` 那句摘掉 markdown 之后印在玩家
   `/account` 屏上是「……三档:整格缺席 = 这支引擎够不着量表 · null = 我没查成 ·
   0 = 我查过了…」,而站点按「不译」纪律**一个字改不动**。
   **裁决:这一格是玩家文案**,先例就摆在它自己的注释里 —— 它照的是
   `blocked` / `blocked_text` 那一对,而 `blocked_text` 就是印在玩家屏上的那一句。
   七句改写成第二人称、无 markdown、无实现词;**「三档」那层语义留在
   REFERENCE 与 `receipt_count_keys` 那一行**,gloss 只回答「这个数数的是什么」。
   判据 `tests/test_erase_player.py::test_回执那七句是给玩家看的话_不是开发笔记`
   逐句查 `*` / 反引号 / `null` / `缺席` / `三档` / 「引擎」/「字段」/「键」/ 第三人称。
   ⚠️ **改得动是因为链还没通** —— platform 还没把这扇门带下来,当时还没有一个
   玩家看见过旧句;链通之后改措辞就是改玩家屏。
   ⚠️ player 的前端 fixture 逐字抄了旧句,**它这一刻是过期的**;不必单独开一轮,
   链通那天随手刷新。
   ⚠️ 给 platform 的一句:`receipt_count_gloss` 是 dict of str,**会被壳那道
   按类型过滤的闸静默吃掉** —— 把它当 `dry_run` 的顶层兄弟键带下来,别塞进
   `erasure` 那个计数 dict 里。

### 第 1 期(下半):**needs 搬成第一个出厂插件**

设计稿 §9 的检验标准只有一条:**形状对不对,不看例子,看能不能把出厂的东西用同一
形状搬出去。** 老板那句「最关键的是,把我们目前的所有系统搞成出厂内置插件」的第一个。

**曲线的三个常数一个字没改** —— 搬的是**谁来跑它**:从 `Scheduler._settle_agent_needs`
里那段 Python,变成 `needs.factory_plugin()` 声明的**七条规律**,跑在世界自己那条
规律引擎上。值从黑板 + `:needs` 检查点表搬进**量表**(`stock:agent:<id>` 的
`needs.energy` 这三个键),黑板那几格从**真值**变成**每 tick 折一次的派生值**。
`settle()` 因此退役;`mood` 留下(`mood_of`,照旧永不存储);`URGENT` / `RELEASE` /
`need._restoring` 一个字没动 —— 那是行为树的迟滞,不是量的事。

🔴 **黑板那一格翻过来了,而这一条会咬人**:`blackboard.write("need.hunger", …)`
从今天起**下一 tick 就被盖回去**,而盖回去的样子是"我明明改了,世界不认"。
四个测试模块因此改成写量表(`tests/_needs.py::set_need`)。

#### 为什么是七条规律,以及它逼出来的那一处加法

`settle()` 一次做两件事:总是衰减,而且**如果她正在做那件恢复动作**就再加一份。
规律层没有"读她此刻在做什么"这种表达式,只有选择器 —— 所以每个量拆成**互不相交**
的两条(`{"action": X}` 与它的补集),两条**划分**了所有人,永不抢同一个量。
而 `social` 有**两个**恢复动作,单数的 `not_action` 排不掉两个 —— 于是
**`not_action` 3.8.0 起收一列动作**(`{"not_action": ["chat","idle_social"]}`,纯加法,
老的字符串写法一个字没变)。抢同一个量的下场写在 `evaluate_due` 里:后写的赢、
谁也看不见谁,**一个看起来对、算出来错的语义**。

#### `needs.enabled` 照旧是热的(而这一格差点丢掉)

它现在决定"装不装这个出厂插件",而那是**装载期**的事;REFERENCE §6 却逐字承诺过
这些键"全部支持热更新"。**一句写在文档里、而代码不再兑现的承诺,比没有这句话贵** ——
所以 `World.config_set` 上加了一道**通用**钩子(`FACTORY_SWITCH_KEYS`:出厂插件 id →
开关键那张表),改到表里的键就当场重装一遍。⚠️ 钩子里没有一个具体插件的名字。
⚠️ **关掉不删数据**,再打开从原处接着走 —— 删掉的话"关一下再开"会把她的精力悄悄补满。

#### §9.2 那道闸:逐位比过了,而**有三样有意不比,是量出来的**

`tests/test_needs_plugin_parity.py` 拿搬家**之前**旧路真跑出来的基线比
(`tests/data/needs_legacy_golden.json`,内置橱窗 288 tick):`needs()` 四个数**逐位
相同** · 量表**逐位相同**外加恰好那三个搬过来的键 · 三份 `debug_prompt()` 的 sha256
**逐字节相同** · `state()` 刨掉三格之后**逐字节相同**。

🔴 **刨掉的那三格(`narrative_log` / `recent_events` / `runtime`)与事件日志,
对任何代码都不可复现。** 拿**未改动的**引擎连跑两趟实测:开着叙事时条数就差
(110 vs 112);关掉叙事条数一样了,**次序仍然不同** —— 叙事跑在线程池上
("时钟永不等网络")。**所以任务单里"事件日志逐字节相同"那一条不是插件路做不到,
是这个引擎做不到**;写进闸里只会得到一条永远红的检查。

#### 顺手改准的两把尺子

`test_the_engine_scales_to_many_entities` 从前写死 `snapshot_kind == 10` 与
`evaluated == (100, 10000)`。needs 变成每 tick 到点的七条规律之后这两个数当然会变 ——
而它们变的那件事和这条尺子要量的"往返次数随不随实体数涨"**毫无关系**。改成
**每 tick 的开门次数有常数上限** + **求值次数的增量恰好是多出来的那些树** ——
两条都不随出厂插件增减而烂。⚠️ 同轮把 `snapshot_many` 从"禁"的那一列拿掉:
它是**批量门**(收一列 owner 一次 pipeline 问完),从前碰巧没被走到而已 ——
**"今天没出现"和"出现了就是坏"是两句话。**
⚠️ 另一处真的退化被这把尺子当场逮住:第一版把需求折上黑板写成了循环里的
`store.of(owner)`,3 个角色 120 tick = **370 次** `HGETALL` —— 正是
`RedisStockStore` 说明里点名的那个 72ms/tick 的形状。改成整批一次取回。

`World.rules()` 现在只报**世界自己写的**规律:插件那七条和它跑在同一个引擎里是对的,
**报在同一张表里不是** —— 那是作者问"我这个世界写了哪些规律"的地方。
插件那一份问 `plugin list`。

### 🔴 法务抹除终于够得着玩家身上那张量表(收件箱 D39)

`World.erase_player` 的 docstring 从 3.5.0 起就写着"把这个人的交互数据从世界里抹掉",
而 2026-08-26 量线上:`anima:*:stock:agent:player:*` 在 night-tide 上 **36** 行、
lighthouse-bay 上 **23** 行,**回执十四格里一格都不提**。

引擎给每个玩家开量表,和角色**同一个命名空间**(`agent:player:<id>` —— `me_*` 读的是
"一个人身上的量",而这件事对两种人是同一件),玩家一露面就播下,此后被 `costs`、规律、
`me_*` 读写。**它是这个人在这个世界里留下的一份数据。**

抹三样,都按 **owner** 走:他那张量表(`stock:agent:player:<id>` 整个 hash,顺带从
`stock_owners` 索引里摘掉)、`stock_places` 上他那一行(**那一行的 `label` 就是他的
显示名**;跑着的世界靠幽灵扫描迟早会清掉,**而抹除多半跑在一次性容器里**,那种进程
一个 tick 都不推)。⚠️ **`stock_visibility` 一个字不动** —— 那张表按 (种类, 量名) 声明,
玩家和世界里每一个角色**共用同一行**,跟着他一起删等于把所有人的「体力」从感知里一起
抹掉,而世界照跑、日志干净。**这一格是任务单里写反了的一条,记在这儿免得下一个人照着删。**

回执因此多**第十五格 `facts`**(删了几个量):**零也报 0,不许缺席** —— 老引擎是整格
**没有**(下游一律 `.get("facts", 0)`),而报 0 是在说"我查过了,他身上没有量"。
`dry_run` 只数不删;和转录、记忆一样**第一片就做完**,续跑从进度键带计数,不重数。
⚠️ **审计事件 `player_erased` 的载荷一个字没加**:回执是同步返回值,加格便宜;
事件是已经发出去的形状,改它贵。
`contract --json` 的 `erasure` 段多一格 `receipt_count_keys` 供探测。
CLI 那一行也印了这一格,**零也印** —— 「他身上没有量」和「这一版引擎不查这个」
在屏幕上必须分得开,而后者的样子是这句话整个不出现。

⚠️ **它靠重建镜像才到得了线上。** 在跑的镜像里那一版引擎抹不掉量表,
而它抹不掉的时候不报错。回执 `docs/FOR-STUDIO.md` §3.33。

### 重声明撤掉的量:不裁剪,但从今天起会点名(`dropped_quantities`)

重声明一个种类是**整行替换**,所以作者从 `kinds` 里划掉一个量,本体层就当它不存在了
—— **而存储那一侧一格都不裁剪**:值留在 `stock:<owner>` 里(规律不再更新它),
行留在 `:visibility` 里,而 `perception` 读的正是这两张表的交集,**它不问 `:kinds`**。
⚠️ **这两个是真键名**(`redis_state.py`):作者层那两个**段**才叫 `stocks` /
`stock_visibility`,而文档里照段名去 `redis-cli HLEN anima:{w}:stock_visibility`
会拿到 0 —— 和"这张表是空的"长得一模一样(2026-08-26 验收 C 挑的老账)。
下场是那个量顶着旧 label 与旧分档**继续进她的提示词**,一处不报错。
同一份文件里两条同 id 的 `kind` 会当场报错,所以这件事**只可能跨两次开机发生** ——
也就是它必然安静。

三条路上各印一行,而且是**同一个 token**(`dropped_quantities: {"tree": ["生长速度"]}`,
机器 `grep -o` 取得到):开机(拿新声明比这个世界库里留着什么,唯一答得全的一条)·
`world check` / `validate world`(只比得了同一份文件自己写下的 `stock_visibility` /
`rules` —— 抓的是"手改一份完整世界文件、划掉了量却忘了删可见性行")·
两扇门的 `--edit`(一次编辑包通常只带那条重声明的 `kind`,于是这一格**离线答不了**,
照 `me_X` 那条先例**说出来**,而不是假装查过了)。

⚠️ **这一版只吭声,不裁剪** —— 裁剪归插件命名空间那一期,理由和 `seed.kind_keys`
逐字同一笔账:收严会让写过额外键的已发布世界开不了机。
⚠️ **声明本身就是开关,这一层也不例外**:一个量都不声明的种类,它的量另有来路
(`world` 的季节与雨势写在作者的 `stock_visibility` 段里)。少了这道门,内置橱窗开机
第一句就是三条假警报 —— **实测过,`{"world": ["季节","雨势","雨天数"]}`** ——
而一句会误报的警告等于没有这条警告。REFERENCE §2.9.6.8。

### `contract --json` 多一段 `config`:引擎声明过哪些配置键

顶层从十一段变十二段。逐键 `{default, value_type, category, is_secret, description}`,
66 个 —— 字段名照 `config list` / `ConfigStore.meta()` 的原话,**不另起一套**
(同一样东西两个名字,就得有人维护一张翻译表)。

🔴 **这一段是「这一版引擎声明过什么」(静态),不是「某个世界现在是什么」。**
后者是 `config list` 的合并视图(环境变量 → 机器配置 → 世界 → 引擎默认值,带 `source`);
这里是那条链**最后一层**的原件,而且 `contract` 整条命令**连 Redis 都不连**。
混成一段就是 1.4.0 拆「创世播默认值」时治的那个病:播下去的是**创世那天的快照**,
引擎把 `chat.recall_k` 从 3 改成 99,已有的世界一个都吃不到,而 `config list`
看上去一模一样。这句边界写进了 docstring、REFERENCE §4.8 与 FOR-STUDIO。

⚠️ **密文键只报元数据,一格值都不报**:`is_secret` 为真的键 `default` **永远是 `null`**
—— 不是"这个世界没设",是**这一段根本不报值**。世界里一个 secret 都没有,
所以这里也不该有一个能放它的位置。

**它替创作台答上了一个一直答不出的问题**:`scheduler.max_agents` 此前只能"先建一个
世界再 `config get`"才问得到(`tool/docs/引擎接口诉求-人数上限.md` §7),而现在
**世界之前就问得到**。2026-08-26 拿 tool 那份 `cap_from_contract` **真跑过**:
它读 `config.<键>.default`,答 100,出处印成「这一版 core 的 contract --json
(config.scheduler.max_agents)」—— 那条路上写着"它一报出来就自动优先",这句话是真的。

**platform 的 `test/contract.test.js` 那条 deepEqual 不会红,而且是跑过的**:
它镜的只有 `storage` 一段(`STORAGE_CONTRACT`),顶层加段与 `erasure` 加格都碰不到它。
`ANIMA_PYTHON=<core venv> ANIMA_REQUIRE_ENGINE=1 node --test test/contract.test.js`
→ **16 pass / 0 fail**(2026-08-26,对着本轮工作树跑的)。⚠️ 照旧**真跑一遍再下结论** ——
这个仓库有过一条"报了会红其实是绿的"回执,也有过一条"报了不红其实会红的"。

### 事件白名单 `SUBSCRIBABLE_EVENTS`:第 1 期的触发器要订的那张表

进 `contract --json` 的 **`plugins.subscribable_events`**(⚠️ 这一期 `plugins` 段
**只有这一格**,`type:"plugin"` 记录那一摞是第 1 期)。**十条,策展的,不是全集** ——
引擎里在发的 type 有四十来个,其中一半是内部管道(`subsystem_health` / `memory_seed` /
`plan` / `legacy_seq_gap`),它们的载荷为引擎自己的用途服务,明天就可能因为一次内部
重构而变。🔴 **进了这张表就是一句公开契约,拿不掉**:加一条是加法(便宜),
删一条是破坏消费方(和改线格式同级)。

每条报 `numbers`(数字格,给算术)与 `parties`(当事人格,决定触发器的 `for_each`
对不对得上人)。**空列表不是漏了**,是"这类事情本身不带数"。

⚠️ **关系四轴不在表上**(老板 2026-08-26 拍的 D40 ③):插件读得到、emit 得出,
**写不进** —— 四轴是 `sentiment_delta` 的**投影**,直写等于把关系从"可重放"变成
"直接写"。所以它只以 `state_change{kind:"sentiment_delta"}` 这一种事件形式进来。
⚠️ **顶层的 `location_join` 不在表上**:它是创世时播下的一个**地点**(配置,
不是发生的事);"有人走进了一个地方"是 `state_change{kind:"location_join"}`。

### 顺带修掉:REFERENCE 里那两张对不上的事件表

§2.1 手抄的那张写着"投影器处理的全集"共十一条,而真实的处理器有**十七个** ——
`payment` / `item_transfer` / `agent_invites` / `invitation_settled` / `player_departed` /
`item_consume` 一条都没列,而下面那张 payload 表有十六行。**两张都没有闸**,
对不上多久了没人知道。合成一段之后这里不再抄清单,改成"三个问题,三处权威,
各自怎么敲"。

⚠️ **而"怎么敲"这一格自己差点又骗人一次**:`git grep -c 'def _apply_'` 答 **18**,
因为它把分发器 `_apply_event` 自己也数进去了,而处理器是 **17**。⚠️ **换成 `-n` 并不会
让那个数变对** —— 它照样是 18 行;`-n` 买到的是**那张单子摆在眼前**,`_apply_event`
那一行看得见、减得掉。**一个差一的数比没有数更坏**,它看上去像个读数。
这句更正是发版前照着自己写的命令敲了一遍才发现的:第一版写的是"别用 `-c`,用 `-n`",
**而那句话把「看得见」说成了「数得对」**。
⚠️ 同理,数"真在发哪几种"**要用 `ast` 不能用 grep**:`entity_spawn` 在两份文档的
`zcat | grep` 例子里各出现一次,grep 数出来是三处而真发它的只有一处;反过来,
一个只活在注释里的 type 会被 grep 判成"真有人在发"。判据
`tests/test_subscribable_events.py` 用的就是 `ast`,而且**它自己有牙**:
往表里塞一个设计稿里编出来的 `law_wanted`,它当场红。

## [3.7.0] —— 同一件事,一把尺一句话 (2026-08-21 定版;2026-08-26 发版前又走了两轮)

排雷单(`docs/任务单/2026-08-21-排雷.md`)。**升次版号是因为行为变了**,不是因为
加了作者层字段:`presence.enforce_colocation` 开着的世界里,"他在不在她跟前"这个
判断的答案换了一种情形(他自己在赶路),而下游按这个答案画按钮。
⚠️ **已发布世界的 `engine_min` 一格没抬**;动的只有橱窗自己的封皮 ——
`test_flagship_seed` 要求它写的就是产它的那一版。
🔴 **但"作者层 schema 一个字没动"这句话在这一版上是假的,别照抄**(它原先写在这儿,
是写在节拍那一件落进来之前的):作者层**多了第十二个段 `beat`**。老世界照旧开得起来
(没写就是这一层整个缺席),而**反过来不成立** —— 一份写了节拍的**新包**在 3.6.0 上是
**开不了机的硬失败**(实测 `WorldFileError: 不认识的 type 'beat'`,退出码 1),
和 3.6.0 的 `importance` 逐字同一种。**所以带节拍的包 `engine_min` 必须写 `3.7.0`。**

### 只读量一次:3.6.0 / 3.7.0 写下的世界,老引擎读得回来吗(排雷单 §3.core 第 7 件)

**零代码**(这一节 `anima_world/` 一个字节没动),三份包、两支真 venv、一台真 Redis
逐条敲出来的。完整那张表在 `docs/FOR-STUDIO.md` §3.30,这里只留三条判断:

- 🔴 **`world check` 对一份开不了机的降级包答了绿灯。** 一个跑过的世界导出来**只有
  状态层**,这扇门于是照实追加一句 warning「没有作者层,所以这里没有可查的东西」——
  **那句话是真的**,可 `loadable` 仍然是 `true`,而**机器只读 `loadable`**。
  实测:3.7.0 导出的世界在 3.5.0 上 `world check` 说绿、`import` rc 0、
  **真开机 rc 1**(`OntologyError: 不认识的字段 ['importance']`)。
  **和这一版上面修掉的 `--edit` 那一格是同一个形状**:不是"没说",是"说窄了",
  而读它的是脚本,脚本读不到那句解释。病灶只是换了一层 —— `importance` 随 `:kinds`
  走**状态层**进包,而这扇门只看作者层。⚠️ **有意没修**:三条修法(答 `false` /
  答 `null` 退 1 / 只加一格 `checked_layers`)各有舰队级后果,不该一个人拍,
  已记进总图契约表等开单。
- **`import` 一个字都不看 `engine_min`**(三次实测全 rc 0,包括那份开不了机的)。
  "装进去了"不等于"跑得起来"。
- **降级换钉的代价有一半是安静的**:老引擎不认识的**状态层键**(比如 `:beats`)
  装得进、开得起来、**剧情从此不响,日志干净**(实测同一份包 3.7.0 上响 1 次、
  3.6.0 上 0 次)。所以换钉往回退必须在目标引擎上真跑一遍,而且看的是"该发生的
  发生了没有",不是"有没有报错"。
- ⚠️ 顺带一个会让这类实验整个作废的坑:在 core 仓库目录里敲
  `旧venv/bin/python -c "import anima_world"`,`-c` 把 cwd 放进 `sys.path[0]`,
  **工作树那份盖掉了 venv 里的**,于是它自报 3.7.0。**你以为在验旧引擎,其实在验
  工作树,而两边都不报错。** 敲之前 `cd` 出去,并让它自报 `__file__`。

### Added —— 节拍过河了(看板 D1;创作台 2026-08-08 那份诉求 A)

节拍此前是这个引擎里**唯一一样作者写下、却进不了世界**的东西 —— 它只能靠 `--beats`
单独喂一个文件。于是这条链断在中间:

    工作台   写节拍 → beats.json          (本地试炼五拍全按顺序触发,验过)
             导出   → x.cyberworld        ← 节拍不在里面
    运维台   导入   → 只搬这一个文件进实例目录
    世界镜像 首启   → entrypoint 只在 /data/beats.json 存在时才传 --beats
                                          ← 那个文件从来没人放

**作者写的剧情在本地跑得通,在舰队上一拍都不响,而且没有任何一处报错**(世界照常
启动、居民照常过日子,只是那条故事线不存在)。病根是判断错位:节拍**是作者层**,
和人物、地点、关系同属"作者写下的",而它被留在了"作者层 / 状态层"这个划分之外。
按看板 D1 的选项 (a) 做:收进作者层,**交接仍然是一件产物**,契约的形状一个字没改。

- `AUTHOR_SECTIONS` 从十一个段变十二个:`beat` → `beats`。落库在
  `anima:{world_id}:beats`,**一个 list** —— 节拍是**有序**的一串,`after` 那条链和
  "先写的先判"都靠顺序;hash 的 field 顺序不保证,按 id 排序又会把作者写的顺序换成
  字典序,**而那个换法不报错**,只是剧情的次序悄悄变了。
- **首启不给 `--beats` 也带上**,这一条才是 D1 的另一半 —— 装得进去而首启不带,
  等于节拍进了包却仍然要靠那个参数才响,而舰队上没有一条路会去传它。
  🔴 **修补轮把这句承诺写窄了**:它在 `--world-file` 和"跑过的世界 export→import"
  两条路上成立,在 **`world import` 一份只有作者层的包**那条路上**不成立** ——
  `import` 只落键、不编译作者层(一直如此,有意的),那种包因此**一个键都不落**,
  world_id 仍然是**空的**,而**空世界首启装的是内置橱窗**:住着夏、遥、柔,
  不是作者写的那些人,退出码 0、日志干净。**这条路上丢的不只是节拍,是整个世界。**
  这一轮把那句警告改准(它从前一律说"状态记录已经落键,这个世界从此不是空的了"
  —— 对这种文件是**假的**,而假在最要紧的一格上,于是它把人推向"那就这样跑吧"),
  并补两条用例把"窄到哪儿"两头都钉住;**路本身通不通等拍板**(让 `world import`
  编译作者层 = 改它的语义 = 契约变更)。
- **`--beats` 仍然认,而且赢这一趟,但不写库**:它是一次明示的覆盖(试炼、调试靠它),
  让它写回去,一次试炼就会把作者的剧情换掉,**而且不报错**。
- **空的时候播一次,之后库里那份说了算**,没有"逐条合并"这一格:一条规律是**法**,
  而节拍是**剧情**,它和 `beat_fired` 那份历史配对;逐条合并等于让"第三幕"在第五幕
  之后插进来。⚠️ **不合并会说出来**(一条 warning)—— 不合并是有意的,一句话不说
  的样子却是"我把新剧情装进去了"。
- **"哪几拍响过"不落库,也不该落**:它是历史,从 `beat_fired` 重放。两份真相里存
  一份,另一份对不上的样子是"这一拍又响了一次"。
- **坏脚本当场开不了机,而且一个字都不写** —— 验在第一次写之前,和坏 `kinds` 同一条。
  ⚠️ **离线那两扇门跟着一起收**(`world_beat_errors`,和开机调**同一个**
  `BeatScript.from_data`):第一版把段收进来了却没补这两扇门,于是 `world check` 对一份
  **开不了机**的文件答 `loadable: true`(实测)—— 而那正是同一版上面那一节刚刚修掉的
  假绿。**新增一种开机失败,就必须同一轮补进这两扇门。** 节拍**没有跨引用**
  (`op` / `after` / id 重复全在包自己肚子里),所以 `--edit` 这一格也照查。
- 顺带修掉一个**哑参数**:`world export --beats FILE` 此前一路传到底然后什么也不做。
  一个"传了没报错、也没生效"的参数比没有这个参数更坏 —— 它让人以为剧情已经进包了。
- 顺带拆掉播种前那道 `isinstance(b, dict)` 过滤:它在**校验之前**把不是对象的那几拍
  安静扔掉。今天到不了(`author_records_to_seed` 先一步拦住),但它是一道**准备好静默
  丢弃**的闸,而这一层的失败方式恰恰是安静的。现在播的就是验过的那一份。
- 按出口探测:`contract --json` 新增 `beats.author_type` / `world_file_section` /
  `storage_key` / `report_section`。**别比版本号。**
  🔴 **修补轮更正**:第一版写的是「`author_type: null` = 老引擎」,而**没有任何一支
  引擎会答 `null`** —— 3.6.0 上 `beats` 段**有**、`author_type` 这一格**没有**(实测),
  照那句话写的探测器是 `KeyError` 退 1。**新加的能力格在老引擎上是「缺席」不是
  `null`**,这条对整份契约都成立(`erasure` / `invitations` / `seed.*` 一样)。
  **探测一律 `.get(段, {}).get(格)`,让「问不出来」落在一个值上,而不是落在一个
  异常上** —— 一个崩掉的探测器不是"探测出没有",它长得像"这台机器坏了"。

### Added —— `report` 那个差集:哪几拍白写了(FOR-STUDIO §0-② 的第四条)

创作台列的五个问题里唯一没答上的那一个,**欠了很久不是因为难,是因为没有分母**:
`beat_fired` 事件一直是现成的,而节拍从前只活在一个 `--beats` 文件里,`report` 这条
只读日志的路根本看不见它。节拍收进世界之后,这个差集才第一次算得出来。

- `report` / `simulate --report` / `World.report()` 多一段 `beats`:
  `declared` / `fired` / `unfired` / `fired_not_declared`。
- ⚠️ **`declared: null` 是"问不出来",`[]` 才是"这个世界真的一拍都没写"** ——
  合成一个的话,一份读不到剧情的报告读起来像一个没有剧情的世界。
- **屏幕上也说**:`anima-world report` 的人话输出与 `simulate --report` 那行摘要都点名
  白写的那几拍。诉求原话是"一拍都没响 = 这个脚本白写,作者必须当场知道" ——
  只进 `--json` 的话,走**默认那条路**的人屏幕上什么都没有,而"响了 0 拍"和"这个世界
  压根没有剧情"长得一模一样。
- 这一段是**加法**,`report_format_version` 仍然是 **2**(口径一个字没改)。
- 🔴 **修补轮:纯函数分得开,两个真出口把它合了。** `build_run_report` 那一侧一直
  是对的(`[]` 与 `None` 各一支,有用例钉着),可 `__main__.run_report` 写了
  `.definitions() or None`、`World.report()` 写了 `declared if declared else None`
  —— **在最后一步把 `[]` 压成了 `None`**。于是一个真的一拍没写的世界,`report --json`
  答 `{"declared": null}`,正是本文档 / REFERENCE / FOR-STUDIO §0-② **三处反复警告
  "不许合成一个"的那一合**,而三份文档都在,代码里没有一处会红。
  更难看的一格:用 `--beats` 跑过的世界答 `{"declared":null,"fired":["第一幕"],
  "unfired":null}` —— **响过一拍,却说不出分母**。
  两个出口都读得到库,所以**永远答得出分母**,一格都不该折;`simulate --report` 的
  分母改成 `[]`(它刚跑完这一趟,没有"问不出来"的道理)。
- 🔴 **同一处还少一格键**:`beats=None` 那一支只回三格,缺 `fired_not_declared` ——
  照着文档取第四格的消费方拿到 `KeyError`,而他取的正是这份报告说自己有的东西。
  **"问不出来"由值说(`null`),不由键在不在说**:后者逼每个调用方写一句
  `if "fired_not_declared" in …`,而漏写那句的人在自己的测试里看不出来。
  **四格永远都在。** 用例补了两条,其中一条走**真出口**(此前那三条用例全在纯函数
  那一侧,所以出口坏着而它们全绿):`git grep -n 'declared"\] == \[\]' -- tests/`。

### Added —— `chat --message`:一次一问(看板 D5;创作台从 2026-07-27 起卡在这条上)

在这之前 `chat` **只有 REPL**,而一个子进程驱动它只能喂 stdin、按提示符切 stdout。
那条路**脆在排版上**:抬头、降级提示、`名字 > ` 那几段任何一版换了样子,调用方就
切错 —— **而它不会报错,只会把半句抬头当成她说的话**。
库里 `World.chat_reply` 早就够,缺的一直只是一道门。

- `--message` / `-m` **可重复**,按顺序一句一轮,共用**同一份**进程内转录 ——
  多轮的连贯性因此还在(世界只收当轮有限历史,完整转录归宿主,这条一个字没变)。
  不进 REPL、**不读一个字的 stdin**、不要 tty(有一条测试往 stdin 里塞一句话钉住这点:
  喂空串的话,"说完就退"和"读到 EOF 才退"给出同一个答案)。
- `--json` 时 **stdout 上只有那一份 JSON**;散场那行「这一场引擎记了 N 条警告」
  **仍然印,只是走 stderr** —— 静音它就把"收着不丢"换成了"丢掉"。
- 回执带 `degraded_reason`:一个跑在 Mock 上的世界照样回得出话,而那几句是**模板**。
  **降级绝不无声。**

### Fixed —— 离线那两扇门的绿灯,三格假话(看板 D29 + 同族两条)

`validate world` / `world check` 存在的唯一理由是**离线答出「开机收不收」**。
它们在三格上答错了,而且**两个方向都有**:

| 写法 | 3.6.0 `validate` | 3.6.0 开机 | 3.7.0 |
|---|---|---|---|
| `--edit` + 量名拼错 / `spawn` 没代价 / 动词没 label | ✅ **说绿** | 🔴 挂 | 两边都 🔴 |
| 规律 `for_each.owner` 指着没声明的种类,而**一个 `kinds` 都没写** | 🔴 退 2 | ✅ 退 0,规律真的在跑 | 两边都 ✅ |
| 声明了种类,`stocks` 里量名拼错 | ✅ **说绿** | 🔴 `OntologyError` | 两边都 🔴 |

**比开机严是假红**(一份跑得好好的世界出不了包,而报错指着一个不存在的问题),
**比开机松是假绿**。两种都比没有校验器更坏 —— 它们教会使用者不信这扇门。

- **`--edit`**:从前整个跳过本体预检,而 `loadable` 就是 `not errors`。
  🔬 它不是"没说",是"**说窄了**":那一支追加过一句 warning「引用完整性没查:
  种类/地点/物品/规律可以来自目标世界」——**那句话是真的**,可它只解释得了被跳过
  的那一摞里的最后一件。**人会拿一条真的理由去覆盖整个遗漏。**
  现在分界是**查得动查不动**:包自己肚子里那几件照查,跨引用照旧豁免。
  ⚠️ `me_X` 是个例外(2026-08-21 实测更正):它查的是 `agent` 种类声明过的量,
  而一份只改某个种类的编辑包完全可以不重声明 `agent` —— 硬查就是假红。
  这一格在包没声明 `agent` 时跳过,**而且说出来**。
- **第二行**:这扇门把「**声明本身就是开关**」那条按住了。不写 `kinds` 的世界这一层
  整个缺席,开机一直这么判(`if … world_seed.get("kinds")`),而这扇门无条件跑预检。
- **第三行**:量名那道闸只住在**播种**里,而播种在预检**之后**。搬进预检之后顺带
  修掉一个更贵的:开机的那次失败从前发生在**写过几张表之后**,留下一个装了一半的
  世界 —— 而那正是 `_precheck_ontology` 当初被开出来要修的形状。**验不过就一个字都不写。**
- 🔴 **修补轮:「量名」是两支,而 `--edit` 那条路只接上了一支。** `_package_only_ontology_errors`
  只跑了 `parse_kinds`(管 `set:`/`costs:` 里读写的名字),**没调** `_undeclared_stock_names`
  (管 `stocks:` 里写初值的名字)。同一份包:`world check --edit` 答 `loadable: true`、
  `validate world` 退 2、真当编辑合并进去**当场 `OntologyError`**。
  **而更贵的是那句 warning 正面撒谎** ——「包自己肚子里那几件(**量名**、动词 label、
  `spawn` 代价、不认识的字段)已经查过了」:`set:` 那一支确实查了,`stocks:` 那一支
  一个字没查。**一句说得比做到的宽的话,和一盏假绿灯是同一件事** —— 人正是拿它去
  决定不再自己查的。用例也漏在同一处(`test_编辑包里量名拼错_必须红` 只钉了 `set:`
  那一支),所以整套测试全绿而这一格坏着。现在两支各一条用例,warning 那句写明是
  哪两支。⚠️ owner 所属种类这份包没声明时照 `me_X` 那条先例**跳过并说出来**
  (硬查是假红:那份声明在目标世界里);合成的 `agent` 替身**摘掉再查 `stocks`**,
  否则一份给 `agent:甲` 写初值、没重声明 `agent` 的正常编辑包会被判成假红。

⚠️ 顺带纠正**看板 D20 的标题**:「示例作业产出的世界,3.x 上一个都开不了机」——
那份世界**开得起来**,坏的是**出不了包**。「开不了机」和「出不了包」得分开说。

### Added —— `contract --json` 的 `seed.kind_keys`,以及 `parent` 终于进了回执板

`parent`(单继承 + 加载期 copy-down)**从 2.0 起就能用**,而 `docs/FOR-STUDIO.md`
§3.7 到 2026-08-21 都没写过它 —— 于是创作台不认这一格,对着一份**完全合法**的声明
产出过一条**假红**,作者照着去改,改掉的是没错的东西。
**回执板漏一格,下游就多一条假警报。**

修法两半:§3.7 补上 `parent` 的语义(六条,数字全是 `parse_kinds` 直接跑出来的),
外加一格 `seed.kind_keys` —— **消费方问这一格,别照文档维护一份清单**。
⚠️ 它是**"读得到的那几格"的清单,不是一道闸**:能力级的不认识字段当场开不了机,
而**种类级的今天被静默忽略**。收严那一条这一轮有意没做(会让写过额外键的已发布世界
当场开不了机),而这份清单本身就是为了让人**先看得见**才写下来的。

### Changed —— `doctor` 的长过程那一格:**账要全,判要新**(看板 D25)

从前那一格按**全量事件日志**判,于是一个世界历史上出过一次「起了头就被排班带走」,
`doctor` **从此永远退 1** —— 而 `CLAUDE.md` 同时写着它能进 CI。
**一条永远红的检查等于没有这条检查**:人只会 `|| true` 掉它,真出事那天它照样是红的、
照样没人看。

现在屏幕上那几行照旧报**这个世界的一生**(事件日志只增不减,这笔账不许少),
退出码只看**本次开机以来**。窗口是新的 `:meta` 一格 `run_since_seq`
(`Scheduler.RUN_SINCE_SEQ`),**世界这一趟推第一 tick 时**盖的戳。

⚠️ **盖戳的时机不是 `World.open`**:只读的门(`map` / `prompt` / 运维脚本)每开一次
世界也走 `open`,拿它当"开机"的话,一次 10 秒的 `anima-world map` 会把水位推到最新,
紧接着的 `doctor` 报一句「本次开机以来 0 件」的绿勾 —— **而那句话什么也没度量**。
⚠️ **戳不在时不许悄悄放行**:退出码照旧按一生算,并印出"我为什么答不出最近那一段"。
⚠️ 它是**进程态**,`world export` 剥掉它(和 `owner_pid` 那三格同一条)。

真 Redis 上复演(一个"出过一次事"的世界,重启后什么也没做):

```console
[第一趟之后]                    ! 起了 1 件,做完 0 件,1 件半路被带走(代价不退)
                                其中 1 件是本次开机以来的(事件 #23 之后)—— 退出码记的是这一格
                                3 项需要处理
[重启之后(这一趟一件都没丢)]  ! 起了 1 件,做完 0 件,1 件半路被带走(代价不退)
                                本次开机以来(事件 #29 之后)一件都没被带走 —— 退出码不记这一笔
                                2 项需要处理
```

⚠️ **进程退出码两次都是 1**,而那不是这一格的锅:`doctor` 的退出码是**总账**
(这台 Redis 没开 AOF、这个世界没配 LLM 各记一笔)。要单项结论就读那一行 ——
这一条 `CLAUDE.md` 早写着,而我在写这次复演脚本时**先照 rc 断言了一遍,当场撞上**。

### Added —— `World.invitation_outcomes_page()`:结局那扇门也有游标了(看板 D23)

在这之前一份邀请的**结局**只住在两个有上限的地方:`state()` 的 `recent_events`
(壳截 40 条)和 `invitations_page()` 每行那格 `outcome`(只记最近 200 份)。
玩家在手机上离线几分钟回来,那条「她已经走开了」就掉出窗外,屏幕上印的是
「你错过了」—— **不是显示错,是彻底没有**,而链路上一处不报错。
**把她做的事记在他头上,是这条链上最贵的一种错。**

新门是 `_filtered_page` 的**第四个**消费者(游标语义和另外三扇逐字相同,
空页也推得动游标)。**有意没有非分页的姊妹方法** —— "只给最近 N 条"正是这个 bug
本身,再造一个等于把 40 换成别的数字。契约上按出口探测:`contract --json` 新增
`invitations` 段(`outcomes_api` **缺席或为 `null`** = 这支引擎没有这扇门;
🔴 **3.6.0 上是整段缺席**,实测 —— 探测写 `.get("invitations", {}).get("outcomes_api")`)。

### Fixed —— 「在路上」这件事,引擎对她和对他用的是两把尺(看板 D24)

`_ToolRuntime.face_to_face()` 判她用 `scheduler._transit`(**在途 = 不在任何地方**),
判他用 `World.player_location()`(**在途 = 还算在出发地**)。于是他**走在路上时按
「好」,一起做事真的做成了**。真 Redis 上复演(基线 `4cb4aff`,`git archive` 出的
临时树):

```console
③ 他在路上按「好」: {"ok": true, "outcome": "accepted", "changed": {"坐过几回": 1.0}}
```

修完同一句复演答 `{"ok": false, "gate": "player_in_transit"}`,`坐过几回 = 0.0`;
**对照组**(他落了脚再按)照旧 `ok:true`、量真的动 —— 一条期望 0 的判据旁边配一条
期望非 0 的,否则"按不动"这个结论对一个整体坏掉的夹具同样成立。

这一头的答案只准有一句(`World._player_here()` 的 docstring 2026-08 就写死过:
「**新开的门问它,别在门上再写一遍**」),而这扇门当年正是在门上又写了一遍。
连带 `together.COLOCATION_GATES` **从四条变五条**,多的是 `player_in_transit` ——
**别把它并进 `player_where_unknown`**:世界知道他在哪,他在从 A 去 B 的路上。

### Fixed —— 同一时刻,两扇门给玩家两种互相矛盾的回执(看板 D27)

她在 `cafe → workshop` 的路上、他站在广场时:

```console
# 基线 4cb4aff,真 Redis 复演
① 他动手那扇 : 这件事要当面才办得到 —— 你在广场,苏晚夏在咖啡店。隔着这么远,你只能跟她说话
② 他按好那扇 : 苏晚夏这会儿在路上,还没落脚 —— 不是你不在
两扇门同句? False
```

**两个地名都对,合起来是一句谎** —— 咖啡店是她的**出发地**。病根不在措辞,在判据:
`World._colocation_error` 与 `intent.Director._colocation_refusal` **各写了一份逐字
同构的三分表**,都拿 `here == where` 去猜她在不在赶路,而他站在**别处**时两扇都掉进
`else`。他在别处是最常见的那一种,所以这句谎六轮没人逮到;第六轮把地名改成人话之后
**它更像真的了**。

修法是两半,缺一半都不算修:**判断**收进 `_ToolRuntime._colocation_gate()` 那一个
枚举(三扇门都问它,一处位置比较都不再自己做),**句子**收进
`together.colocation_line()`(三扇门取同一份词)。2026-08-20 已经收过其中一支
(`_where_unknown_line`),而**收一支、留三支各写各的,买到的是"看起来治过了"** ——
两个验收员各自独立把那条测试读成了"两扇门整体统一了"。

`intent` 那扇门顺带修掉一处**把"查不到"说成一件很具体的事**:`elif not here or
here == where:` 把「世界压根不知道她在哪」印成了「她这会儿在路上」。
`reason` **只加不改** —— 三个老取值一个字没动,新的两支各给一个新名字
(`agent_where_unknown` / `player_in_transit`),同时多一格 `gate`(整族的判据)。

### Changed

- `intent` 那扇门的回执多一格 `detail["gate"]`(只加);`reason` 多两个取值。
- `together.GATE_LABELS` 多一行 `player_in_transit`;`COLOCATION_GATES` 五条。
- `api._where_unknown_line()` 降成一层转发,留着名字是当病历用(见它自己的 docstring)。


### 发版前那一轮:两条会替引擎认罪的判据,以及一笔迟到三周的回执 (2026-08-25)

清欠账那一单(`../../docs/任务单/2026-08-25-清欠账.md` —— ⚠️ **任务单住在调度台目录,不在本仓**;
上面 3.7.0 那一节把它写成 `docs/任务单/…`,照那条 `ls` 会一无所获)。**没有一行引擎代码改动**:
这一版动的全是**判据**与**回执** —— 而这两样烂掉的样子,恰恰是这个仓库最怕的
"照跑但给错东西"的文档版与测试版。

### Fixed —— 判据自己在撒谎(两处,都不是引擎的病)

- **`tests/test_autonomy.py` 那 24 条共用的 `_settle()`,判据是一个 5 秒挂钟。**
  2026-08-25 四仓测试并行跑那趟,`test_a_broken_decision_call_never_takes_down_the_clock`
  FAILED,而同一份代码单跑 0.17 秒绿、整文件绿、独占重跑全绿。到点就返回一个**还没
  落地**的空 `predicate()`,于是红出来的是**调用方自己的断言**——「定时轮次没走通」。
  **它红出来的样子和真的把时钟拖垮逐字相同**,而下一个看见它红的人多半在 CI 上,
  没有第二台机器可以复核。现在按事件等:`_drain()` 往世界自己那条循环上再投一个协程,
  等掉它眼前排着的 task(`tick(n)` 是同步的,返回时那 n 轮已经全排上了)。
  `timeout` 参数一并删掉 —— 留一个能调的秒数,等于把"等够了"重新变成判据。
  剩下的那个数只当**兜底闸门**(`ANIMA_TEST_ROUND_GUARD`,默认 60 秒),而且
  **跳了就当场说清是这台机器,不给结论**。
- **同族的另一处**:`test_she_can_act_on_the_world_and_not_only_on_people` 默认
  `interval=1`,第一个 tick 就起一轮、当场用掉那句脚本化的回复并把树照料了,
  而"照料之前的读数"取在它后面 —— `树高 > before` 靠的是一场竞赛。改成 `interval=2`。
  判据(同机、40 路 busy loop 满载、整文件各 6 趟):**改前 6 趟里 2 趟红**
  (`assert 3.254 > 3.254`)、13–17 秒;**改后 6 趟全绿**、5–9 秒。
- **同一个文件里剩下的最后一处真挂钟(同日第二轮补掉)**:
  `test_the_clock_never_waits_for_the_network` 的 `assert elapsed < 0.2`
  —— 让 LLM 睡 0.5 秒,量 `tick()` 花了多久。上一轮量到"现敲有余量、load 20.5 下连跑
  6 趟全绿"就放过了它,**而余量不是判据**:它和上面那条同族,**红出来的原话是
  「tick 被 LLM 拖住了(0.31s)」—— 一句在指控引擎的话**,真相多半是这台机器那 0.3 秒
  没排上 CPU。现在判据是一个**事实**:把那次调用卡在一扇只有测试开得了的闸上,问
  **`tick()` 回来的那一刻,那次调用做完了没有**。没做完 = 时钟先回来了;做完了 = 它
  只可能是被那次调用本身放回来的。三条判据:① 兜底闸门 `ANIMA_TEST_ROUND_GUARD`
  从 1 拧到 600(600 倍)—— 两趟都 **26 passed**(改之前那个 `0.2` 就是答案本身:
  在 `git archive HEAD` 的临时树上把它拧成 `0.0`、**引擎一个字不碰** → 当场 **1 failed**);
  ② 反向突变 —— 在临时树上给 `_on_autonomy_due` 的 `future` 加一句 `.result()`
  (= 时钟真的在那儿等网络)→ 当场 **1 failed**,而且报的是
  「`tick()` 回来的时候,那次 LLM 调用**已经做完了**……:['anima-chat-loop']」,
  指着的是对的东西;③ `grep -n '^import time' tests/test_autonomy.py` → **0 行**
  (`DecidingLLM` 的 `delay=` 旋钮一并删掉 —— **一个"睡几秒"的旋钮就是一把挂钟**,
  留着就是给下一条测试留一个坑)。`_GatedLLM` 从文件末尾那节自测里搬到开头,
  和 `DecidingLLM` 并排:它现在是这个文件的第二种 LLM 替身,不再是自测的私产。
  ⚠️ 顺手复量了文件头那三个 `_drain` 的数(它们是在一份已经不存在的文件上量的,
  而没有一处会因为文件变了而变红):**9 / 17 与 2 / 24 一位没动**,复量命令写进了文件头。

### Docs —— `docs/ARCHITECTURE.md` 逐节核过一遍,盖十个章而不是一个数

上一轮只把那句假的版本戳(「对应引擎版本 1.0.0(db 格式 1)」)**拿掉了没盖新的**,
理由是"没逐节核过就盖,等于把一句烂话换成一句新的烂话"。这一轮把那件事做完了 ——
而**核完发现"盖一个数"本身就是错的做法**:十节漂的距离差着两个数量级,
§10 整节是 1.0.0 的病历,§4 停在 1.x 的 6 步帧,而 §2 的道理一个字都没错。
所以抬头换成一张**逐节盖章表**:一节一个日期、**一条敲得动的判据**。

逐节的处置(全部现敲复核过):

- **§1**:那段示例 `World.open("saves/world.db")` —— **照着敲会当场报错**。换成今天真跑得动的
  三行,并把实跑输出贴上(⚠️ `tick` 那一格是 **462** 不是 288:创世把时钟拨到了
  `world.start_time`,而 `tick(n)` 是**再推 n 下** —— 两个数看上去都很像对的)。
- **§2**:道理一个字没动,**衬底从 SQL 表名换成 Redis 键**;补上第二条分家线
  (`mysql=` 的四样)与"她带不带得进上下文"那条可验的判据。
- **§3**:线程形态**重新实测** —— 多了一条 `anima-chat-loop`(`open` 就起,聊天/自主/contact
  全跑在它上面)。🔴 更要紧的是**第 1 条硬约束今天整个反过来了**:上一版写"一个运行中的
  世界**独占**它的 `world.db`",而今天世界住 Redis、**很多进程可以同时驱动它**
  (`RedisLock`)。代价换了形状没有消失,换成了投影水位那条。
- **§4**:一帧从 **6 步**改成今天的 **15 步**(规律、长过程收尾、邀请结算、自主、contact、
  量与位置各自的结算全是 2.0 之后长出来的),每一步为什么排在那儿也一并写清。
  日切那一节补上"固化与衰减是 if/else 不是两段都跑"。
- **§5**:三个池仍然对,但**漏了第五个执行体**,而 2.0 之后最要紧的几件事都跑在它上面。
- **§6**:原先只有"需求带 + 作者的树"。补上 2.0 之后的三层(本体与四种代价、感知、
  自主轮次)和"拒绝是四类不是一类"。
- **§7**:地图上画着 `db.py` 与 `graph.py`,**两个文件今天都不存在**;契约那张表补上
  `world_file.py`、注明 `lib/worldSeed.js` 已删。
- **§8**:🔴 **九条不变量里四条讲的是已经不存在的东西**(Fernet 密钥随 db 搬迁、
  `executescript()` 的事务、`DB_FORMAT_VERSION` 联锁、`world_seed.json` 是唯一 package data)。
  **一份写着四条作废不变量的"关键不变量"清单,比没有这一节更坏。** 这一节因此不再列清单
  —— 病根不是那四条写错了,是**它从一开始就是 `CLAUDE.md` 的一份镜像**,而镜像必然、
  且安静地烂掉。现在只留指路牌 + 三条"属于为什么而不属于是什么"的。
- **§9**:图里的 `world.db` 换成 Redis;"协作只有两种"补一句 —— 系统里**确实有 HTTP**,
  只是它不在引擎里(读的人会照那句话去世界容器上找门)。
- **§10**:🔴 **有意保持原样,并在抬头钉死它是 1.0.0 的病历。** 删掉等于把"当初为什么
  这么做"一起删了;而那几笔债的形状在 Redis 上一样会长出来 —— 投影那一笔**真的又长了一次**,
  已就地接上今天的判据(`tests/test_cross_process_projection.py`)。"还欠的"四条逐条现敲复核,
  **四条今天仍然成立**,其中"经济没有劳动"那条只剩一半(工资 2.0 之后按真上过班的 tick 数发)。

🔴 **顺带交代一条这份文档特有的、会咬人的事实**:`tests/test_reference_docs.py`
**只读 `REFERENCE.md` 与 `FOR-STUDIO.md`**(判据 `git grep -n '_DOCS_DIR /' tests/test_reference_docs.py`
→ 两行),所以 ARCHITECTURE 上**没有任何一道闸** —— 在这里写一个不存在的符号名、
画一个删掉的模块,测试一条都不会红。`db.py` 就是这么在地图上多画了半年的。
那张逐节盖章表是这道缺失的闸的**替代品**:它不会自己变红,但它给了下一个人一条一条去敲的路。

### Added —— 一道此前完全不存在的闸

- **`test_every_declared_config_key_is_documented`**:`config_store._DEFAULTS` 里的
  每个键都必须在 `docs/REFERENCE.md` 里出现过。**加上它当场红**,红的正是下面那一个键。
  公开方法早有闸(`test_every_public_method_is_documented`),**配置键此前一条都没有**
  —— 而配置键恰恰是运营与作者唯一够得着的那一面。

### Docs —— 回执(库里有而对方看不见,等于没有)

- **`scheduler.max_agents` 补上了迟到三周的回执。** 能力 **2.0 就交付了**(`3254f36`,
  **写代码是 2026-08-05(`3254f36`),发出去是 2026-08-06 的 2.0.0 定版(`655988b`)** ——
  第一版把这两个日子写成了一个,判据 `git log --no-walk --date=short --format='%h %ad %s' 3254f36 655988b`):`int`、默认 100、**`0` = 不限**、报错里带着
  `config set scheduler.max_agents N` 的解法。而到 2026-08-25 为止,REFERENCE 零行、
  FOR-STUDIO 零行、CHANGELOG 零行、issue #19 还开着 —— 一个照文档判断"这引擎有没有
  人数上限"的人,三周里拿到的答案是"没有这回事"。现在 REFERENCE 配置表有它,
  FOR-STUDIO 新增 §3.31 逐条对账那份诉求。
  ⚠️ **实测补一条你们会关心的**:这个数**进不进包取决于有没有人动过它** ——
  没人动过就一个字都没有(创世不播引擎默认值),在那个世界上 `config set` 过就住进
  `:config`、**跟着导出的包走**(实测 `zcat pkg | grep -c max_agents` → `1` 对 `0`)。
- **FOR-STUDIO §5「已知的洞」整表复核。** 四行把「排 1.4.0」当排期写,而 1.4.0 是
  2026-08-04 发出去的那一版,那个里程碑再也不会到来。⚠️ 这不是一句假承诺,是
  **一个已经不存在的坐标系里的真承诺** —— 更难发现,因为字面上没有一处错。
  逐行换成今天的实况并带上复核日期与敲得动的判据:autonomy 统计 / 感知 / 东西身上的量
  三格**已经到了**(`doctor` / `prompt --json` 的 `perception` 块 / `ontology --json`
  的 `values`);`rule_stats` 与**她身上的**量仍然没有 CLI 出口;「挖矿让矿脉减少」
  2.0 起用**能力**写得出来,欠的是**规律层的扇入**。
- **GitHub issue 清账(2026-08-25 真的关了,判据 `gh issue list --state open` → 只剩 #8)。**
  六个"做完没关"的:**#19** 人数上限(2.0 `3254f36`)· **#13** 角色会来找你
  (1.2.0 访客模型 → 3.2.0 在场进 Redis → 3.6.0 邀请门)· **#15/#16/#17/#18**
  chat-agent 四条(1.3.0,四个开关默认关)。每个 issue 上留一条**关掉之前**写的对账:
  落在哪一版、怎么开、判据是哪条命令,以及**它和 issue 原文差在哪儿**
  —— #15/#16/#18 都写着"每轮发一个事件",而那条被有意否决了(理由在本文件 1.3.0 末尾
  「一处对 issue 文本的偏离(有意)」),观测量落在 `messages` 行上;#15 还差在
  不走 OpenAI 原生 `tools=`(没有 key 是默认状态)。**#8 是真 open,一个字没动**
  —— 它问的是"下一个主版本落地时,已有的世界怎么办",那是一条**还没有答案的政策**,
  不是一件做完没关的事。
  🔴 **这一条自己就是一次现场教训,记在这儿**:上一轮写下这段话的人**先把账记好了**
  ——「六个关掉」这句话写进 CHANGELOG 躺在工作树上等着被提交,而那一刻**七个 issue
  一个都没关**。一句说得比做到的宽的话,而且它不会有任何一处报错:CHANGELOG 里
  没有闸,`git status` 只说这个文件改了。**判据必须是敲得出来的那一条**,
  写在句子里的"已经关掉"从来不是判据。

### 第二轮(修复轮,2026-08-25)—— 上面那道新闸,自己就是它要挡的那种假话

这一轮同样**没有一行引擎代码改动**。三条都是验收拿探针量出来的,不是读代码推的。

- 🔴 **`test_every_declared_config_key_is_documented` 会被一个提示词模板名满足。**
  第一版的判据是"这个键在 REFERENCE 里被反引号包着出现过",而 REFERENCE 里反引号
  token 有 **1017** 个、带点的 **239** 个,其中只有 66 个是配置键 —— 剩下 **173** 个
  带点 token 里,`prompt_store._SAMPLE_VARS` 那 36 个**提示词模板名**占了 28 个,
  而 `contact.compose`(模板)与 `contact.compose.enabled`(配置)正好同族。
  **这是现实的洞,不是理论的**:验收探针往 `_DEFAULTS` 里塞一个很像会有的开关
  `chat.intent_classifier`,这道闸答 **1 passed** —— 满足它的那个反引号,
  说的是一条提示词模板的名字,和"这个世界有没有这个开关"一个字都不相干。
  **又一次:证据成立,而它证的不是你以为的那句话** —— 而这一次犯的人,
  正是上面那一段刚写完"配置键此前一条闸都没有"的同一个人。
  现在判据是**"配置表上有它自己那一行"**;家在正文的那一个
  (`economy.player_allowance`,进表会把那整段 ⚠️ 说明拆散)走一张**列得出名字**的
  例外表,而那张表**自己也上了闸**:名字不再是真配置键就红(防它比代码老)、
  键在正文里一个字都没有也红(例外表放行的是"不进表",不是"不写")。
  判据自己也钉了一条自测:`test_the_config_key_gate_is_not_satisfied_by_a_prompt_template_name`。
  **两个方向都敲过**:同一个假开关,`git show HEAD:tests/test_reference_docs.py` 那版
  **1 passed**、新版 **FAILED**(报文点名 `chat.intent_classifier`);
  66 个真键 **65 个走表格行、1 个走例外表**,整个文件 **10 passed**。
- **`_settle(world, predicate)` 的返回值,24 个调用点里 20 个丢掉了。**
  谓词只求值一次、**不参与任何判断**,等待全在 `_drain` 里 —— 于是
  `_settle(world, lambda: world.autonomy_stats()["failed"])` 读起来像"等到 failed 非零",
  实际什么都不把,还挡在真正的 `assert` 前面替它顶了个名。
  **一条看起来在把关、其实什么都不把的判据,出现了 20 次。**
  那 20 处改成直呼 `_drain(world)`(等待一个字没少,少的只是那句假话),
  其中 3 处连 `_drain` 都是多余的(循环体最后一句就是它)——直接删。
  `_settle` 只留给**返回值真的被用上**的调用点,现在 5 处。整文件 **26 passed**。
- **`tests/test_autonomy.py` 文件头写下 `_drain` 的射程,因为"全绿"证的不是那句话。**
  验收量出 barrier 第一眼看见的 `pending` **每一次都是 0**(我复现:26 条里 24 条调过
  `_drain`,其中 22 条引擎测试的每一次调用都是 0,只有那两条判据自测量到 1),
  据此判"barrier 什么都没等"。**前半句我复现了,后半句我核伪了**:`_drain` 是两半,
  ① 一次循环往返(顺带让排在前面的回调跑完)、② 到了那边再等掉眼前排着的 task。
  把 `_drain` 整个改成 `return` → **9 failed / 17 passed**(所以 ① **承重**,
  7 条引擎测试真的靠它);只把 ② 那段 `while pending: await asyncio.wait(pending)`
  换成 `pass` → **2 failed / 24 passed**,红的正是那两条判据自测(所以 ② 在那 22 条
  引擎测试上**一次都没走过**)。两句话都写进了文件头:**别说 barrier 没等**,
  也**别拿"引擎那些测试还是绿的"当 `_drain` 的验收**。
- **`test_packaging.py` 里那条"先严后松"的中间档,在这台机器上是死的。**
  `.venv/bin/python -c "import setuptools"` 是 `ModuleNotFoundError`,于是
  `--no-isolation` 一开口就是 `BackendUnavailable`,连 `Building sdist` 都到不了 ——
  **真实行为是"联网就 pass、断网就 skip",没有中间档**。那一档留着仍然对
  (换一台装了 setuptools 的机器它就活),但**它此刻没被任何一次运行证过**,
  docstring 里照实说了。把"我写了一条退路"当成"这条退路走得通",是同一族的第三种。
- **本文件抬头那句"这里刻意没有 `[Unreleased]`",和它下面那个 `[Unreleased]` 打了一整轮架。**
  已改成这一单实际在用的规矩(见抬头)。**两句打架的话比其中任何一句都糟** ——
  它不会红,只会让下一个人在版本标题下面找一个不在那儿的条目。

### 发版:3.7.0 是自 1.4.0 之后第一个真的上索引的版本(2026-08-26)

✅ **发出去了。** tag `v3.7.0` 指着 `5f44a97`,`release.yml` 五段**一次全绿**
(run `32941362138`):`verify` 3.11/3.12/3.13 · `build` · `testpypi` · **`smoke`** · `pypi`。
**`smoke` 那一段是这条链上唯一"外面真的能用"的证据**(它从索引装回来再跑一个世界),
而它此前一次都没跑绿过 —— 下面那一整节记的就是发版前把它逐条敲通的经过。

**没拿绿勾当答案,在索引上又敲了一遍**:`pip index versions anima-world` → `LATEST: 3.7.0`;
干净 venv 从公网 `pip install anima-world==3.7.0` → `anima-world --version` 答
`anima-world 3.7.0 (存储:redis;包格式 3)`、`contract --json` 的
`engine_version`/`storage.backend`/`beats.author_type`/`invitations.outcomes_api` 全对、
`pip show` 那格是 `License-Expression: AGPL-3.0-or-later`、拿它真跑了一个世界(rc 0);
PyPI 的 JSON 面自报 `latest=3.7.0`、`license_expression=AGPL-3.0-or-later`、
wheel 与 sdist 两个文件都在;装出来那份橱窗 `cdn.animametaverse` **零命中**。
⚠️ **索引上从此是两个孤岛**:`1.0.0…1.4.0` 与 `3.7.0`,`2.x`/`3.0.0–3.6.0` 中间全空,
**而且以后也不会有** —— `pip install anima-world==3.4.0` 答"这个版本不存在",**不是坏了**。

老板拍了 D2「做,发 pypi」。这一节记的是**发版前把整条管线在本地逐步复现验通**的结果 ——
`release.yml` 的 `verify → build → testpypi → smoke → pypi` 里,**smoke 是唯一一个挂掉就把
版本号烧在 TestPyPI 上的阶段**,而它此前从没跑绿过(v3.0.0 那次死在它前面的 `build`,
所以那条路上的断言一次都没被真运行证过)。

- **smoke 那几条断言逐条对 3.7.0 敲过,全部成立,`release.yml` 一个字都不用改。**
  条件按它的真实形状摆:`python -m build` 出来的 wheel → 装进一个干净 venv(依赖从
  PyPI 真下)→ 打一台真 Redis,**不是在工作树里跑**。`World.open(id, redis=…,
  force_mock_llm=True)` / `state()['agents']` 三个人 / `tick(100)` / `memories(...)` 非空 /
  重开一次 `state()['world_time']['tick']` 还等于那个数 / `anima-world --help` /
  `simulate --redis … --llm mock` / `contract --json` —— 连敲三趟,趟趟绿,每趟约 1 秒。
- **`pip show` 那一格换了名字而 grep 照样中。** PEP 639 之后元数据里是
  `License-Expression: AGPL-3.0-or-later`,不再是 `License:`;那一步的
  `grep -E "^(Name|Version|License|Requires)"` 是前缀匹配,所以它匹配得到。
  (1.4.0 也是 PEP 639 出的,PyPI 上那一版报的是 `license_expression: Apache-2.0`。)
- **TestPyPI 那条 `--index-url` 的老隐患这次是空的。** 它让 pip 把 TestPyPI 排在前面,
  真正的危险是那边有一个版本号更高的同名包。实测:TestPyPI 上 `redis` 最高 2.10.6、
  `openai` 最高 0.11.5(两个都低于本包的下限 `>=5.0` / `>=1.30`),`httpx` 压根没有 ——
  三个依赖都只能从 `--extra-index-url` 那侧的 PyPI 解出来。**这一条会变,复核别省。**
- **3.7.0 这个版本号在两个索引上都还是空的**(PyPI 与 TestPyPI 上 `anima-world` 都停在
  1.4.0,各 7 个版本)。v3.0.0 那次 `testpypi` 是 `skipped` —— 它死在 `build` 第 8 步,
  **一个字节都没上传过**,所以没有烧掉任何版本号。

#### Fixed —— 装进包里的那份许可声明,漏在了三周前那次更正之外

- **`NOTICE` 写着「Releases up to and including 1.3.0 were published under the Apache
  License 2.0」—— 差一版,而差的那一版正是此刻用户 `pip install anima-world` 装到的
  那一版。** 3.3.0 那一轮(见下面 `## [3.3.0]` 的同名条目)查出这个 off-by-one 并跑遍了
  **四份**文档:README、CLAUDE.md、FOR-STUDIO、2.0.0 那节 CHANGELOG。**`NOTICE` 不在那四份里,
  而它是唯一一份真的随 wheel 和 sdist 发出去的** —— 另外四份都只住在仓库里。
  一份对外的许可声明少说一版,和 `README` 少说一版不是同一件事的两个副本:
  前者是**装在分发物里的**那句话。
- **`README` 与 `CLAUDE.md` 里「3.3.0 是照 AGPL 发的第一版」是一句已经过期的未来时。**
  3.3.0 到 3.6.0 一版都没上过索引,第一版是 3.7.0。README 尤其要紧:它就是 PyPI 的
  项目页正文,那句话会印在页面上。
- **新增两道闸(`tests/test_packaging.py`)**:① `NOTICE`(装进包的)与 `README`(印在
  PyPI 上的)必须说同一个 Apache 截止版本 —— 这一条**加上当场红**,红的正是上面那一格;
  ② `pyproject.toml` 的 `license` / `license-files` 与 `LICENSE` 正文必须对得上
  (SPDX 串写着 AGPL 而 `LICENSE` 里躺着别的许可,今天没有任何一处会报错)。
  ⚠️ **两道闸都只读文本、不建包**,所以它们不会像 sdist 那条一样被网络拖成假红。

#### Fixed —— 🔴 橱窗那 8 张图指着一台**从来没存在过**的主机(发版前拦下)

- **`demo.cyberworld` 里那 7 个 URL 全部指着 `cdn.animametaverse.com/oldport/…`,
  而那台主机从来没服务过一个字节**(实测 `curl` 连着两次 `000`;同一时刻
  `animametaverse.com` 自己答 200、它的 `/media/` 根答 404 —— **所以不是网断了,
  是那台机器不存在**。⚠️ 顺带一条:这台开发机的 DNS 会给**任何**名字一个 `198.18.x`
  的假 IP,`example.com` 也解析得到 —— **DNS 在这儿不是判据,`curl` 才是**)。
  当初写它是为了走通配图那条路(`e2ba877`,还真当场逮出 `anima-world map` 对
  配了图的世界整个 TypeError),**但 URL 本身是编的**。
- **为什么能挂到发版前**:裁决划得很清楚 —— 图的家归网站,**引擎不取字节**,
  只校验"是不是绝对 URI + 有没有超上限"。于是那 8 张图在每个 `pip install` 的新用户
  第一屏上**静静地全 404,而世界照跑、日志干净、退出码 0**。
  **而 `demo.cyberworld` 是本包唯一的 package data —— 发出去就是公开的、烧死的。**
- **改成 RFC 2606 给文档保留的 `example.com`**,路径里写着 `anima-demo-placeholder`。
  三条路里选它的理由是**另外两条各有一笔看不见的代价**:**摘掉图**会让
  `test_mapview.py::test_the_cli_prints_a_map` 里那句"橱窗真的带图"的前置断言失去对象
  ——那条正是上面那个 TypeError 的回归钉,摘图 = **悄悄少一道闸**;**写 `data:` URI**
  能让包自足(`character_card.py` 留了这条路),但它砸的是"`demo.cyberworld` 以纯文本
  进仓库、可 diff 可 review"那条不变量,而一个 1×1 的假像素和一张 404 一样假,只是更安静。
  **指真图床要先有字节**(`/media/` 是内容寻址,没有字节算不出 sha256)—— 那是另一件活。
- **上了一道闸**:`test_flagship_seed.py::test_橱窗里的外链只许指着一台明摆着不存在的主机`
  —— 扫整份橱窗文件里的每一串 `http(s)://`,主机名必须在一张**明示的**白名单里
  (今天只有 `example.com` 一个),报错点名是哪台主机。**它盯的是主机名不是可达性**:
  改成"去 GET 一下"就是一条联网判据,而这个仓库今年已经有两条判据栽在联网/挂钟上,
  红出来的话都在指控被测的东西。**验过它有牙**:把 `cdn.animametaverse.com` 塞回
  `git archive HEAD` 的临时树 → 当场 `1 failed`,原文
  `橱窗世界里有外链指着不在白名单上的主机:['cdn.animametaverse.com']`;换回来 `1 passed`。
- **`demo.cyberworld` 一动就把分发物那几条判据重敲了一遍**(它是分发物,改错了收不回来):
  `world inspect` 封皮 `engine_min` 仍是 **3.7.0**(两格图是 3.4.0 起的作者层键,本来就在,
  这一改没抬也没降)· `validate world` **没有发现问题** · `world check --json`
  `loadable: true / errors: []`,而 `external_media` 现在**照实报**
  `{"host": "example.com", "count": 7, "fields": ["map_image","portrait","scene_image"]}`
  —— 那一段本来就是为"让外链这笔代价看得见"做的,它现在指着一个诚实的答案 ·
  文件仍是**裸 JSONL 纯文本**(头两个字节 `{"`)、43 条记录逐行合法 JSON ·
  真开机 → `map` 渲染 rc=0 → `state()` / `map_data()` / `roster()` 三个读出口都带得出新 URL ·
  导出再看包里 `cdn` **零命中**。

## [3.6.0] —— 她点你的名时,你得自己答 (2026-08-20)

### 第九轮:把闸补宽到它本该有的射程,以及一条只会点头的判据

**零代码轮**(`anima_world/` 下一个字节都没动,判据:`git diff --stat cad89bf HEAD --
anima_world/` 一行不印)。上一轮把全称断言换成了敲得动的命令,这一轮发现**命令本身
也会撒谎**,而且是三种不同的撒法。

- 🔴 **一条结构上只会点头的判据。** REFERENCE 里写着「`loc` **只在落地那一刻写**,判据
  `git grep -nE 'write("loc"' -- anima_world/scheduler.py` 只答一行」—— 结论没错,那句
  机制话是假的,而 **pathspec 把这条判据锁死成了永远推不翻它**:它只问了一个文件,而那个
  文件里本来就只有一处。**一条只会点头的判据比没有判据更坏**,因为它看上去像已经验过了。
  去掉 pathspec 重问,全仓五处写点,**没有一处发生在上路那一刻**;五处逐条注上文件与函数,
  外加一条反向问法(`awk` 出 `_start_journey` 的范围再 `grep -c`,先 `wc -l` 自证范围非空)。
  结论改写成「**排班这条路上,`loc` 只在落地那一刻写**」—— 限定词是承重的。
- 🔴 **三个错名字,以及放它们过去的那道闸有三个结构性盲区。** REFERENCE 把
  `_ToolRuntime.agent_location` 写成裸函数、把 `_ToolRuntime.give_item` 写成 `World.give_item`
  (这个名字从 `01f0f1b` 起就错着)、FOR-STUDIO 写着一个 1.4.0 排期时起的名字 `World.set_rules`
  而它到今天都不存在。**上一轮那次红(`_colocation_gate` 安错类)不是孤例,是这道闸的射程
  边界**:`tests/test_reference_docs.py` 只读 REFERENCE、只认 `World.` 打头、只认带括号的形状。
  三处都补了:FOR-STUDIO 进入射程、任意类名按 AST 静态索引核对(不 import 引擎)、
  `` `World.x` `` 不带括号的提法也核。**拆掉新闸当场三红**,而这三条错话在旧闸下一条都不红 ——
  这个"拆了看它自答"的对照留成了回归语料(`test_these_gates_still_bite`),否则"补宽"这句话
  和"拆掉"在屏幕上长得一模一样。收宽/放宽都不许只靠一句自述。
- 🔴 **`--edit` 的绿灯是半个答案,而权威文档把这个洞说窄了。** `world check --edit` /
  `validate world --edit` 跳过的不是"名册/地图 + 引用完整性"(REFERENCE 原话),而是
  **整摞本体/规律闸**(`_authored_ontology_errors` → `_precheck_ontology` 只挂在 `else:` 上,
  两处调用点都是)。于是量名拼错、动词没声明、`me_X` 没声明、`spawn` 没写代价、`emit` 三字段
  写坏、能力里写了 `rand()` 这六类在 `--edit` 下**一条都不报,照答 `loadable: true` / 退出码 0**,
  而真装载时不看 `--edit`。四份实测探针逐个复现过。REFERENCE 两张表说准了(`--edit` 单列一列)、
  退出码表与"两条命令判断是同一份"那句各补了例外、FOR-STUDIO §3.17/§3.21/§5/§CLI 可达性
  四处认下这一格。**行为一个字没改** —— 这是文档轮,修法归引擎,见下面 `### Known`。
- ⚪ **D27 的欠账只落了一半,补齐另一半。** 「两扇门猜、一扇门问」这件事写在引擎代码与
  REFERENCE 里,而 FOR-STUDIO **零处提及** —— 照这份文档写脚本的那一侧看不见。§3.15 补了
  三扇门的对照表(`World._colocation_error` / `intent.Director._colocation_refusal` 猜,
  `_ToolRuntime._colocation_gate` 问)、每一扇的判据命令、以及"别按句子分支按 `reason` 枚举
  分支""别指望前两扇吐得出 `agent_in_transit`"两条用法。**库里有而对方看不见,等于没有。**
- ⚪ **`grep -c` 的 `0` 有两种来路,而两种在屏幕上逐字相同。** REFERENCE 里有一条 `awk|grep -c`
  的判据只写了 `grep` 那一半 —— 范围里真没有,和 `awk` 的模式写漏了导致范围压根是空的,
  都印 `0`。给它配了 `wc -l` 的伴生命令并注上实测值(106 / 80 / 35)。**同一条纪律在
  FOR-STUDIO 里早就守着,而权威那份没守。**
- ⚪ **`CLAUDE.md` 里那句「而敲出来是六」。** 上一轮已经写明"这儿有意不再填数",可同一句话里
  那个 `6` 还留着,读的人会把它当现状(今天同一条命令答 `7`)。**没删,标了**:注明是第八轮
  那一刻的快照、留着是记账不是判据,并顺手点出那条命令自己的坑(`^###` 全数是 9,带「轮」的
  才是 7)。删掉等于把当初的判断依据一起删了。

**这一轮的形状**:上一轮的教训是"配一条能自己答话的命令";这一轮的是**那条命令得能说"不"**。
pathspec 锁死的判据、只覆盖一个文件的闸、只在 `else` 分支里跑的校验 —— 三件事都是"看上去
验过了",而它们结构上给不出否定的答案。**判据的价值不在它答了什么,在它有没有答"不"的能力。**

### 第八轮:把全称断言换成敲得动的命令 —— 而这一轮自己的第一个提交是红的落地的

上一轮治的就是「顺手交代一下」时写的**全称断言**,而它在治它的那一次提交里又写了一句:
`docs/REFERENCE.md` 说新那句话「和 `presence` 那条命令、和答话那扇门的闸表**逐字同一份**」。
**三个验收员各自独立敲出来它是假的** —— 闸表那三个短语在新句里命中 0/3,FOR-STUDIO 是
第三种措辞,三份两两都不逐字。**这是同一形状的第四次**(`cafe`→「咖啡店」·「只剩这一处」·
「有测试守着」· 现在「逐字同一份」),**每一次都写在治上一次的那个提交里**。
这一轮**零代码、零契约**:只改说法与判据。

⚠️ **而这一轮自己也没能例外,连着三次:**一个当轮就作废的数(`6`→`7`)、一个红的提交
(把一扇门安在了错的类上)、一句"别这么写"的反例自己就那么写了。三次全被机械的东西
逮住 —— 一条命令、一道闸、又一条命令。**结论不是"下次更小心"**:前七轮每一轮都更小心过。
结论是**凡是"顺手交代一下"的句子,当场配一条能自己答话的命令,让它以后不必依赖谁细心。**

- 🔴 **「逐字同一份」降成可核的说法。** 改成「**同的是这三条成因,措辞各文档各写各的 ——
  别拿字符串去比**」,并给出取闸表那两格的命令(`git grep -nE '^\| .player_where_unknown' -- docs/`,
  答 2 行)。**没有把它换成一句更准的全称断言** —— 那正是前三次的做法。
- ⚪ **同一句给的导航判据,四行里两行是它自己。** 原判据 `git grep -n player_where_unknown -- docs/`
  在 HEAD 上答 4 行,其中 2 行是这句话本身。新判据用 `^\|` 锚只捞表格格子,**锚是承重的**:
  去掉它,写着命令的那一行自己就会被数进去。上一轮的 commit 标题写的是「判据自己命中自己:
  同一天里踩了三次,三条都改成不会自答的写法」—— **第四条当时没照做。**
- 🟡 **本文件头上「没有 `[Unreleased]`」的理由,今天是镜面反过来的同一句谎。** 原话是
  「挂在版本号底下的东西,当天就在跑着的镜像里」;而现存唯一那个 `anima-world:3.6.0`
  (`c8151d581dfd`)里 `_where_unknown_line` **一次都不出现**,repo 里是 6 次。当年防的是
  「停在 Unreleased 而其实已发」,今天是「挂在已发版号下而其实没发」。头上补了一段
  **「镜像滞后于本文件时怎么判」**:判据只能是**钻进镜像里**(`inspect.getsource` /
  `contract --json`),**不是版号** —— tag、LABEL、`__version__` 都是建镜像的人写上去的,
  三个一致而字节不同是常态。那条 `docker run --rm` 探针连同它当天的输出一起写在文件头。
- 🟡 **`CLAUDE.md` 里那个会烂的数字。** 「三轮验收修复见 CHANGELOG」而 3.6.0 底下已经有
  六轮 —— 改成一条**现数的命令**,不再写死一个数(顺手把「常用命令」那行的测试数按本轮
  实跑改对,并注明是哪一天跑的)。⚠️ **写下这个数的那一轮自己会把它加一**:本轮先填了
  `6`,而这一节一落进本文件,同一条命令当场答 `7` —— **一个"当下正确"的数,在写下它的那次
  提交里就已经不对了。** 所以那儿最后一个数都不填,只留命令。
  同一行里「常用命令」那个测试数也按本轮实跑改对了(`1774` → **`1788 passed / 19 skipped,
  365 秒`**,2026-08-20)。⚠️ **有意思的是秒数一直是对的,烂的只有件数** —— 两个数并排写在
  同一条注释里,一个准一个不准,而没有任何东西会因为其中一个过期而报错。新写法点明**这个
  数不是判据,它唯一的用处是别往下掉**:掉了就说明有测试悄悄不跑了,而屏幕上照样一片绿。
- ⚪ **本轮写下的每条判据都原样敲过,连"锚是承重的"这种话也是拆了锚量出来的**:去掉 `^\|`,
  同一条命令多捞出**写着它自己的那一行**(`docs/REFERENCE.md` 从 1 变 2);而 `intent.py`
  那条的四空格锚**拆掉之后答案不变**,于是"锚承重"这句只写在真承重的那一条上。
  **说锚承重不算数,把锚拆了看它自答才算。**(⚠️ 但"判据都敲过"**不等于绿** —— 见下一条:
  我敲的全是自己写的命令。)
- 🔴 **而"每条都敲过"照样红了:本轮第一个提交(`f8e5186`)是红的落地的。** 全量
  `1 failed, 1787 passed, 19 skipped in 377.17s`,红的是 `tests/test_reference_docs.py::
  test_every_documented_method_exists`,报「REFERENCE 提到了不存在的成员:
  `['_colocation_gate']`」。**而它说的是字面意思:`World` 上根本没有这个方法** ——
  邀请那条路上问在途的那扇门是 `_ToolRuntime._colocation_gate`(由同类的 `_invitee`
  调用),我给它安了个 `World.` 的姓。这句话我自己复核过好几遍,每一遍复核的都是
  "它问不问在途"(问的),**没有一遍复核过"它挂在哪个类上"** —— 那种细节没人会去查,
  而它恰好是一道闸一查就是字符串比对的东西。
  **教训不是"要跑测试",是"我敲的全是自己写的那几条命令,没敲仓库本来就有的那道闸"** ——
  自证的判据只覆盖我想得到的错法,而我想不到的那种正是会漏的那种;那道闸 0.26 秒跑完。
  派单说"零代码,全量跑一遍确认没掉即可",我照做了却把它放在提交之后:**改 REFERENCE
  的那一刻,守 REFERENCE 的那个测试就该当场跑一次。**
- 🔴 **修它的第一版还是红的:那句"别写成带括号的样子"自己带着括号写了一遍。** 反例
  写在反引号里 = 又一次命中同一条正则。**同一形状的第五、第六次**(第五次是那个
  `6`→`7` 的数,第六次是这个反例),而抓住后两次的都不是人,是一道闸和一次实敲 ——
  这正是"把话换成敲得动的东西"值钱的地方:**全称断言没人能当场证伪,一条正则能。**
- ⚪ **「没有两处是逐字的」这句也没留。** 取四处(REFERENCE 正文、`presence` 印的那句、
  两张闸表)去掉 markdown 两两比过:**六对全不相等,但差得很不一样** —— 两张闸表只差
  一个标点,而 `presence` 那句和 REFERENCE 正文共享一百多字。技术上为真、读起来像抠字眼,
  且**明天有人统一掉一个标点它就翻**。所以写成量出来的形状,并点明**"它们不一样"同样
  不能当判据**:该数的是列了几条成因。
- ⚪ **一条有意留着的欠账,补进宿主读的那份权威。** `_colocation_error` **从不问在途**、
  猜错时印的是她的**出发地**(「你在后院,苏晚夏在咖啡店」)—— 这笔账此前只写在 `api.py`
  的 docstring、两条测试的 docstring 和本文件里,`docs/` 侧**零命中**,只读 REFERENCE 的
  宿主查不到。REFERENCE §2.9.9 那张三格拒绝表下面补了这一段,连同两条 `awk` 判据,并写明
  同一条路上的 `intent.Director._colocation_refusal` **也只是猜**:它那一行里唯一带
  `transit` 的字样是**枚举名本身**。**行为一个字没改**(修法已进看板,定版后另开)。
  「为什么印的是出发地」这一句原本是从 `api.py` 的 docstring 里抄过来的 —— 这个仓库的
  docstring 是**病历不是现状**,抄之前回去把机制核了一遍:`agent_location()` 读黑板上的
  `loc`,而 `loc` **只在落地那一刻写**(`git grep -nE 'write\("loc"' -- anima_world/scheduler.py`
  只答一行),上路只往 `_transit` 记一笔。机制连同这条判据一起写进了 REFERENCE,
  **不再让读者靠一句转述相信它**。
- ⚪ **`docs/FOR-STUDIO.md` 那张三支表的「退出码」列是贡献值,不是进程退出码。** 同一批
  输入在一个还没配 LLM 的世界上**一律 exit 1**(验收员实测)。表下补了一段,并把**机制**
  写出来而不只是复述现象:`run_doctor` 里 `problems` 从 0 起累加、非零退 1,没配 LLM 的
  世界在 `llm_status(...).degraded` 那一格就先加了 1。给了条数得出来的判据(实敲 `5`:
  3 处 `+= 1` 加 2 处把函数返回的件数整个加进来)。
  ⚠️ **这条判据的第一版自己是错的**:收尾模式写成 `/^def /`,它命中 `def run_doctor`
  那一行自己、范围当场闭合,答 `0` —— 而 `0` 长得跟"一处都没有"一模一样,**正好会把读者
  引向和这一段相反的结论**。是敲出来才发现的,反例连同它一起写进了文档。
  同节上方 `validate` 那条「判据是 `loadable` 不是 `$?`」改成明写"两条命令的退出码都是
  汇总量" —— 原先写的是它「逐字同样成立」,而那条讲的是另一个命令的另一个字段。

### 第七轮:上一轮修好的那句话,自己又夸了一次口(定版前最后一轮)

上一轮做了件对的事:把两扇门那句「世界这会儿不知道你在哪」抽成 `_where_unknown_line()`,
让"逐字同一句"由**代码**保证而不再由人手抄维持。然后它在旁边写下「只剩这一处停在两种上」
——**当场是假的**。这一单里同一个形状出现了三次,三次都是"顺手交代一下"时写的夸口。
**改法不是换一个更准的全称断言,是换成一条敲得动的命令。** 除一处文案外全是文本与注释。

- 🔴 **「只剩这一处」当场被证伪,而漏的那一处在宿主读的那份权威上。**
  `docs/REFERENCE.md` 的 `player options` 那段还写着「没有位置只剩两种可能」,
  而 `presence` / `api.py` / 闸表都已经是三种(漏的是 `player_leave`)。两边一起改:
  REFERENCE 补齐三种;CLI 那句夸口换成
  `git grep -nE '(只剩|只有)两种可能' -- anima_world/ docs/` —— **答 0 行才算统一**。
  括号是承重的:写成不带括号的那两个词,注释自己会命中自己。
- 🔴 **认账:两扇门只统一了四支里的一支。** `_colocation_error` **从不问在途**,
  只按 `here == where` 猜她在不在赶路;猜错时它说的是**她的出发地**
  (她在「咖啡店 → 工作室」的路上,玩家读到「苏晚夏在咖啡店」),而隔壁邀请门在
  同一情形下说的是「她这会儿在路上,还没落脚」。两扇门在这两种情形上说的话**恰好
  对调了**。⚠️ **本轮把 `cafe` 换成「咖啡店」之后,它更像真的了 —— 把谎说得更流利
  是净负作用。** 判断逻辑**有意没动**(定版前不换没人复验过的分支,已进看板);
  动的是「关于它的说法」:`_where_unknown_line()` 的 docstring 从"两扇门统一了"改成
  逐支对照表 + 一条敲得动的判据,并写明这不是上一轮引入的(`fdd2408` 的分支条件
  逐位相同)。测试 `test_两扇门上同一件事必须是同一句话` **管的比名字小**,
  docstring 里写死它覆盖哪一支、没覆盖哪两支、以及**为什么不许把它放开到那两支**
  ——「一句"有测试守着"比没有测试更坏,读的人会照着它省下自己那一次检查。」
- 🟡 **玩家被回敬了一个函数名。** 「「reach_out」要当面才办得到」——上一轮把 `{verb!r}`
  换成「」是对的一半,框里那几个字母仍是函数名,而读它的是刚刚才动手做过这件事的人。
  四句一律改成「**这件事**要当面才办得到」:全仓声明 `requires_colocation` 的能力
  **有且只有一个**,这一格里没有歧义可消,加了第二个也照样对。**动词名一个字没丢**
  (还在 `result["tool"]` 与那行 `logger.info` 里),去掉的只是玩家读到的那份;
  也不套「」——框的只有数据里来的那一截。宿主/作者面那 6 处 `{verb!r}` **一处没动**。
- 🟡 **`doctor` 那行黄 `!` 其实有三支,而说明书给的分法分不开。** FOR-STUDIO 写着
  「看有没有跟着那两行说明」,可"被带走"那一支后面也跟着两行。改成精确判据:
  **看有没有「这几个数加不平」这一句**(有 = 引擎的账错了请贴回来;没有它而有
  「最近一件:」= 排班在抢她的手,退 1;两样都没有 = 世界自己有一件收不了尾,退 0)。
  权威侧的 docstring 同步记一份。**数、措辞、退出码一个字没动。**
- 🟡 **自查逮到的第五句假话**:`_colocation_error` 的第一行写着「`act()` 和 `intend()`
  共用这一句」,而 `git grep -nE 'self[.]_colocation_error[(]' -- anima_world/`
  只答得出**一处调用**。`intend()` 走的是另一条路、另一句话(「排不进打算」),
  而且是**抛 `ValueError` 不是回执** —— 照原话去 `intend()` 里找那句回执的人会找不到。
- 🔴 **「有测试守着」这句话本身也是夸口:四句话里有两句没人守。** 交活前逐句反打
  补丁量了一遍(一次只改坏一句,跑遍十个提到「当面/colocation」的测试文件,基线
  287 项):「世界不知道你在哪」塌了红 2 条、「你在 X、她在 Y」红 1 条,而
  **「没说是替哪个玩家」和「她这会儿在路上」改坏之后 287 全绿**。而这一轮改的正是
  这四句的措辞 —— 一半的改动没有任何东西拦得住它悄悄改回去。补了一条
  `test_四句话里从前没人守的那两句` 把那两句钉上(钉的是"玩家读到的句子里不出现
  动词名 + 动词名照旧在 `result["tool"]` 里")。**第四句有意不钉**:它在她在途时
  报的是出发地,钉住一句假话,那句假话就再也改不动了。
- 🟡 **本轮自己造出来的一条告警,当天修掉。** 上面那几条判据里的 `\.` `\(` `\}`
  写在**非 raw 的 docstring** 里,于是整个包一被导入就报
  `SyntaxWarning: invalid escape sequence`(全量跑那行末尾的「1 warning」就是它,
  基线 `26a204c` 是 0)。判据改用 `[.]` `[(]` `[}]` 这种 POSIX 括号表达式:
  照样不自答,而且一个反斜杠都不用。**一条为了防假话而写的判据,自己在包里留了条
  告警** —— 注意事项写进那两处 docstring。
- **顺手两条**:`presence` 那段说明里 `带过期时间 ——跨进程` 补回破折号后的空格;
  `_colocation_error` 里 `where_name` 在用不着它的两支上白算一次,挪到真正用它的
  那一支前面。**核过没改的一条**:`presence` 的「角色在哪」印裸地点 id,和
  `roster`/`map` 的人话不一致 —— 运维面用它的人下一步要拿这个 id 去查 `--json` 和
  日志,翻成人话反而要他翻回来。**结论记在原地**,免得下一个人再核一遍。

### 第六轮:让写下来的那句话是真的

上一轮治的是"说出来的那句话是假的",这一轮治的是**它的两种复发形态**:一句被手抄维持的
"逐字同一句",和一条只在一个地方被执行的纪律。两种都会在**写下纪律的那次提交里**当场破功。

- 🔴 **老病复发,而且就长在上一轮编辑过的那个函数里。** 3.0.0 的
  `### Fixed —— 她读到的地名一半是人话、一半是 id` 修的是「你在建筑工作室，正在去cafe
  的路上」这种半人话半 id 的句子。上一轮改 `_colocation_error()` 时,**新写进去的两句
  拿 `location_id` 直接拼**,于是玩家读到「你在 yard,苏晚夏在 cafe」——
  **同一个病、同一类句子、同一个版本里**。更贵的是那次还在旁边写了一句注释,说这扇门和
  `_invite_absence` 那扇「逐字同一句」;而那份"同一句"是**手抄**维持的,抄的时候已经差了
  一个空格。修法不是再抄一遍准的:把那句抽成 `_where_unknown_line()`,**两扇门调同一个
  函数**;地名一律过 `Scheduler.place_name()`;测试从"子串包含"改成**整句相等**,并加一条
  跨门等值的用例(`test_两扇门上同一件事必须是同一句话`)。长期判据写进 docstring:
  **凡是拼给玩家/角色看的句子里出现地点变量,一律问一句「过 `place_name()` 了吗」。**
- 🔴 **上一轮新加的那道闸,自己就漏掉了两处 —— 其中一处印在玩家的按钮上。**
  `test_屏幕上不许出现裸markdown星号` 的 docstring 写着「一条只在一个地方被执行的纪律,
  等于没有这条纪律」,然后**把文件名写死成一个**。放开到整个 `anima_world/` 之后当场红:
  `tools/body.py` 的 `walk` 与 `eat` 各有一处裸 `**`,而 `walk` 在 PLAYER 面上 ——
  那四个星号是**原样印在玩家按钮说明里**的。另外三处 AST 扫不到(经变量 / `logger` /
  `warnings` 列表上屏),手工找出来一并改成「」;**扫不到的那一类写进 docstring 的盲区
  声明**,不假装覆盖了。
- 🟡 **`presence` 那两处仍在说"两种可能",而世界有三种。** 玩家没位置的原因是
  `player_leave` / 在场记录过了 15 分钟没续上 / 宿主确实没落过 `player_move` ——
  和 `api.py`、REFERENCE 已经改好的那一份对齐;少说一种,宿主就会照着去查一个不存在的
  故障。CLI 的那张表与下面那段说明各改一处。
- 🟡 **一行绿勾说着「N 件收不了尾」。** 上一轮把"加不平就说出来"修在了 `dropped`
  那一支,而 `gone > 0` 且加得平的时候仍旧挂绿勾 —— 同一条纪律的**第二个落点**没跟上。
  **只换脸色**:黄 `!`;**数与退出码一个字不动**(§3.13 A4 冻着)。
- 🟡 **`docs/REFERENCE.md` 抄的 `blocked_text` 三句全是错的。** 改成「照
  `_BLOCKED_WORDS` 印」并给出取法 —— **有意不抄原文**:抄一遍就是再造一份会烂的副本,
  而那正是本轮的题目。同段 `unknown_player_location` 的括号注同上一条的病,一并改成三种。
- 🟡 **一句永远不会失败的断言。** `test_闸和面对面必须逐位同构` 里
  `assert gate == "" or gate in COLOCATION_GATES` 摆在 `assert gate == expected` 的下一行,
  上一句成立时下一句必然成立。**认账 + 补实,不悄悄删掉**:改成把这一趟见过的闸攒进
  `seen`,收尾对 `COLOCATION_GATES` 全集 —— 往族里加第五条闸而不写用例,这里当场红。
- **核过没改的一条**:`GATE_LABELS` 里「这个世界不让角色主动开口相约」的"这个世界"
  三个字。唯一产出它的是 `Scheduler._invite_config()` 读的**世界级**配置键
  `social.joint.npc_may_invite_player`,仓库里没有按玩家覆盖的路径 —— 措辞照实。
  **结论记进代码**,免得下一个人再核一遍。

### 第五轮:上一轮自己也犯了它要治的病

第四轮的题目是「说出来的那句话得是真的」,而它一边写这句话一边犯了三次同一个错 ——
**这不是巧合,是这种病的形状**:每一处假话都是在"顺手交代一下"的时候写下的,而正是
那种时候没有人会去核。三条都被第四轮的验收员当场逮住。

- 🔴 **`doctor` 那笔账仍然加不平,而它是绿的。** 上一轮把"做完了几件"从减法改成四样
  各数各的,可 `_destroy_entity` 给**每条**在做的记录都发了一条 `entity_disengage`
  —— 包括参与者;而数"起了几件"那一头**有意不数参与者**。分子分母不是同一个单位,
  于是一个"一件事起了头、东西被抹掉"的世界算出 `1-0-0-2 = -1`,被 `max(0,…)` 压成
  0,屏幕上是一行**绿勾**说着「起了 1 件,做完 0 件,2 件收不了尾」——
  **一行绿勾说出算术上不可能的话,比一行红字更贵:看的人会信它。** 两处一起改:
  修在源头(一起做的事在日志上只记一次,由发起人那条记;人照样放开)、以及**加不平
  就说出来**(黄色 `!` + 两行说明,并点明"这是引擎自己的账错了,不是这个世界的毛病")。
  **退出码一个字没动** —— 加不平只改脸色。
- 🔴 **上一轮自己新写了两句假话。** FOR-STUDIO 的闸那一格写「四个取值,见下」,而
  `GATE_LABELS` 有十六个(四个只是**当面那一族**);`_colocation_gate` 的 docstring
  写着这条同构「钉着」,而当时 `tests/` 里一处都没提过这个函数名 ——
  **一句"有测试守着"比没有测试更坏,读的人会照着它省下自己那一次检查。**
  前者改文档,后者补上真测试(按**状态 ×(她, 他)**铺开对,不是挑一个样本点)。
- 🟡 **`answer_invitation` 宣称"五选一"而门只吐得出四种**:`INVITE_OUTCOMES` 里的
  `cancelled` 只有她自己收回才产得出,这扇门一次都不会返回它。下游照着写的那个
  `match` 会有一支永远进不去,而它看上去像是在防守。
- 🟡 **权威标了日期,镜像没标。** 上一轮给会过期的断言补日期时只改了代码那一份,
  REFERENCE 里对着同一句话的那一格还光着 —— 而**镜像不标日期,那句话就会以"现状"
  的身份被一直引用下去**,这正是那条不变量要治的病。这一轮扫平了五处同型的。
- 🟡 **`[Unreleased]` 在对宿主撒谎**:上面那几轮全都已经进了正在跑的镜像,而这份
  文档把它们放在一个写着"还没发"的标题下面。并入 `[3.6.0]`,并在版本约定那一段
  就地写明"这里没有 `[Unreleased]` 这一节"以及为什么。
- **顺手项八条**(grep 示范少一个空格、「这位玩家」被套成「「这位玩家」」、
  `Consent.explain()` 那句"单独看也成立"、doctor 那段手写的示范输出、`--help` 屏幕上
  的裸 markdown 星号、`engine_version 是唯一权威`缺一个指向例外的指针、答话那扇门整族
  四个闸只有一条测试、`_colocation_error` 把原因写死成「宿主没调过 `player_move`」)。
  最后一条上一轮记成了欠账,理由是"改它要动一条测试断言" —— 而**那条断言本身就是
  病**:它把一句 3.2.0 的实况焊成了这扇门的契约,于是那句假话再也改不动了。现在两扇
  门(`act()` 与 `answer_invitation()`)对同一件事说同一句;`reason` 的枚举名没动。

### 第四轮:说出来的那句话得是真的(三个验收员从三条路撞上同一句)

这一轮**一个新能力都没有**,修的全是同一种错:**代码干的事是对的,而它说出来的那句话
是假的。** 这种错不报警、不掉数据、测试全绿,只是让照着它做决定的人做错决定 ——
这一轮就有一个验收员照着一句过期的注释,把一个假结论写进了验收报告。

- **「不在她跟前」拆成四句**(`together.GATE_LABELS` + 新增 `COLOCATION_GATES`)。
  `face_to_face()` 把四种原因折成同一个 `False`,而回执一律写「不在她跟前 —— 一起做事
  得当面」:**两个人真的在两处**、**世界不知道他在哪**、**她在赶路**、**世界不知道她
  在哪**,四件事一句话。差别是**他能不能改**:后三种他做什么都改不掉,而那句话听起来
  像他站错了地方。现在四个闸各有名字(`player_not_here` / `player_where_unknown` /
  `inviter_in_transit` / `inviter_where_unknown`),`answer_invitation()` 按**整族**分支
  (`in together.COLOCATION_GATES`)—— 写死单个闸名的话,拆闸本身就会悄悄关掉"到底是谁
  不在场"那一支,而少掉的三种恰好是最需要点名的。
- **回执里漏出的裸 pid 堵上了**。`player_name()` 找不到行时回落成 id(给调用方的兜底),
  而拼人话那一步照抄了它,于是玩家读到的是「`「p1」`不在她跟前」。这一支恰好只在世界
  没有这个玩家的行时走到 —— 也就是他刚 `player_leave`、或宿主从没登记过他,**正是最该
  说实话的那一次**。现在印「这位玩家」。
- **`gone` 只剩一种形状**。`accept=False` 和 `settle_invitation` 撞车那条路上另写了一句
  光秃秃的「这份邀请已经不在了」且不带 `settled`,而正常那条路带 —— 同一件事在两条路上
  长得不一样,下游没办法知道自己碰上的是哪一条。合并成 `_invite_gone()`。
- 🔴 **一句过期的注释,骗了一个验收员整整一轮**。`intent._colocation_refusal` 里写着
  「`player_move` 今天线上根本没人调」—— 那是 **3.2.0(2026-08 上旬)** 的实况,站点
  **2026-08-13 前后**就接上了(落脚 / 重连 / 世界重启复位三处)。一个验收员照它推出
  「这扇门在线上一次都不会开」并写进了报告,而真相是**门开着,只对最近 15 分钟内进过
  世界的人开**(`_PLAYER_TTL_SECONDS`)。修法不是删那句话(删掉等于把当初这条闸为什么
  默认关着的依据一起删了),是**给它标上日期**并写下今天的形状。同一轮扫掉了
  `api.py` / `intent.py` / `__main__.py` 里其余同类断言 —— 其中 `run_presence` 的
  docstring 里躺着**逐字相同的那一句**,而那道命令恰恰是量这件事的尺子:实况过期了
  不等于尺子没用了,反而正说明"谁有位置"只能当场量。**规矩已经进 `CLAUDE.md` 的不变量**:
  凡是写「今天 / 目前 / 线上 / 一次都没」的句子,当场补上量它的日期与版本;判据是
  **这句话半年后还成立吗**。
  ⚠️ **头一遍扫漏了,而漏的恰好是人最先读到的那几处**:同一句话还逐字躺在
  `config_store.py`(那个开关自己的注释)、`tools/base.py`(`requires_colocation` 的
  声明处)、`REFERENCE.md §2.9.7`、以及 `test_colocation.py` 的两条 docstring 里 ——
  一个想弄明白"这道闸为什么默认关"的人,四个最可能落脚的地方全是那句过期的话,而
  已经改好的三处他多半不会去看。这一遍一并标了日期;`mysql_state.py` 的
  「目前没有这种代码」也补上了复核日期。
  **当时欠了一笔,第五轮补掉了**:`_colocation_error`(`presence.enforce_colocation`
  那扇门,默认关)的回执还把原因写死成「宿主没调过 `player_move`」,而**下线**与
  **在场行过期**走的是同一支 —— 那两种情形里宿主刚刚才调过。当时判断"它的措辞被
  `test_colocation.py:168` 钉着,改它要动断言",而那条断言正是病本身。见 第五轮。
- **`doctor` 的"做完了几件"从前是减出来的**(`起了 - 被带走`),而那个减法把三样东西
  一起算成了"做完":东西没了收不了尾的(`gone`,代价一样不退)、条件没过的、以及
  **此刻还在做的**。一个刚起了三件长活的世界会被报成「做完了 3 件」—— 一句听起来最像
  好消息、而恰好在自己要度量的那件事上说反了的话。现在四样各数各的(做完按收尾那条
  `entity_interaction` 的 `duration` 记号,起头**不数参与者** —— 一起做的一件事一人一条
  `entity_engage` 而收尾只有一条)。**退出码一个字没动**(有被带走的就退 1)。
- **`engagement_kept` 那盏灯的 `status` 从 `null` 回到 `"ok"`**。粘住(`sticky=True`)
  那个早退排在设 `status` **之前**,于是件件收得了尾的世界这一格是 `null` —— 而 `null`
  和"这条链一次都没跑过"在报文上分不出来。**粘的意思是"红了不许自己变绿",不是"绿这
  一档不存在"。** 这是一个**既有字段的取值悄悄变了**(3.6.0 之前是 `"ok"`):加一格
  下游看不见,改一格下游的判断当场错,而两者都不报错。
- **文档里四句照着敲会失败的话**:`world_file.py` 的 `grep '"type":"entity_spawn"'`
  少一个冒号后的空格(找不到时退 1、屏幕空白,和"这个世界确实没生过东西"长得一模一样);
  `doctor` 的 `--help` 还写着 world.db 时代的「世界文件」,而它真查的最贵那一样(长过程
  有没有做完)一个字都没提 —— 人不会去跑一个自己不知道能回答这个问题的命令;
  FOR-STUDIO 的「(有就退 1)」没说那是**总账**(一件长过程没丢的世界照样会因为 Redis
  没开 AOF 而退 1);`CLAUDE.md` 说「`.cyberworld` 是 gzip JSONL」而包里自带的
  `demo.cyberworld` 是**裸文本**(有意的,为了可 review),对它 `zcat` 直接退 1 ——
  一句在唯一一个人人手上都有的文件上就会失败的示范命令。
- **REFERENCE 三处不实**:「没出过事的世界这一格根本不出现」(它出现,只是当时是 `null`);
  `doctor` 的示例输出里有一个**编出来的**「(第 88 tick)」,还漏了一行;`doctor` 一节
  没说退出码是总账。另加两笔此前只在代码里的事实:`refusal` 那句人话**带着角色此刻的
  地名**(不说清她在哪,「你们不在一处」就是一句没有下一步的话 —— 但宿主若有"不许把她
  在哪透给玩家"的判断,要在自己那一层挡),以及这四格的判据是**键在不在**,不是
  `engine_version`(`contract --json` 里搜不到它们的名字)。
- **`[3.6.0]` 补上了它最贵的一句话**:写了 `importance` 的世界文件**在 3.5.0 上开不了机**
  (`✗ 不认识的字段`,退 2),`engine_min` 必须写 3.6.0。此前它只在 FOR-STUDIO 里,
  而读 CHANGELOG 的正是不看那份文档的那批人。

### 第三轮:她走开的时候,那句话不许说成他不在

两个验收员从两个方向撞上了同一处伤,而它是**一句报文里的假话**。`cancelled` 只堵住了
最窄的一条路:`walk_away` 是聊天里的工具,只有她**在对话里**走开才收回邀请。她按作息表
溜达去后院时,那份邀请还亮着、还在倒计时,玩家按下「好」收到的是
`expired` + `「访客」不在她跟前` —— **她走的,话却说成他不在**。而且不论谁走开,那一支
返回的报文**逐字节相同**:宿主无从分辨,一个在手机上玩的人只知道自己被怪了一句。

- **`answer_invitation` 多三格,答"到底是谁不在场"**:`absent` ∈ `agent` / `player` /
  `both` / `unknown`(机器读的那一格 —— 此前宿主要判断怪谁只能去 `in` 一个中文子串)、
  `gate`(挡下的那道闸的名字,如 `player_not_here`)、`settled`(只在 `"gone"` 那一支,
  是那次的**结局本身**)。`refusal` 改成**两头都说**:「苏晚夏已经离开咖啡店了 ——
  她这会儿在后院,而你还在咖啡店。是她走开了,不是你没到场」。`gone` 那一支同理:
  「她不等了 —— 这句话是她自己收回去的,不是你没答」,此前给的是一句
  「要么答过了,要么已经过期」—— 一句**恰好把真因排除在外**的话。
  ⚠️ **`reason` 一个字没动**(它是闸自己的分类,下游按现有取值交过活了),`absent` 里
  单列 `unknown` 是因为**世界不知道他在哪**(宿主从没调过 `player_move`)和"他站错了
  地方"是两回事,混成一句就是拿一个宿主的漏接去怪玩家。
- **她在哪儿开的口跟着那份邀请一起挂着**:投影行上多一格 `loc`,从**事件顶层**抄进来。
  ⚠️ **事件的 payload 一个字没改** —— 线格式没动,镜像端不必跟(有测试钉着
  `"loc" not in payload`)。没有它就说不出「她已经离开咖啡店了」,只说得出「她在后院」,
  而那句话对一个也在后院的玩家是假的。
- **`invitations_page()` 每行多一格 `outcome`**:这份邀请**是怎么结束的**(还等着的行是
  空串 —— 「还没有结局」和「结局是等着」是两件事)。它答的是**离线回来那一眼**:结局只
  落在 `invitation_settled` 那一条事件上,而宿主的事件窗是截最后 N 条、**没有游标**的,
  离线久一点那条就滑出去了,于是那一行只剩"没答过"的样子。⚠️ **有界**
  (`SETTLED_INVITATIONS_KEPT = 200`),更老的回落成空串 —— 说不出来就别猜,而不是让这张
  表随世界一起长。⚠️ **要下游跟一笔**:运维台读侧是逐键挑的白名单,不加这个键玩家端
  看不见它。
- **`engagement_kept` 那盏灯不再抖**。实测:五次事故发**五条** `subsystem_health`,而每次
  有人顺利做完一件事就把灯拨回绿、`reason` 抹成 `""` —— 于是线上读出来的样子是
  `degraded: 3` 配 `reason: ""` 配 `status: "ok"`:数字说出过事,却说不出出的是什么事。
  它现在是**粘的**(`note_subsystem(..., sticky=True)`):别的灯答"这个子系统现在还转不转
  得动",而这一格答的是"**这次开机里有没有人白花过时间**",是一笔账。顺带代价也降了 ——
  出五次事只发**一个**事件(灯已经红着就只更新"最近是哪一件"),不淹日志。此前**零测试**。
- **`anima-world doctor` 报得出这条链**:「要花时间的长过程:起了 12 件,1 件半路被带走
  (代价不退)」+ 最近被带走的是谁的哪件事 + 做完了几件,有被带走的退出码 1。
  ⚠️ 它读**事件日志**不是那盏灯 —— 灯只记本次开机,而一个昨天出过事、今早重启过的世界,
  灯是刚点亮的绿。此前这条账**库里有而 CLI 上没有,对外面等于不存在**。
- **在途那一句自主上下文也读得到**。同一个 NPC,`world_context` 说「正在去后院的路上」而
  `_autonomy_context` 说「闲着」—— 两处各拼了一遍 `_transit` 分支,其中一处漏了。合并成
  `World._self_activity_label()` 一处。⚠️ 说明白:这一条**今天还是潜伏的**,
  `_maybe_run_autonomy` 会把在途的人排除在名单外,所以那条岔路现在跑不到 —— 修的是"哪天
  这道过滤松一格就当场出错"的地雷,不是一处正在冒烟的伤。

**裁决记在这儿**(REFERENCE §2.9.6.7 末尾):**「她按作息表溜达开」不算「她不等你了」,
`INVITE_OUTCOMES` 一个取值都没加。** 「她还等不等你」是一个**决定**(她自己动手收回),
「这一刻办不办得成」是一个**事实**(位置、闸,随时会变也随时会变回来)。把溜达判成
`cancelled` 就是拿事实冒充决定,而下游两仓已经按现有取值集合交过活:在他们那儿这份邀请
会当场变成一行「她收回了」,再也回不来。留着一个**产品向问题**没代拍:他在她短暂离开时
按了「好」,今天是**当场烧掉**这份邀请(落 `expired`),要不要改成"这一下不算,邀请留着
等她回来",引擎两种都做得出来。

**文档改的三笔**:`agent_invites` 那行的 `consented` 说明**比实现多说了话** ——
写着「她自己、以及名单里当场答应了的角色」,而发起的人**从不进名单**(`_interact_with`
见到就当场报错);「不该再夹一次模型调用」也说过了头,**跳过的只有"问"这一步,闸照查**。
另外两笔是上面三格与 `outcome` 的登记。

**顺手敲了一遍才发现的**:README 与 CLAUDE.md 里那句"`.cyberworld` 是文本,排障不需要任何
工具"配的示例命令 `zcat x.cyberworld | grep '"type":"entity_spawn"'` **一条都匹配不到** ——
记录用 `json.dumps` 的默认分隔符写出去,冒号后有一个空格。而 grep 找不到时只是安静地退 1,
和"这个世界确实没生出过东西"在屏幕上一模一样。两处都改对了(实测:同一份导出上,
带空格的那条数出 3 条 `location_join`,不带空格的数出 0)。⚠️ CHANGELOG 的 2.0.0 那一节里
同一句话留着不改 —— 那是**当时说过的话**,而它当时就是错的。

### 做一件事要算数:`importance` / 玩家进旁白 / 她点你的名时你得自己答

上一节把在场名单还给了她,这一节管**做完之后**。此前一次交互做完就结束了:量动了,
事件躺在日志里,而屋里没有一个人记得它发生过 —— 一个人可以当着满屋子人的面把那棵树
砍了,下一句话谁都不会提。三格,同一根轴(REFERENCE §2.9.6.7):

- **能力上的 `importance`**(`kinds.<种类>.affordances.<动词>.importance`,0~1,可选)。
  写了它,做完时**此刻同处一地的每个角色各记一条**(记忆 kind 复用 `witness`,来路
  `source_type: "entity_interaction"` / `affordance` / `actor` 写在**事件**上)。
  三条纪律逐字照抄规律那一半的见证记忆:**声明本身就是开关**(不写 = 这一层整个不
  存在,**没有默认值** —— 给个缺省等于替作者宣布"任何一次交互都值得记一辈子")、
  **见证者按位置算**、**走 `memory_seed` 不新增记忆种类**。做的人是玩家还是角色,
  这条路上**一处分支都没有**(见证者从引擎模拟得动的名册里来,玩家自然不在其中);
  做这件事的角色**自己也是见证者**,她那条写「我…」,别人那条写「江晚…」。
- **玩家做的那一下也进旁白**(`narrative.player.enabled`,**默认 0**)。此前只有角色
  的动作有旁白 —— 一个玩家和一个角色并排擦同一扇窗,量一样地动,而时间线上只有她
  那一句。三道闸:只管玩家那一半、作者声明了 `importance` 才有、开关默认关(旁白是
  一次 LLM 调用,按玩家的每一次动作触发)。永不在 tick 线程上调。
- **邀请门,修的是一句假话**。一起做事那扇门上写着"一次调用只替一个玩家说话",而
  代码里有一条岔路绕开了它:**角色**发起的 `interact` 把玩家写进 `with` 时,引擎
  **替他答应了**。于是"取消对方意志的能力比没有这个能力坏得多"这句话,对角色成立,
  对玩家不成立 —— 而这一层保护的本来就是玩家。现在她开口只落一条 `agent_invites`
  然后**等**:`World.invitations()` / `invitations_page()` / `answer_invitation()`。
  按"好"要**在这一刻重查一遍闸**再去做;按"不"落 `declined` 并且**只有这一支**动
  关系(`decline_delta`,不动 respect)、进她的记忆(复用 `relation_shift`);
  **没答挂到 ttl 的那一支是「错过」不是拒绝,一个字都不写**(不落种子、不发
  `sentiment_delta`)—— 一个在手机上玩的人放下手机不等于他说了不,而记忆一旦开始记
  就抹不掉,所以那一支有一条专门钉死的测试。⚠️ **玩家自己按按钮那条路一个字没变**
  (发起调用的人就是他,不必问他肯不肯);`player_options` / `player_tools` 的线格式
  也一个字没动 —— 邀请不是"他此时此地点得动什么"。

🔴 **这一版的世界文件在 3.5.0 上开不了机 —— 上面那条"minor 两个方向都挂得上"对它
不成立。** 一份写了 `importance` 的 `.cyberworld` 拿 3.5.0 去装,得到的是
`✗ 不认识的字段 ['importance']`,退出码 2,**整个世界拒绝加载**(不是静默忽略 ——
"不认识的字段当场报错"是作者层的既有纪律,没松,而正是它让这次的加法变成单向的)。
所以**出包时 `engine_min` 必须写 `3.6.0`**;包里的 `demo.cyberworld` 已经写了。
这句话此前只写在 `docs/FOR-STUDIO.md` §3.25 里 —— 而**读 CHANGELOG 的那批人正是不看
那份文档的那批人**(FOR-STUDIO 是给创作台的),他们照着开头那段"minor = 两个方向都
挂得上"去回滚一个引擎,拿到的是一个开不了机的世界。**这是这一版最贵的一句话,而它
本来不在这儿。**

四个新配置键:`social.joint.npc_may_invite_player`(**默认开**)、
`social.joint.invite_ttl_ticks`(12,**按世界时钟判不按墙钟**)、
`social.joint.invites_per_player_per_day`(2,**用完不是错,是她今天不再开口** ——
这是「像个人」和「像推送」的分界)、`narrative.player.enabled`(默认关)。

两个新事件类型 `agent_invites` / `invitation_settled`(四种结局只落这一种事件),
投影里多一张 `invitations`(重放折得出同一份"还等着的清单")。
⚠️ **没有新增 Redis 键、`volatile_keys` 一格没动** —— 邀请是事件,不是易失态。

`contract --json` 的 `seed` 段多两格:`affordance_keys`(能力字段全集)与
`affordance_importance`(范围 / 读出口 / 没有默认值 / 一句人话);`world check` 与
`validate world` 认这个字段。⚠️ 那一格**读的就是校验器减的那个元组**
(`ontology.AFFORDANCE_KEYS`)—— 抄成两份的话,新加一格时总会有一次只改了校验器,
引擎收得下而创作台问出来的答案里没有它,两边都不报错。

⚠️ **对已发布的世界不生效,要等作者重新出包**:晚潮那份世界文件里一个 `importance`
都没写,所以见证记忆与玩家旁白对它整层缺席(这正是"声明本身就是开关"的代价,
也正是它的好处 —— 老世界的行为逐位不变)。

### 验收修复轮:开口的人是谁,决定了要不要问他

三视角验收把上面那扇新门查出九处,都在同一根线上 —— **那扇门问对了人吗、答得了吗、
说的和做的是不是一回事**:

- **玩家自己开的口不再变成一封写给他自己的信。** 判据改成**这句话是谁开的口**,
  不是"参与者里有没有玩家":他在对话里说「陪我听完这一面」(`chat.intent` 的导演路,
  `Director._together`)时,那句话**就是他的同意**,当场带进去。此前他刚说完就会收到
  一封问他要不要做他刚要做的事的信,而那封信今天还没有一处画得出来。⚠️ 判据**不能**
  写成"`player_id` 非空且就是他" —— 她在对话里点他的名走的也是那条路,那么判等于
  把刚修好的洞重新挖开。**她点他的名那条路一个字没变**,有测试各钉一条。
- **一次只算得进一个还没点头的人。** 名单里出现两个都要被问的玩家时,`interact_with`
  **当场拒绝**(点名是哪几个),一封信都不发、每日额度一次都不扣。此前两封信发出去,
  谁先按"好"那一下都会在 `answer_invitation` 里撞上另一个人的 `player_not_you`,
  落成 `expired` —— **玩家按了「好」而世界一声不吭**,是这一层最坏的坏法。
- **重查的是闸,不是人心。** `answer_invitation` 的说明写着"重查一遍闸",实际重跑了
  整个 `_consent`,里面含**同意判定那次模型调用**:他按一下"好"要等一趟网络,而她
  这一次改主意的话他收到的是 `expired`。现在开口那一刻点过头的人写进事件的
  `consented`,答复时原样带回,只重查会变的那些(她还在不在、手上有没有别的事、
  他的位置)。说明和行为这一轮对上了,两侧各有测试。
- **`cancelled` 不再是个说了不算的枚举。** 她在聊天里 `walk_away` 时,还等着的邀请
  跟着收回(`Scheduler.cancel_invitations()`,唯一来源),和过期一样一个字都不写。
  ⚠️ 它挂在**她的动作**上而不是"她还在不在原地"的定时判断上 —— 后者会把「她去了趟
  隔壁」和「她不等你了」算成同一件事。`settle_invitation()` 现在拒掉任何不在
  `INVITE_OUTCOMES` 里的结局。
- **「某某此刻在做什么」真的只剩一份措辞。** 上一节说"一处分支都没有",而
  `_autonomy_context` / `world_context` 里她读**自己**那一句仍走 `_ACTIVITY_LABELS`
  的老路:同一个联合动作,别人读到「江晚(在一起听完一面)」,她自己读到「闲着」——
  两个人在同一件事里对不上账。两处都改走 `_activities_now()`(在路上那一句留着,
  它答的是"你在哪儿"不是"你在做什么")。
- **`invitations_page()` 一次调用答得出"还剩多久"**:多一格 `now_tick`,每行多一格
  `expires_in`。此前要画倒计时得再调一次 `state()` 去问时钟,而两次调用之间世界还在
  走。⚠️ 加在拷贝上,那条事件一个字没改。
- **文档欠的三笔**:`player_options()` 从 3.3.0 起就返回 `blocked_text`(枚举旁边那句
  人话),REFERENCE 两处都只列了 `blocked` —— 这一笔真的骗到过人:读 `own` 为空就报
  成"玩家看不到自己身上任何一个量",而真相是 `blocked=unknown_player_location`、
  人话就摆在下一格。`perception.activities` 是 3.6.0 加的,不是"3.5.0 之后"。
  FOR-STUDIO 写着 `importance` 写超范围「`validate world` / `world check` 都认,
  当场退 2」,**后半句是假的**:`world check` 问的是"这一版引擎装不装得下它",
  **答上来就是 0**,答案在 `loadable` 里 —— 拿 `$?` 当判据的流水线会把写错了的世界
  当合格品放过去。改的是文档不是退出码:退出码语义由 `test_validate_matches_boot.py`
  钉着,而两个命令问的本来就不是同一个问题。

⚠️ **一条没在这一轮改的,写进 FOR-STUDIO 当丑话**:一件 `occupies` 的长过程起了头
之后,她自己的排班仍可能在下一 tick 把她带走,到点落 `entity_disengage{reason:
"left"}`,**代价照付、效果一样不落**。那条账在排班与"她手上占着一件事"之间,不在
能力这一层,顺手改会牵到每个角色的行为树。但**它不再是无声的**:
`state()["runtime"]["subsystems"]["engagement_kept"]` 数得出成了几件、被带走几件,
`reason` 点名是谁的哪一件。

### 在场名单不再只数玩家;「某某此刻在做什么」全世界只有一份措辞

晚潮跑了 238 天、16.16 万条事件,作者写的 11 个「得有人一起」的动词
(并排坐着 · 陪一次夜播 · 一起听完一面 …)**一次都没被用过**。查到底不是模型的错,
是**名单从来没给过她**:`_autonomy_context` 的在场只遍历 `self.players`,于是一个
站在三个同事中间的角色读到的是「这会儿你身边没有别人」,而紧跟着的 `interact` 说明
让她用 `with` 点名 —— 点谁?**同一屏里一句话让她做一件事,另一句话告诉她这件事的
前提不成立**,而两句都是引擎自己写的。

同一条链上的第二个现场:齐老板在线上对一个刚擦完窗的玩家说「我没见你动过手」。
那句话在数据上**完全成立** —— 感知块给了她他身上的量(`手上的活儿 0.9`),没给她
一个字的动作。

四格,合起来是"在场这件事要算数":

- **在场名单收同一地点的角色**,每条带 `kind`(`agent`/`player`);关系那一格按
  `kind` 查对的键(玩家要剥 `player:`,`_relation_id`)。`_autonomy_menu` 那道
  `requires_colocation` 的闸**按玩家那一半判**(`reach_out` 的语义就是"要玩家真的
  在她跟前"),`interact` 这类不挑人的能力从此在"只有 NPC 在场"时也留在菜单上。
- **补齐 `_ACTIVITY_LABELS` 缺的 `interact` / `eat`** —— 一个正在干活的人此前被写成
  「闲着」。
- **`Scheduler.actions_now()` / `occupations_now()` → `World._activities_now()`
  是唯一的合并点**,三个读者共用它:聊天提示词的 presence 块、自主轮次的在场块与
  感知 notes、`Perception.activities` → `describe_here()`。**NPC 与玩家一处分支都
  没有**(她看得见他擦窗,他也看得见她擦窗);长过程盖过动作名(`在陪一次夜播`
  比 `在忙手上的事` 说得清)。各拼一遍必然分叉,而分叉不报错。
- **行为树撞上联合动词时说话**(`joint_from_tree` 子系统 + 一条只在档位切换时刷的
  日志),不再静默重试。**不替她挑同伴** —— 叫人要先征得同意,征同意要走网络,
  而行为树在时钟线程的锁里;她要一起做,走自主轮次那条路。

`Perception.to_dict()` 多一段 `activities`(只加不改,`World.perception()` 同步),
REFERENCE 那一格已记。

### Known

- 🔴 **`world check --edit` / `validate world --edit` 会给一份装不进去的世界文件开绿灯**
  (第九轮 2026-08-20 逮到,**行为有意没改**:那一轮是零代码轮,定版镜像正要从这个
  提交重建)。`--edit` 分支跳过 `_authored_ontology_errors`(= 开机那条路上的
  `_precheck_ontology`),于是量名拼错、动词没声明、`me_X` 没声明、`spawn` 没写代价、
  `emit` 三字段写坏、能力里写了 `rand()` 这六类**一条都不报**,`loadable` 照答 `true`、
  退出码 0 —— 而真装载时不看 `--edit`,同一份文件当场开不了机。判据:

      git grep -n '_authored_ontology_errors' -- anima_world/__main__.py
      # 三行:一行定义 + 两行调用(validate world 一处、world check 一处),
      # 两处调用都挂在 `else:` 上,而那个 `if` 就是 `--edit`。

  **危害不是"少查了",是它把"形状对"答成了"装得进去"** —— 一个答案被当成另一个答案读,
  正是 `world check` 这条命令当初要修的那种病。**在修好之前**:`--edit` 的绿灯只当形状
  体检用,要问"真的装得进去吗",连着目标世界跑
  `simulate --ticks 0 --world-file <file> --world-id <目标>`(顺带查引用完整性)。
  修法(把这一摞在 `--edit` 下也跑起来,只把引用完整性那半留在 `else` 里)另立小单 ——
  它会让一批今天绿的 `--edit` 调用变红,所以不该混在定版这一趟里。
  文档三处已认下:REFERENCE §不建世界就检查那条 🔴、FOR-STUDIO §3.21 / §3.17 / §5 /
  §CLI 可达性。

## [3.5.0] —— 抹除被杀在半路之后,名字不许丢 (2026-08-20)

**这一版修的是正确性,不是性能。** 晚潮(21 角色 / 13.9 万事件)的法务抹除在
`POST /internal/v1/erasure` 上 8 次跨 67 分钟全 503、队列永不收敛,查下去查到三个
数字错位(壳的答复预算 5 秒 ≪ 一趟 188 秒、寄存 TTL 300 秒 < 站点重试 600 秒)——
那三个数归宿主。而顺着那条链查出来的**引擎这一侧的洞是另一件事,而且更硬**:

**3.4.0 及更早,一趟抹除被杀在半路就再也补不齐了,一处不报错。** 改写从低 seq 往高
seq 走,而他的名字的来源之一就是日志自己(`*_id`/`*_name` 配对);`erase_player`
又在第一遍之后就调 `forget_player`,把在场与联系态这两个名字来源清掉了。于是半路
被杀之后:低 seq 那半的配对已经是「(已注销)」→ **重跑的第一遍收不到他的名字** →
`replacements` 是空的 → 尾巴上那些**只在自由文本里提过他名字**的句子
**再也抹不掉**。世界照跑、日志干净、回执上每一格都对。

### Added

- **抹除可续**:解析好的名字、涉他的 seq、改写水位与已累计的计数,在动日志的
  第一个字节**之前**落进 `anima:{world_id}:erasure:{player_id}`(HASH,TTL 24 小时)。
  续跑一律读它,**绝不重新推断名字** —— 那正是上面那个死角。**平时这个键不存在**:
  键在就说明有一趟停在半路。走到日志尽头时连同它记着的名字一起删掉。
- **抹除可分片**:`World.erase_player(..., since_seq=, limit=, resume=)` 与
  `anima-world player erase --since-seq / --limit / --resume`。⚠️ **分的是改写那一遍**
  —— 收名字那一遍永远 O(全量事件)(他的名字可能出现在任何一条事件的自由文本里,
  而那种句子不带他的 id,任何按 `player_id` 建的索引都覆盖不到),`World.open` 那次
  重放更是分不动。**所以它不让任何一发请求变快**,别拿它去换宿主那侧的墙。
- **回执多两格**:`phase`(`not_started` / `partial` / `done` —— **这个人在这个世界里
  的抹除处在哪一步**,不是"他被抹干净了没有";一次**预演**也答得出 `partial`,
  而那正是宿主今天问不出来、只能把"被墙挡在门外"和"抹到一半"混成同一个 503 的
  那一格)与 `resume_seq`(下一趟从哪儿接着看)。
- `contract --json` 新增 `erasure` 段,**八格**:`read_command`(`player erase`)/
  `write_command`(`player erase --yes`)/ `resume_command`(`player erase --yes --resume`)/
  `shard_params` / `phases` / `progress_key` / `progress_ttl_seconds` / `gloss`。
  **消费方按出口在不在探测,不比版本号** —— 照 `location_image_write_command` 那条先例。
  ⚠️ **那三格命令里的 `--yes` 一个都不能少**:这条命令默认是预演,`resume_command`
  发出去的第一版少了它(验收当场逮住),而照它原样敲的续跑是一次 `dry_run: true`
  的空转 —— 退出码 0、水位一格不动,宿主拿它驱动重试会**永远** partial 下去。
  报一条照着敲会静默什么都不做的命令,比不报这一格还坏。

### Changed

- `storage.volatile_keys` 多一格 `erasure:{player_id}`。**镜像端会当场红,这是对的**
  (运维台 `test/contract.test.js` 对着真门逐格 deepEqual)。打包与装包两侧都跳过它
  —— 一份"这个人叫什么、我们正要抹他"的清单进了分发物,比不抹还糟。
- 导入侧的跳过收进 `_is_volatile_key` 一处,顺带把 `lock` 也拦住了:导出早就跳过它,
  而导入没有 —— 一个手改过的包能装进一把永不过期的死锁。
- `--resume` **只续,不新开**:没有未完成的就什么都不做(各格全 0、
  `phase="not_started"`)。不带它的普通重跑照旧自动续上,所以宿主的重试循环
  一个字都不用改。真跑的 `--since-seq` **不许越过已完成的水位**(越过去就是在日志
  里留一个洞,而下一趟从更高的水位接着做,没有一处会报错)—— 越了退 2。
  预演不受这条管:它不写,造不出洞。
- 停在半路时**不写审计事件**(`player_erased` 的意思就是"抹完了"),CLI 也
  **不印它**,改印一行"还没到日志尽头"。只印一行计数会被读成做完了,而这条路上
  "以为做完了"是不可逆的那一边。
- 转录与记忆在**第一片**就删掉,**而且挪到了那个长循环之前**:它们便宜、有界,
  又是这条链上最私密的一份 —— 放在循环后面,第一片被杀在循环里就留下"转录一条
  没动";放在前面,被杀留下的是"最要紧的已经没了、剩下的是改写"。代价不对称,
  所以顺序不对称。续跑不重做它们,计数从进度键带过来。

### Fixed

- **拒绝路径不许先写世界再拒绝。** `since_seq` 越水位那一句原先排在 `forget_player`
  **后面**:一条被拒的命令 rc=2、stdout 零字节,而世界已经被改了(在场与联系态清掉、
  日志多一条 `player_departed`)—— 连敲三次 `max_seq` 54→57。三条校验现在一律排在
  任何一次写之前。**一次拒绝在调用方那儿的意思就是"什么都没发生"**,而这条路上
  没有比这更容易被信以为真的一句话。
- **`phase` 与 `resume_seq` 不再自相矛盾。** 两条硬不变量:`partial` ⇔ 进度键在
  (真跑被截断时**一定**留下一个,哪怕这一趟什么都不用改);`not_started` ⇒ 这一趟
  一个字都没写且 `resume_seq` 必然是 `None`。从前"没什么可抹 + `--limit` + 循环期间
  落了别人的新事件"会报 `{"phase": "not_started", "resume_seq": 55}` —— 而那一趟
  已经跑过 forget;宿主照 `not_started` 判"还没开始"、照 `resume_seq` 去续,而
  `--resume` 又回它"没有未完成的":三句话互相打架,没有一句是对的。
- 导入侧也跳过 `lock` 与进度键(`_is_volatile_key` 两头共用)——
  手改过的包能装进一把永不过期的死锁,而这支路此前零覆盖。

### Known

- **预演的 `memories_redacted` 可能偏大**(实测预演 3 / 真跑 0)。真跑时
  `erase_for_event_seqs` 先把由他而起的记忆整行删掉,`redact_summaries` 就数不到
  它们了;预演不真删,于是同一批被数第二遍。**3.4.0 就有,不是这一轮的回归**,
  方向是偏大不是偏小(当上界安全),修法另立小单。判"抹干净了"看
  `conversations` / `messages` / `memories_dropped`。

⚠️ **这一版仍然没有打 tag、没有发 PyPI**(发布那条路的账见 3.3.0 那一节),
上线只走镜像。`engine_min` 的地板**一格没抬**:纯加法,已发布的世界一个都不受影响。

## [3.4.0] —— 那扇明着欠下的写门,以及一个版本号下不许有两套引擎 (2026-08-19)

清一批"已知没修"的账。四条里三条是同一个形状:**引擎自己给出的那个答案是假的,
而下游照着它做了判断**,没有任何一处报错。而同一个形状在舰队上现了第四次身,
于是这一轮多出一个新出口:**"这一版引擎装得进这份世界文件吗"从今天起问得出来。**

**为什么这一堆是 3.4.0 而不是继续挂在 3.3.0 下面。** 3.3.0 定版之后,工作树又走完了
上面这一整轮(地点两格图、图的闸、`world check`、`agent set-card --portrait-file`、
那五条 🔴),而它们**全都顶着 `3.3.0` 这个号**。舰队上跑着的 `anima-world:3.3.0`
镜像里连 `anima_world.media` 这个模块都没有,`contract --json` 的 `seed` 段一格图
都没有 —— 而它自报的版本号和工作树这一份**逐字相同**。于是消费方的能力探测
**问不出**两者的差别:同一个号下有两套引擎,而这正是 3.3.0 那一节自己写下的教训
(「同一个 `3.2.0` 下有过七份不同的引擎」)第二次发作,这一次是在生产上。
**定版的唯一目的就是终结它** —— 版本号是消费方唯一读得到的那句自述,
让它对两套不同的引擎说同一句话,和一把把上升报成下降的尺子是同一类东西。
⚠️ 这一版**没有打 tag、没有发 PyPI**:发布那条路的账见 3.3.0 那一节,
tag 留给仓库主人扣扳机,这次上线只走镜像。

### Added

- **`anima-world location set-image` —— 那扇写门真的有了**(`World.set_location_image`
  + CLI + `contract --json` 的 `seed.location_image_write_command`)。
  上一轮它是**明着欠下的一笔**:作者层是"只填缺、不覆盖",而合并粒度是**整个地点行**
  (`seed_defaults(merge=True)` 按地点 id 整条跳过已有地点)——于是拿一份补了图的
  `.cyberworld` 去编辑一个**已经跑起来的**世界,那两格一个都装不进去,
  而作者写得进、`validate world` 放行、包也导得出。**角色卡那一次的形状逐字重演。**
  当时的处置是"不让它无声"(逐个地点 `logger.warning`、离线两扇门也说),
  但**一句警告不是一扇门**:退出码仍然是 0,而事没做 —— 也就是说下游脚本
  照样把它当成功。
  形状逐格照着 `agent set-card`(它已经是运维台维护白名单里的成熟先例):
  **明示的编辑所以覆盖**(和作者层合并有意相反,两边 docstring 互相点名)、
  **两格分开合并**(只给 `map_image` 不许把作者写了几周的 `scene_image` 顺手抹掉)、
  **空串抹掉一格 / `--clear` 抹掉两格**、**逐字相同就一个字都不写**、
  **`--dry-run` 一个字节都不写**、地点不存在**退 2 并把这个世界里有哪些地点说出来**。
  ⚠️ **它只写这两格,别的键当场拒绝** —— 和角色卡"不认识的键原样带过去"有意不同:
  地点行的其余部分(名字、描述、几何)只有一个合法的写入者,就是作者层;
  在这里开第二个,"这张地图为什么变成这样"就多出一个日志之外的答案。
  ⚠️ **它不发事件,而 `set_card` 发**,这条差别也是有意的:角色卡住在
  `agent_join.payload.spec` 上——它的家本来就是事件日志;地图不是,`locations` 表
  是它唯一的权威(`projection.py` 早就为此退役了 `location_desc_update`:
  **地图是配置,不是历史**)。
  ⚠️ **`--map-image-file` / `--scene-image-file`**(`-` = 标准输入)和
  `--portrait-file` 同一个理由,而这里的距离更远:契约公布每格 **256 KiB**,
  而 argv 单个元素被 `MAX_ARG_STRLEN` 封在 **128 KiB** —— 上限本身就是那道坎的
  两倍。**两格都写 `-` 当场拒绝**:标准输入只有一份,第二格会读到空,
  而空在这条路上的意思是"抹掉这一格" —— 一次手滑会安静地删掉线上那张图,
  而回执上写着"改了"。
  读 URI 文件那四条拒绝(读不了 / 不是文本 / 空文件 / 中间有空白)收进
  **一份** `__main__._uri_from_file`,立绘那扇门改成调它:两份的话,下一次收紧
  只会收紧其中一处,而另一处不报错,它只是继续放行。
  ⚠️ **「别动这一格」和「抹掉这一格」是两句不同的话,四扇门给同一个答案**
  (验收挑出来的:头一版里 argv 那侧和 `-file` 那侧对同一个输入答得不一样)。
  **不给这个 flag / 不给这个键 = 别动**;**明写空串 `''` = 抹掉**;
  **纯空白 `'   '`、空文件、API 的 `None` = 拒绝**。第三类是这一条的重点,
  理由是**代价不对称**:运维台模板里一个没展开的变量走 argv 进来长得就是 `'   '`,
  `None` 在宿主那儿最常见的来源是 `row.get()` 没取到值 —— 把它们读成「抹掉」
  就是一次**静默删图**,而回执上写着「改了」、退出码 0;而拒一次的代价只是
  调用方补一个字。**「别动」已经有写法了(不给这个键),所以 `None` 不必再承担
  第二种含义。** 判断落在 `World.set_location_image` 上、**不在 CLI 上** ——
  放 CLI 上的话两扇门迟早分叉,而分叉的那一半不报错。

- **`anima-world world check <file> [--edit] [--json]`** —— 一句"这一版引擎装得进
  这份 `.cyberworld` 吗"的**真校验出口**:跑的是开机第一秒调的那两个函数
  (`__main__.authored_layer_errors` + `__main__._precheck_ontology`),
  **不读封皮、不连 Redis、不建世界**。
  由来是运维台正式立的一条跨仓诉求(`platform/docs/引擎接口诉求-世界文件可装载性.md`):
  2026-08-19 舰队上灯塔湾 `815b3ae9` 的容器 0.57 秒退出 1
  (`stocks[8] 'values' must be an object` ×11 —— 卷上那份 `world.cyberworld` 是
  2.3.0 时代导入的,作者层是今天不再接受的形状),而**离线 `world inspect --json`
  对同一份文件照答 `runnable: true`**:下游按引擎自己的假答案做了判断。
  运维台拒绝在自己那侧补,是对的 —— 补法只有再实现一份作者层校验,那是引擎语义的
  第二实现,猜错了不报错。
  ⚠️ **`world inspect` 那一格一个字没动**:`inspect` 读封皮,答的是**作者声称**要
  哪个引擎(廉价、正当,一个 5 GB 的包也只是一次 readline);`check` 答的是**这一版
  引擎真的收不收它**。两个问题,别互相顶替。
  ⚠️ **退出码问的是另一个问题**:`validate world` 是**作者的门**(错误 2、提醒 0,
  CI 里当断言用),`world check` 是**宿主的门** —— 0 = **这句话我答上来了**
  (`loadable` 才是答案,`false` 是一个答案不是一个异常),1 = 没答上来(**文件打不开**:
  路径错 / 没权限 / 指着一个目录,`loadable: null`,**世界根本没有被看过**),
  2 = 这一版引擎没有这个子命令。和"`start` 是人的门、`run` 是程序的门"是同一条分法。
  ⚠️ **退出码 1 那一格只装"文件打不开"**,这条界画错过一次:凡是读文件出错就报
  "没答上来"的话,**不认识的记录类型 / 平铺的 `body` / 不认识的 `author` type /
  格式版本比引擎新 / 校验和对不上**这一整摞会全落进 1 —— 它们每一条都让开机当场
  失败、`validate world` 也照实报错误,而这个出口自己写着"问不出来一律不拦",
  于是**一份引擎明确拒收的文件会被下游放行**(同一种病的又一个版本:一个答案被当成
  另一个答案读)。分界不在"读的时候出没出错",在 `_cannot_even_look`:
  **这个文件被看过没有。**
  ⚠️ **能力探测按子命令在不在做,不比版本号**:`validate world` 在老引擎上**也存在**,
  只是那时它对一份纯状态层的导出包答 `'agents' must be a list`(存在但答错,
  是能力探测最骗人的形态),而同一个 `3.2.0` 下有过七份不同的引擎。
- **`anima-world validate world --edit`** —— "这份文件要装进一个**已有**的世界
  (= 一次编辑)":不要求它把名册和地图再抄一遍,引用完整性也不查(它们可以来自
  目标世界),并把**没查的那一半说出来**。开机那条路一直按目标世界空不空自己判
  (`_world_exists`);校验器手上没有目标世界,所以那一格只能由调用方说。
- **地点有图了,而且是两格:`map_image` 与 `scene_image`**(`anima_world/media.py`
  新模块 + `world_seed` / `world_store` / `redis_state` / `api` / `__main__` 五处落点)。
  两个都**可选**,进 `seed.location_optional_keys`;**必填集 `location_keys` 一个字没动**,
  `manifest` 与 `CARD_KEYS` 也没动。名字各自说的是它用在哪儿 —— 这正是唯一要分清的
  那个区别,而**名字发出去就收不回来**,所以在这里写死:
  - **`map_image`** = **地图上那一格**的缩略图(小、方形、一眼认得出是哪儿);
  - **`scene_image`** = **走进这个地点之后铺开的那张大图**(要留白,压暗了还压得住正文)。

  「背景」不是第三样东西 —— 它**就是**这个地点的图,所以没有第三个键。
  **落点必须是五处,漏一处就静默丢**,这条是照着角色卡那一次的病逐个数出来的:
  作者层 schema(`WORLD_SEED_LOCATION_OPTIONAL_KEYS`)→ 落库
  (`world_store._LOCATION_FIELDS`,漏了它作者写的图在加载那一刻就被过滤掉)→
  读出口(`api._LOCATION_KEYS`,`state()` 是它们**唯一**的读门)→ `map_data()` 的
  `places` 行 → `contract --json`。前四处任意一处漏掉的样子都一样:图一路存进 Redis,
  玩家永远看不见,全程零报错。
  ⚠️ **引擎一个字节都不存**:这两格装的是**绝对外链**,图的家是网站那侧的 `/media/`
  图床(那条契约的权威不在本仓)。引擎不下载、不缩略、不联网、不生成 —— 它只管
  "这一格里的话说得通不通"。
  ⚠️ **写门欠着,而且这一笔是明着欠的**:作者层是"只填缺不覆盖",而
  `seed_defaults(merge=True)` 是**按地点 id 整条跳过**已有的地点 —— 于是拿一份
  带图的文件去编辑一个**已经跑起来的**世界,图装不进去。这正是角色卡那一次的形状
  (线上 20 个早就在册的角色一张卡都装不进去,全程零报错),所以这一轮先**不让它
  无声**:跳过时逐个地点点名 `logger.warning`,说清楚"这次没装进去的是哪几格"。
  真正的修法是补一扇写门(形状对着 `agent set-card`:`location set-image`),
  它是下一单的事。
- **图的闸:scheme 必须是绝对的,`data:` 不禁但有字节上限**(`media.media_uri_errors`)。
  和 `portrait` 同一道闸、同一个理由:相对路径的图**发出去就是一张断的图,而作者
  自己看不见**(他本地打得开)。`data:` 照旧收 —— 它是"一个包自足"的唯一实现,
  禁它等于拿契约当政策工具。**真正的病一直是"没有上限"**:
  - **`portrait` ≤ 1 MiB(1048576 字节)** —— **这一格此前引擎一个字节都没管过**,
    是引擎自己的漏:一张 8 MB 的图 base64 进世界文件,加载放行、导出带着走、
    `roster()` 每次整个吐出来,没有任何一处说话。
  - **`map_image` / `scene_image` 各 ≤ 256 KiB(262144 字节)。**

  **两个数不必相等,因为上限是按读出口定的**:`roster()` 是按需拿一次,而地点的图
  骑在 `state()` 上 —— 那道门被网站每几秒轮询一次,且**一次带回全部地点**。
  「进得了提示词的必须有界」在这里的对偶是**进得了读出口的必须有界**。
  ⚠️ **量的是这条 URI 字符串本身**(`len(uri.encode("utf-8"))`),不是原图字节:
  base64 之后约 **4/3**。所以创作台那侧若按**原图**字节设闸,1 MiB 的原图会变成
  约 **1.33 MiB**(1048576 × 4/3 + 22 = 1398126 字节)的 URI 而**被引擎拒收** ——
  立绘那侧建议卡在 **750 KiB 原图**左右(768 KiB 是数学上的临界,加上前缀正好越线)。
  一条规则对所有 scheme 一视同仁(`https:` 的长 URL 也照量),不给 scheme 开分支。
  ⚠️ **是 error 不是 warning,而这一条是查了真数据才敢定的**:线上两个活世界
  (灯塔湾 2385 条事件、晚潮 145674 条)、七份归档 `.cyberworld`、创作台全部
  seed/beats —— **`data:` 零处、`portrait` 零处**;自带的 `demo.cyberworld` 用的是
  一条 https 立绘。也就是说这两道闸落成"当场开不了机"**不会让任何一个已有的世界
  开不了机**。反过来,给一条**写坏了的图**发警告等于让它一路存进世界再在玩家那儿
  变成一张断图 —— 而作者永远收不到那个反馈。
  闸挂在 `authored_layer_errors` 上,所以**开机 / `validate world` / `world check`
  三处给同一个答案**(`tests/test_validate_matches_boot.py` 钉着);它**不在**
  `world_seed_errors` 里 —— 那是镜像端持有的那张必填表,可选键的闸有意挂在外面。
- **`world check --json` 多两段:`external_media` 与 `inline_media_bytes`。**
  前者**按 host 归并**列出这个包里的每一条外链(哪台服务器、几张图、哪几个字段),
  后者数自带的 `data:` 有几条、共多少字节、最大一条多大。
  ⚠️ **不联网探活**:要答的是"这个包挂在哪几台服务器身上、自己又带了多少字节",
  不是"这些链接还活着吗"(那是运维的活,答案每分钟都在变,而一个会因为网断了
  就报红的校验器只会教人忽略它)。**"包发出去图就没了"是产品选外链要付的代价,
  不是格式的缺口** —— 引擎该做的是让这个代价**在拿到包的那一刻看得见**。
  两条实现上的坑各被自己的第一版当场打脸:Redis 的 hash 行在包里是 **JSON 字符串**
  不是嵌套 dict(不拆开就一张图都扫不到,而那正是导出包的样子);同一条 URI 会在
  `:meta.world_seed`(创世出生证明)、`agent_join` 载荷和 Redis 行里各出现一次
  (**同一件事数成三件,和数漏了一样是个错答案**),所以按 URI 摘要去重 ——
  现在同一个世界的**作者层文件**和它**跑过之后的导出包**报出逐格相同的账。
  文件根本打不开时这两段是空的:**没看过就不给账**,半本账比没有账更坏。
- **`anima-world agent set-card --portrait-file <PATH>`**(`-` = 标准输入)——
  补上契约与这扇门之间那一段够不到的距离。`--portrait` 在 URI 超过约 **128 KiB**
  时会炸,而**炸的不是引擎**:Linux 的 `MAX_ARG_STRLEN` 把单个 argv 元素封在
  128 KiB(实测 122902 字节过、177518 字节炸),再往上 `execve` 直接 `E2BIG`,
  壳报「参数列表过长」给 **rc 126** —— 引擎连被叫起来的机会都没有,于是没有回执、
  没有退出码 2、没有一句能翻译给运维的人的话。而 `portrait_max_bytes` 公布的是
  **1 MiB**,一条 `data:` 立绘到那个量级是常事。**文件里装的是那条 URI 文本,
  不是图片字节** —— 引擎照旧一个字节都不碰(嗅 MIME、转 base64 是创作台的活)。
  两头空白掐掉(尾部换行是必然的);**空文件退 2** 而不是读成"抹掉这一格"
  (一次失败的写会安静地删掉线上那张立绘);URI 中间还剩空白(编辑器折了行)
  也退 2 —— 原来那道闸只看 scheme 和字节数,这种 URI 它照放,出去就是一张断的图。
- **契约现在说得出"这两格没有写门"**:`seed.location_image_read_command`(`"map"`)、
  **`seed.location_image_write_command`(`null`)**、`location_image_write_gloss`
  一句人话。**"没有写门"是一个答案,沉默不是** —— 少了这两格,这一段看上去和
  `character_card`(它有 `write_command: "agent set-card"`)一模一样,而消费方的
  铁律是"问引擎不读文档":它会合理地推断"作者层写得进,跑着的世界当然也改得动",
  然后把那个按钮画出来,点下去改不动任何东西,零报错。
- **`validate world --edit` / `world check --edit` 现在也说"这次编辑的图装不进去"。**
  这句话从前**只在真开机时**进服务器日志,而作者的顺序是先校验、绿了才装 ——
  于是他拿到一个绿灯、装完、图没了,只有去翻日志才查得到原因。离线两扇门存在的
  全部理由就是**开机之前把话说完**。两扇都加(`test_validate_matches_boot` 只钉
  错误相等、警告可以不同 —— 少钉一扇,两扇门就会安静地给出不同的判断)。
  文件里真带了图时才说,而且逐个点名:一句总在响的警告等于没有警告。
- **橱窗里演得出图了**:`demo.cyberworld` 的三个走得进去的地点(`cafe` / `workshop`
  / `home`)各配两格图,两个大区**故意留空**——那两格是可选的,全写满会让人以为必填。
  **做了却开箱看不见等于没做**:交付时橱窗一格图都没有,于是新用户第一屏证明不了
  这个引擎带得动图,网站那侧也没有一个**带图的参照世界**可以对着写前端(只能照文档
  编一份,而编出来的形状和引擎真给的形状不一致时没有一处会报错)。
  `tests/test_flagship_seed.py` 两头都验:文件里写了、`state()` 与 `map_data()`
  真的带出来了 —— 只验前一半的话,一次改错键名的重构会让图安静地停在半路。
  **补上它的当场就逮出一个线上会炸的 🔴**(`anima-world map` 见 Fixed 第一条)——
  这道闸不是仪式。

### Fixed

- 🔴 **`anima-world map`(不带 `--json`)对任何一个**配了图的世界**当场 TypeError**
  (`__main__._map_frame`)。`_map_frame` 是 `MapPlace(**place)` —— 而 `map_data()` 的
  `places` 行是**契约**,加图那一轮它多了 `map_image` / `scene_image` 两格,于是
  `MapPlace.__init__() got an unexpected keyword argument 'map_image'`:**这个引擎
  最出片的那条命令,在它自己新加的特性面前直接崩**。渲染是赠品、`--json` 才是契约
  (CLAUDE.md 原话),所以现在**逐格取**,不再 `**place`。
  ⚠️ **它是被"橱窗里补上图"这一件事逮出来的**,不是被想出来的:交付那一轮
  `demo.cyberworld` 一格图都没有,于是全套 1600 多条测试里没有一条走得到"带图的世界
  过一遍字符画"这条路 —— 而线上灯塔湾一旦配了图,第一个敲 `anima-world map` 的人
  就会撞上。这正是 CLAUDE.md 那三道闸里第一道("橱窗里展示它了吗")存在的理由,
  而它这次是**真的**拦下了一个线上会炸的错。
- 🔴 **`world check` 的 `external_media[].fields` 给的是个错答案**(`media.py`)。
  `fields.add` 挂在按摘要去重的那次 early-return **下游**,于是同一条 URI 用在
  两格上时,第二格**整个消失**:三处引用同一张图的世界报出来是
  `{"count":1,"fields":["map_image"]}`,`scene_image` 一个字都没有,
  而 CHANGELOG 自己写着这一段报的是"哪几个字段"。
  **`count`(几张图)和 `fields`(这台图床供着哪几格)是两个问题,去重只该答第一个。**
  这条排在最前面是因为网站的图床是**内容寻址**的:同一张图传两次拿回同一条 URL ——
  "一张图既当缩略又当背景"是**常态,不是边角**。准备自建部署的人照这段账读下来,
  会得出"这台服务器只供缩略图"的结论,而这一整段存在的理由正是让那笔账**看得见**。
  修法是把"这条 URI 见过没有"和"哪一格在用它"拆开(`tests/test_location_media.py`
  那条"同一张图抄在几处只算一张"补了 `fields` 的钉子,旧实现上当场红)。
- **算错的一个数**:`data:` 立绘那道换算三处写着 `约 1.37 MiB`,正确的是
  **1.33 MiB**(1048576 × 4/3 + 22 = **1398126** 字节)。它是**给创作台照着设闸的数**,
  差 4% 就够让一批图卡在两侧中间。`media.py` 模块 docstring / `CHANGELOG` /
  `FOR-STUDIO §3.22` 三处一起改。
- **`world check` 那段图账现在说得出它自己是干什么用的**:抬头从"只报这个包指向哪儿"
  改成 **"这些图不在包里:发出去之后要靠下面这几台还活着"**。前一句描述了行为,
  没说出代价 —— 而这一段存在的全部理由就是让那笔代价在拿到包的那一刻看得见。
  全内嵌的包则明说"都内嵌在包里,不依赖任何一台服务器"。
- 🔴 **玩家那几扇门补课时一把锁都没拿,而水位还能被按回去**(`api.py` / `scheduler.py`)。
  `catch_up_projection()` 是四拍(读水位 → replay → 折 → 写水位),一直是**要在
  `scheduler._lock` 下调**的 —— `state()` / `act()` 就是这么写的。而玩家那一侧
  全漏:`forget_player` 的 `dry_run` 前导**一把锁都没有**,`erase_player` 只有
  `_guard()` 那把**跨进程** RedisLock(它挡别的进程,挡不住自己的 tick 线程,
  而劈开那四拍的恰恰是同进程的第二条线程),`player_engagement` / `set_card` /
  见面礼那条同病(后三处不在诊断清单里,是对着九个调用点逐个数出来的 ——
  **半闭的不变量比不闭更坏,它读起来像是闭上了**)。
  后果不是慢,是**账翻倍**:第二拍拿到的 `fresh` 可能已经被 tick 线程折过,
  这里再折一遍,而 `payment` / `item_transfer` 折两遍就是钱和物品凭空多一份 ——
  事件日志一条不错,没有任何一处报错。
  第四拍还有第二个洞:它是**直接赋值** `= max(fresh 的 seq)`,不是
  `max(旧水位, …)`。水位于是会被从 tick 线程刚推到的位置**按回去**,被按回去的
  那一段下一次补课再折一遍 —— 一处漏锁从"这一次多折一遍"变成"往后每一条都跟着
  再折一遍"。**锁是纪律,`max` 是那道兜底的闸:它单点挡住整族回拉。**
  (往回倒带只剩一条合法的路:`reset_projection`,它连投影一起从头重建。)
  今天这个窗口是毫秒级的(两条线程抢一台机器),所以线上从没观测到 ——
  写下来是因为它挡在下一步的路上:**宿主一旦把这类调用挪进线程池,窗口就从毫秒
  变成整整一次全表扫描那么长**,而那时它一声不吭。
  `tests/test_cross_process_projection.py` 两条新的都在旧实现上真的红:
  `test_水位不许往回拉`(水位 28≠29,那笔 7.0 折成 14.0)、
  `test_玩家那几扇门补课时手里拿着锁`(`_is_owned()` 逐扇门问 —— 起线程去撞会
  得到一个偶尔红的测试,而偶尔红等于没有)。

- 🔴 **`RedisEventLog.page(limit=…)` 从来没把 `limit` 交给 `LRANGE`** ——
  读完整条尾巴再切。于是任何"一页页翻完整条日志"的活整体是 **O(N²/2)**,
  而它**长得和线性一模一样**:每次确实只返回 `limit` 条,没有一处报错,
  只是世界越老越慢。`MySQLEventLog.page` 那边一直是真的 `LIMIT` ——
  **同一份"接口逐字相同"的接口(`events.py` 的原话),两个后端从来不在一个
  复杂度上**,而 fakeredis 上跑的全套测试对这种差别一个字都问不出来。
  实测(同一台机器,一条日志翻一遍再抹一遍):
  2 万条 2149 ms → **177 ms**(12 倍),6 万条 18985 ms → **561 ms**(34 倍)。
  改前是 3 倍数据 8.8 倍时间(平方),改后 3 倍数据 3.2 倍时间(线性)。
  13.9 万条外推过去约 100 秒 —— 那正是 2026-08-19 线上那 80 秒的形状。
  ⚠️ **`who`/`kind` 那一支有意没跟着截**:过滤在客户端判(列表没有二级索引),
  要凑够 `limit` 条命中的就得往后翻多少读多少。跟着截的话一页会少给甚至空给,
  **而调用方看到的是"没有更多了"** —— 拿一次静默的截断换一点速度,是这个仓库
  最不该做的交易。那一支的 O(尾巴) 是这个后端的既有账,不在这一轮里翻。
  三条新测试量的是**读回来多少行**、不是墙钟(一把会因为机器快而绿的尺子,
  量的从来不是它说要量的那件事):`tests/test_redis_state.py::test_翻一页只读一页_而不是读到日志末尾再切`
  / `test_从头翻到尾读的行数只跟日志一样长`(旧实现上是 **21000 ≠ 2000**,当场红)
  / `test_按人按类型筛的那一页照旧凑得满`(挡的是"顺手把过滤那支也截了")。
- 🔴 **`validate world` 对三种文件里的两种,答案和开机是反的**。同一份 `.cyberworld`,
  校验器和真开机说不同的话 —— 比没有校验器更坏,它教会使用者不信它:
  一个跑过的世界导出来(只有状态记录)开机欢迎它,校验器答
  `'agents' must be a list (missing)` + 退出码 2(**作者层为空 = 没有种子,
  不是一个空种子**);一份只带 `kinds` 的编辑层装进已有世界是允许的,校验器照旧
  要求一份完整的名册和地图。而反过来,规律/本体那一整摞闸(量名拼错、动词没声明、
  `me_X` 没声明、`spawn` 没写代价、物品 id 解不开)校验器从前**一条都跑不到**
  (FOR-STUDIO §3.17 把这个缺口列成过一张表,建议"出包前那一步请是
  `simulate --ticks 0`" —— 那句话诚实,但它把一件引擎该答的事推给了每个消费方:
  运维台的判包容器里没有 Redis,创作台的体检跑在世界之前)。
  修法是**判断只留一份**:三道作者层闸收进 `authored_layer_errors`,本体那一摞走
  `_precheck_ontology` —— 都是开机调的那一个,不是抄过去的第二份。
  `tests/test_validate_matches_boot.py` 是这条纪律的闸:同一批文件驱三条路
  (`validate world` / `world check` / `World.open(world_file=…)`),答案必须逐个相等;
  哪一侧多写一条或少写一条闸,它当场红。⚠️ 断言写成两半是有意的 —— 先断"两边一致",
  再断"一致到哪个答案上":只断前一半的话,两边**一起**判错还是绿的。

- 🔴 **橱窗封皮上"要哪个引擎"那一格是假声明**(`demo.cyberworld` 的 `engine_min`:
  `2.3.0` → **`3.3.0`**;`source_engine_version` 同步)。它上一次动是 2.3.0 那一版,
  而橱窗此后跟着走了 3.0–3.3 四轮:量声明里用上了 `bands` / `label`
  (**3.1.0 才进字段白名单**,更早的引擎 `不认识的字段` 当场拒整个世界),
  `config` 段点亮了 `chat.persona_anchor.enabled` / `memory.consolidation.enabled` /
  `economy.player_allowance`(**三个都是 3.3.0 加的键**)。
  **后果是跨仓库的**:`world inspect --json` 照实报 `engine_min: 2.3.0`,
  于是运维台的判包一次性容器对一个 3.1 或 3.2 的引擎镜像答 `runnable: true` ——
  然后开不了机,**而报出来的原因指错人**(那是包的声明错了,不是引擎坏了)。
  这就是"照跑但给错东西"的跨仓库版:一个下游按引擎自己的回答做了判断,而那个回答是假的。
  **为什么钉 3.3.0 而不是 3.1.0**(硬底线是 3.1.0 —— 装得上就跑得起来):
  `engine_min` 要答的是"哪些引擎跑得了**这个世界**",不是"哪些引擎不会当场崩"。
  3.2.0 装得上它,但 `apply_seed_config` 会把那三个键**静默跳过**(未知键跳过而不
  拒绝,那道宽容是给装载器的:一份更新的世界不该让老引擎整个打不开)——
  于是开箱的"橱窗"少三件展品:她的人设不再尾部重注、夜里不固化记忆、
  **玩家进来兜里一分钱都没有**,而世界照跑、日志干净。
  拿一个会静默少装的引擎换一个 `runnable: true`,买到的正是这个仓库最怕的那种坏法。
  **装载器要宽容,声明必须诚实** —— 这是两件事,不是一件。
  闸是**一个等号**(`tests/test_flagship_seed.py`):橱窗的 `engine_min` 与
  `source_engine_version` 必须**等于** `anima_world.__version__`。
  ⚠️ 这条比原来那两道严,而这一版严得有理由:上一轮写的是"两道闸盯着以后"
  (点亮的键必须落地 + `runnable` 必须为真),**那句是夸大的** —— 把封皮改回
  `2.3.0`,两条一条都不红:前者拿的是**当前**引擎的 `config_list()`,和封皮无关;
  后者只挡"写高"。写低那一半当时其实没有任何机械保障。
  等号的理由是"坏声明一个字都不写":这个仓库对橱窗产生过的**全部证据**只来自一个
  引擎 —— 它自己那个(测试套件跑的就是它)。任何更低的数字都是一句没人验过的话,
  而它偏偏是下游唯一读得到的那句。代价说清楚:**升 `__version__` 就要顺手改
  `demo.cyberworld` 第一行的两个数**,这是有意的成本(橱窗以纯文本进仓库正是为了
  这种改动能被 review)。要"它在 3.1.0 上也跑得动"这种更松的声明,得先有一次真的
  在 3.1.0 上跑 —— 那条路的形状记在 `docs/FOR-STUDIO.md` §3.21 ④。
- **关系边的审计视图和 `as_of` 对同一行给两个答案**(`redis_state.RedisKnowledgeGraph.query`)。
  `invalid_at` 那道下界("永远晚于 `valid_from`",R3)本来就是**读侧写侧共用一个函数**,
  但读侧其实是**两个视图**,而只有 `as_of` 那一支夹了:`include_invalid=True`
  原样交出整行。于是一条从别的门直写进来的 `invalid_at: 0`(装一份世界文件、
  维护脚本直写 `RedisRows` 都能造出它),审计视图说"它在第 0 tick 作废"——
  一个不可能的值,它 `valid_from=100` 才立起来 —— 而 `query(as_of=100)` 说
  "那一刻它还立着"。**同一份数据、两个门、两个答案,而没有一个门会报错。**
  现在夹这一下挪到读的入口、两个视图共用(`_edge_row_view`)。
  ⚠️ **只改读出来的那份拷贝,不回写**:读一次顺手修一次库,等于给演化态多一个
  日志之外的写入者,而库里那个坏值本身是"它从别的门进来"的证据。
  ⚠️ 上一轮这一条(以及 `docs/REFERENCE.md` §2.9.9、`_edge_row_view` 的注释)写着
  "**审计视图恰好是导出包与维护脚本读的那一格**" —— **那句是假的**,2026-08-19
  对账查出。导出走 `world_package.dump_world_records`,遍历 Redis 键原样吐类型化的
  字节,**一次 `query()` 都不调**;引擎里读 `include_invalid=True` 的只有
  `forget_player` / `erase_player`,而它们只看 `subject` / `object` ——
  审计视图今天没有读 `invalid_at` 的消费方,这条闸是**提前立的**。
  真实的后果因此是:**导出包里照旧躺着 `invalid_at: 0` 的原始字节**,装回去之后
  在读的入口被夹(和"不回写"自洽,是这条设计的必然结果,不是漏)。三处措辞已改,
  并加了一条钉住这个形状的测试
  (`tests/test_relation_edges.py::test_导出包里躺的是原始字节_夹在读的入口`)——
  REFERENCE 是别的仓库照着写代码的那一份,它上面的每句话都该有人验。
- **`_apply_memory_trigger` 的 `memory_seed` 那一支自己兜了一次出处默认值**
  (`payload.get("provenance") or self._provenance_of(kind)`),而两个 store 的
  `add()` 早就在做同一件事。同一条规则在两处各写一遍就是给它们分叉留位置,
  而分叉的那天没有一处会报错 —— 3.3.0 说的"写侧只有一处真相"因此略微夸大。
  连 `Scheduler._provenance_of()` 这个转发口一起删掉(留着它就等于留一个随时可以
  再兜一次的入口);kind → 出处的表仍在 `memory_store.PROVENANCE_BY_KIND`,
  `Scheduler.PROVENANCE_BY_KIND` 照旧引用它。

### Changed

- **`seed.location_image_write_command` 从 `null` 变成 `"location set-image"`,
  `location_image_write_gloss` 跟着重写。** 这一格是**消费方据以决定那个按钮画不画**
  的地方(创作台的铁律是"问引擎不读文档"),所以它必须跟着门走:一个还报着 `null`
  的契约会让下游继续把补图那个按钮关着,而库里明明有了 —— **库里有而对方看不见
  等于没有**,这是本仓的回执纪律原话。作者层那条路**一个字没变**(仍然是
  "只填缺、不覆盖",已在册的地点照旧整条跳过并逐个点名),契约里也把这句说了出来。
- **`demo.cyberworld` 封皮上的 `engine_min` / `source_engine_version` 3.3.0 → 3.4.0。**
  升 `__version__` 就要顺手改这两个数,这是有意的成本(`test_flagship_seed.py` 那条
  等号钉着,理由写在它的 docstring 里:橱窗产生过的全部证据只来自它自己那个引擎,
  写成任何更低的数字都是一句没人验过的话,而它偏偏是下游唯一读得到的那句)。
- **`media._clip` 改名成公开的 `media.clip_uri`。** 报错不再是唯一会把一条 URI
  印给人看的地方 —— `location set-image` 的人类回执要印"从哪一条换成了哪一条",
  而一条 256 KiB 的 `data:` 原样吐出去会把终端刷没。两处各写一份 `text[:120]` 的话,
  下一次调这个数只会调其中一处。
- **那两句"图没装进去"的警告现在指得出解法**(`redis_state._warn_skipped_location_media`
  与 `__main__._edit_location_media_warnings`)。它们上一版的结尾是"眼下只能重建
  那个世界" —— 那句当时是真的,现在不是了。**一句把人留在原地的警告,和没有警告
  差不多**,而这两句的收件人恰恰是唯一会去补图的那个人。
- 🔴 **「换图再装回去」这一种,从前一个字都不说**(`_warn_skipped_location_media`)。
  它只在"文件有图、库里那格是空的"时点名,而**两边都有、值不同**(= 作者把图换了)
  安静跳过、退出码 0。上一版给的理由是"两边都写了按「只增不改」本来就该以世界为准,
  那不是错" —— **那句话不成立**:作者刚刚编辑了那份文件、把 URL 换成新的一条,
  而世界一声不吭地留着旧图。「按语义本该如此」和「他要的事没发生」是两回事,
  **只有前者被说出来的时候,后者就是一次静默失败**。而换图恰恰是**最常见**的那种
  编辑:图床是内容寻址的,换一张图就是换一条 URL。现在两种都点名(分开说"要补的"
  和"要换的"),并指向写门;两边一模一样时照旧闭嘴 —— 一句总在响的警告等于没有警告。
- **`erase_player(dry_run=True)` 的代价现在写在门上**(docstring + `docs/REFERENCE.md`
  §2.9.10.1 / §3 那张表 / §4.2.3.2)。此前它一个字没说,而它读起来完全像一次查询 ——
  "不带 `--yes` 只数"、"一个字节都不写"、"幂等"。于是下游照着"它只是数一下"建了那扇门:
  2026-08-19 玩家读一次抹除清单,一个 21 角色、13.9 万事件的世界被打停 80 秒。
  现在那三处都写着同一句:**`dry_run=True` 是 O(全量事件) 的两遍全表扫描,绝不许在
  event loop / tick 线程上同步调。** 两遍是语义上必需的 —— 名字要先收齐才知道拿什么
  去比对,而后一遍必须看每一条(他的名字可能出现在任何一条事件的正文里);
  **"抹干净"和"只看前一页"互斥,所以没有 `limit` 可以给。**
  和 `player_engagement` 那句 ⚠️ 同一族但贵一个量级:**那一条是一次 SELECT,
  这一条是整本账。** 把它挪出请求线程是宿主的活,**而把代价说出来是引擎的责任** ——
  一个下游读得到的公开出口,它的复杂度不写出来,就等于默认它是廉价的。
  ⚠️ **这条曲线的长期解是给事件按 `player_id` 建一条索引**,让抹除只碰涉他的那几条。
  它动 `storage` 契约、要给老世界回填、持镜像的仓库要跟,所以不在这一轮里 ——
  记在这里是为了下个月的人不必重新发现一遍:**常数项砍得再多它还是 O(全量事件),
  十几万是一个被正常玩的世界两天的量,一个月就是两百万条。**
- **`erase_player` 的两遍扫描各瘦了一圈**(常数项,`anima_world/api.py`)。
  ⚠️ **这一条不是修复,不许当墙的替代品** —— `erase_player` 仍然是 O(全量事件),
  上面那句"绝不许在 event loop / tick 线程上同步调"一个字都没松。
  **两遍合不成一遍**:名字要先收齐才知道拿什么去比对,而名字可能出现在收到它
  **之前**的任何一条事件里。能省的只有常数,省了三处:
  ① 第一遍已经对每一条判过"涉不涉他"了,第二遍直接查那个集合 ——
  **但只查到第一遍看见过的最后一条为止**(`scanned_through`),比它新的照旧现判:
  `forget_player` 自己就在两遍中间追加一条 `player_departed`,别的进程也可能
  刚好落一条,无脑复用就是把抹除的边界往回缩(`tests/test_erase_player.py::test_两遍之间落库的那几条照旧现判`,
  改成无条件复用当场红);② `dry_run` 只要一个 bool,不再为它把每一层 dict/list
  重建一遍(新 `_erase_probe`,第一处命中就返回)—— 于是预览和真跑成了**两条
  代码路径**,`test_预览走的是另一条路_数出来的却是同一批` 盯着它们数出同一批
  (三条事件各只有一处能改、各藏在不同的嵌套形状里:混在一条里查不出来,
  一条事件只算一次,漏掉的那层会被同一条上别的命中掩护过去);
  ③ 什么都改不动时连日志都不用从头翻。
  预览 2 万条 177 ms → **也就是说墙那侧仍然要有墙**:今天的 13.9 万是
  "一个被正常玩的世界两天的量",一个月就是两百万条。**下个月同一条链会再来一次,
  而那时没人记得**。
- **规律引擎的压测闸从墙钟换成计数**(`tests/test_world_rules.py`)。这一层的承诺
  ("一万棵树也扛得住")本来就是一个计数:`snapshot_kind` 每类一次、`write_round`
  整轮一次。现在测的正是它 —— **1000 棵树和 10 棵树打给存储的往返次数逐格相同**
  (`{'snapshot_kind': 10, 'write_round': 10, 'of': 370, 'get': 15}`),而求值次数
  该涨的照涨(100 → 10000)。上一轮只把假阈值 3.0s 放宽到 30s 止了血
  (CI 上实测就是 3.0s,3.3.0 定版那次推送 3.12 以 `assert 3.0219… < 3.0` 红了,
  而那次只改了一个版本号字符串和几份文档);但一把会因为快了 0.7% 而绿的尺子,
  量的从来不是它说要量的那件事,而它坐在 `release.yml` 的 `verify` job 上。
  计数是确定的、与机器无关的,墙钟只是它的影子。

### 文档(裁决落账,**代码一行未动**)

- **图片层的跨仓库裁决**(`docs/FOR-STUDIO.md` §3.22 + 总图契约表)。「地点、背景、
  人物都有图」按判定程序拆成三件:那**一格**归引擎,图片的**家**归网站(新契约
  `/media/` 图床,**权威不在本仓** —— 继 claim HMAC 之后第二条),图的**来源**归创作台。
  引擎欠的形状当时定了但没写:`locations[]` 的可选图(落点五处,漏一处就静默丢)、
  `portrait` 的 `data:` **字节上限**、`world check --json` 的外链清单两段。
  ✅ **三样都在同一个 Unreleased 里交付了**(见上面 `### Added`),而**那时留白的
  两件产品向由老板拍了**:「背景」不是第三样东西(所以 manifest 与 `CARD_KEYS` 照旧
  一个字没动),地点的图是**两格**不是一格 —— 于是那个占位的键名 `image` 从没存在过,
  真名是 `map_image` / `scene_image`。
  三条裁决理由记在 FOR-STUDIO,因为它们以后还会被问起:**不禁 `data:`**
  (它是格式自足的唯一实现,收紧作者层 schema = 老世界开不了机,而拿契约当政策工具
  今天砸一次明天还要再砸一次)——**真正的病是"没有上限"**,而
  「进得了提示词的必须有界」在这里的对偶是**进得了读出口的必须有界**,上限按它走
  `roster`(按需)还是 `state()`(网站每几秒轮询)分别定;**存量先查再定** error 还是
  warning,不拍脑袋;**"包发出去图就没了"是产品选外链要付的代价,不是格式的缺口**
  (那条路一直在,它叫 `data:` URI),引擎该做的是让代价看得见,不是补格式。
  两件产品向的还没定(「背景」是不是第三样东西、地点图一格还是两格),
  **在它们定下来之前不动 manifest、不动 `CARD_KEYS`**。

### 测试

- **真 MySQL 上的漂移次序,从推断变成实证**(`tests/test_drift.py`)。三条新 drift
  测试此前只跑 fakeredis,而两个后端倒序的方式不一样(Redis 版 `rows.reverse()`,
  MySQL 版 `ORDER BY id DESC`)——"补的两个排序键对两边同样成立"是一句推断。
  起一个一次性 `mysql:8.4` 容器验过:三种形状(同秒批量落库 / 跨秒 / 按 `player_id`
  筛)在真 MySQL 上全绿,而把排序键退回只按 `created_at` 时**这条当场红**。
  同一天有两笔账正是替身掩护真路:`MySQLChatStore.__slots__` 那条 bug 在 store 级
  互验里全绿、真 MySQL 上一开就炸;网站那侧在真 MySQL 上又逮到三条。
- **怎么连真 MySQL 只此一处**(新 `tests/_realmysql.py`:`ANIMA_TEST_MYSQL` 那道门、
  `connect()`、`drop_world_tables()`)。抄第二份 `_connect()` 出去的两份猜测,
  **只在有 MySQL 的机器上才会同时跑** —— 也就是最不常跑的那种机器上。
  连带一条踩出来的:`DROP TABLE` 要元数据锁,而 MySQL 的默认 `lock_wait_timeout`
  是 31536000 秒(**一年**),而引擎的 `ThreadLocalConnection` 按设计只在
  `close()` 时关**本线程**那条 —— 用完就删表会挂住不动,而挂一年和挂死没有区别。
  所以 `drop_world_tables()` 先把等待压到 5 秒(挂改成报错),而清表放在**开世界之前**。
- `tests/test_relation_edges.py` 两处断言补上界:兜底那一刻钉 `== 201` 而不是
  `> 200`(`> 200` 对一段凭空编出来的有效期一样绿),并配上 `as_of=201 == []`;
  直写坏区间那条从头验到尾 —— 此刻 / 那一刻 / 审计视图三个答案自洽,
  而库里的字节一个都不许被读操作改掉。

## [3.3.0] —— 我们对外声称"可测"的那几格,这一轮才真的量得准 (2026-08-19)

**次版本**,判据逐条复核过:新增的全是公开方法与 CLI 出口(`drift` / `engagement` /
`player erase`);`world_file.py`、`world_package.py`、`world_seed.py` 自 3.2.0 起**一行没动**,
`PACKAGE_FORMAT_VERSION` 仍是 3,`contract --json` 的 `storage` 段逐格未变(`key_prefix`、
四张 `mysql_tables`、`volatile_keys`、`presence` 全同)。唯一的存储形状变动是
`memories.provenance` 一列,走 `ADD COLUMN … NOT NULL DEFAULT 'experienced'` 的加法式迁移 ——
按本仓开头那条规则,加法式修订属于次版本。

⚠️ **发版前必须让人看见的一条:`edges` 的语义在同一个版本号上换过。** 见下面 Changed。
`drop()` 从删行改成写 `invalid_at`,而 **3.2.0 的构建不认识这一格** ——
它把作废的边当成有效:绝交过的关系在 `cliques()` 里复活、在提示词里复活,
而三方的日志全是干净的,退出码全是 0。按本仓的定版纪律这不到主版本(世界照样挂得上,
不是"读不了"),所以它不会有版本号替你喊一声 —— **同时跑着 3.2.0 和 3.3.0 的部署,
两个引擎会对同一份 `edges` 给出不同的社交图**。要么两侧一起换,要么接受旧的那侧看到
一个多出了几段死关系的世界。

⚠️ **PyPI 上一个版本是 1.4.0,所以这一版对装包的人是从 1.4 一步跨到 3.3。**
2.0.0–3.2.0 从来没发上索引(v3.0.0 那次 Release 死在冒烟步骤上,原因见下面 Fixed)。
跨过来的破坏性变更是 2.0 那一批,不是这一批:`World.open(world_id, redis=…)`
(world.db 整体退役,世界住 Redis)、CLI 的 `--db-path` → `--redis` + `--world-id`、
`--seed` → `--world-file`、许可从 Apache-2.0 换成 **AGPL-3.0-or-later**。
详情各见 2.0.0 那一节。

这一版并了五批(新的在前)—— 头一批是发版这条路本身;中间两批各是一次真人试玩之后的
收尾,共同点是**把设计主张变成可测的东西**,以及**引擎的返回值有两个读者**:她,和
一个人;末两批是它们过独立验收时被打回的那些。

### 定版:发版这条路自己坏了七个月,而它坏的方式和我们一直在修的那些一模一样(2026-08-19)

#### Fixed

- 🔴 **`release.yml` 的冒烟步骤还在用 1.x 的签名,于是 2.x/3.x 一次都没发上 PyPI。**
  `build` 那一步写的是 `World.open('rel.db', force_mock_llm=True)` —— v3.0.0 那次
  Release(run `31467060331`)就死在这一行上:
  `TypeError: World.open() missing 1 required keyword-only argument: 'redis'`。
  往后每一版都会死在同一行,因为**没人会去改一条从没跑绿过的路**。
  `smoke` 那一步同病,还多两处:`World.open('smoke.db', …)` 与
  `anima-world simulate --db-path /tmp/smoke2.db`(2.0 就改成 `--redis` + `--world-id` 了)。
  改成 3.x 的形状:两个 job 各起一个 `services: redis`(和 `ci.yml` 的 `package` job
  **逐字同一个形状** —— 那个 job 每次推 main 都跑绿,所以它是这个仓库里唯一被真 runner
  证过的写法),`World.open(world_id, redis=…, force_mock_llm=True)` 且
  **`decode_responses=True`**(裸 bytes 客户端下量名变成 `b'树高'`,规律静默失配),
  CLI 那半改 `--redis` + `--world-id` 并补一条 `contract --json`。
  ⚠️ **为什么不拿 fakeredis 顶**:它是 dev extra,索引上装回来的那个 wheel 里没有它;
  更要紧的是**它喂不进 CLI**(CLI 只收 URL),于是这一步会安静地缩成一次 import ——
  而"从索引装回来的那个 wheel 真的跑得动"正是这个 job 存在的全部理由。
  ⚠️ **这一条的形状和这一版修的那些是同一个**:照跑、日志干净、每一格读数都对 ——
  `ci.yml` 一路绿、README 上的 PyPI 徽章一直亮着,而**徽章指着的那个索引停在 1.4.0**。
  许可从 2.0 起是 AGPL-3.0-or-later("用了必须开源,含网络服务"),而**索引上那个 1.4.0
  是 Apache-2.0** —— 换许可的那一版从没发出去,所以此刻 PyPI 上一个 AGPL 的 anima-world
  都没有。**对外承诺与实际发布物脱节了七个月,而没有任何一处会报错**:
  一条测不到自己的发布管线,和一把把上升报成下降的尺子是同一类东西。
- 🟠 **压测那条闸挡的是 runner 的抖动,不是退化 —— 而它坐在发版必经的 `verify` job 上。**
  `test_the_engine_scales_to_many_entities` 的阈值写的是 3.0s,而 GitHub 共享 runner 上
  这段实测就是 3.0s。定版这次推送(run `32229122851`)当场演示了:3.11 / 3.13 过,
  **3.12 以 `assert 3.0219326039999714 < 3.0` 红**,而那次改动只动了一个版本号字符串
  和几份文档 —— **快 0.7% 就绿、慢 0.7% 就红**。它的 docstring 自己写着"阈值取得很松
  (比实测宽一个数量级)……要在别人的机器和 CI 上也稳",所以这不是要不要放宽的取舍,
  是**那句话本来就是假的**。改成 30s:本机约 1.3s、CI 约 3.0s,之上留一个数量级,
  而"逐个 commit"那一版(约 66 倍 ≈ 200s)照样挡得住,两侧各差一个数量级才是原意。
  ⚠️ **为什么值一条而不是悄悄改个数**:这条闸在 `release.yml` 的 `verify` job 上,
  也就是说**发版那一下有一定概率被自己的压测掀翻**,而 PyPI 那边"第一次尝试就是唯一
  的一次尝试"。一条会随机拦住发版的闸,和一条从不检查自己的管线是同一笔账的两面。
  🔻 **欠着的更好的做法**:这一层的承诺本来是个**计数** —— `snapshot_kind` 每类一次、
  `write_round` 整轮一次(`rules.py` 第 2 条)。数调用次数确定、与机器无关,墙钟只是
  它的影子。没顺手换成计数是因为这一版正在收尾发版,改测试机制的风险不该压在那一下上。
- 🟠 **四份文档都把"Apache 到哪一版为止"写错了一版。** README、CLAUDE.md、FOR-STUDIO
  与 2.0.0 那节 CHANGELOG 都写"1.3.0 及以前是 Apache-2.0",而 **`v1.4.0` 也是 Apache-2.0
  并且真的上了 PyPI**(2026-08-04,那次 Release 跑成功了)—— 换许可是 2.0 做的,
  而 2.0 从没发出去。查出来的方式很笨也很有效:`git show v1.4.0:pyproject.toml`
  说 `license = "Apache-2.0"`。**许可这种事不能靠约等于**,尤其是当"错的那一版"恰好
  就是此刻用户 `pip install anima-world` 装到的那一版:照文档以为自己拿的是 AGPL
  的下游,拿到的其实是 Apache 的。四处都改成"到 1.4.0 为止",2.0.0 那节留一条更正
  而不是改掉原句(发布历史不该被悄悄重写)。
- 顺带清掉同一个文件里三处已经变成假话的注释:tag 校验那步说"一次发版冻结 db format"
  (world.db 2.0 整体退役了,冻的是存储形状 + 包格式)、`--extra-index-url` 那条说
  TestPyPI 上缺 `cryptography`(Fernet 随 SQLite 一起退役,现在三个运行时依赖是
  redis / openai / httpx)、重启那条断言的说明还在讲"证明文件写下去了"
  (键前缀时代它证的是另一件更要紧的事:重开一个活着的世界是**只填缺**,不是拿创世
  快照盖回去)。

⚠️ **这一步没法在本机验完 —— 它要真跑一次 Actions,所以这里不声称验过。**
本机走到的地方:`python -m build` → `anima_world-3.3.0-py3-none-any.whl`(`twine check` 双双
PASSED)→ 装进一个干净 venv(`import anima_world` 报 3.3.0)→ **两个 job 的冒烟脚本逐字
跑过那个 wheel + 一台真 Redis,退出码 0**(`build` 那段答"wheel works, clock persists",
`smoke` 那段答"ran a world: 3.3.0"),`anima-world simulate --redis … --world-id … --ticks 20
--llm mock`、`--help`、`contract --json`(`engine_version: 3.3.0`)逐条敲过;
每个 `run:` 块过 `bash -n`,整份 YAML 解析过并确认两个 job 都挂上了 redis service。
**没验的只剩两样**:GitHub Actions `services:` 那条 localhost 网络(由 `ci.yml` 的
`package` job 一路绿背书 —— 同一个写法),以及 TestPyPI 上传再装回来那一段。
所以顺序仍然是**先看一次 CI 绿,再打 tag**:`v*` 一推就是发版,而 PyPI 拒绝重复上传
同一个版本号 —— 第一次尝试就是唯一的一次尝试。

### 收尾之二:一把在最常见的输入上把上升报成下降的尺子(2026-08-19)

上一批过完复核之后,复核的人在**敲命令**时逮到了一条更严重的:它不在新代码里,
在把新代码接上世界的那一步。三条的共同形状仍是那一个 —— **照跑、退出码对、
日志干净**,而三条又各自是同一句纪律的第三、四、五次:**同一条判断抄两份,
就是给它们分叉留位置。**

#### Fixed

- 🔴 **`anima-world drift` 在同一秒落库的转录上把结论整个读反了。** `persona_drift`
  只按 `created_at` 排序,而那是墙钟的**秒** —— 定不了同一秒里的先后;而两个后端的
  `list_conversations` 都是**倒序**给的(Redis `rows.reverse()`、MySQL `ORDER BY id DESC`),
  稳定排序于是原样保留了倒序。实测同一段"先不迎合 6 条 → 后极度迎合 12 条":
  纯函数 `drift.analyze` 答 `rising=True`(基线 −5.556 → 最近 10.714),真 CLI 答
  「迎合度 基线 10.714 最近 2.579 **下降**」、`sycophancy.rising=False`,**退出码照样 0**。
  跨秒的转录不受影响 —— 但 **CI 里喂一段转录进去正是同秒批量落库这个形状**,而
  REFERENCE 承诺的就是"它能进 CI"。
  ⚠️ **为什么这条比一般的排序 bug 重**:这把尺子坐在《拟人化互动办法》第八条(五)
  「不得过度迎合用户」那一格上,是我们对外声称"可测"的那个判据。**一把在最常见的
  输入形状下把上升报成下降的尺子,比没有尺子更坏** —— 没有尺子时人知道自己不知道。
  次序的键改成 `(created_at, 会话 id, 同一场里的座次)`,后两个都单调。
- 🟠 **关系边"作废于一个说不出的时刻"仍然读成"从来没有过"**,只是从 0 挪到了
  `valid_from`。上一批给 `drop(at=None)` 的兜底是落在 `valid_from` 上 —— 而有效区间是
  半开的 `[valid_from, invalid_at)`,零长区间在 `query(as_of=…)` 上**任何一刻**都答 `[]`,
  和 `invalid_at=0` 逐位同一个读数。而"从来没有过"是 `hard=True` 的意思,两件事必须
  分得开。兜底能说的最小的一句真话是**她至少在成立的那一刻是成立的**,所以下界是
  `valid_from + 1`。
  并且**这道闸从写侧挪成读写共用一个函数**(`redis_state._edge_invalid_at`):只装在
  `drop()` 里挡不住别的门 —— 装一份世界文件、维护脚本直写行,照样能把一个不可能的
  区间落盘(实测),而读的那一侧会照单全收。
- 🟠 **写侧还剩第三处"抄一份默认值"**:两个后端 `add()` 的 `provenance` 默认参数都硬写
  `"experienced"`,不走 `provenance_of()`。于是 `store.add(kind="reaction")` 落下的**新**行
  读作亲历,而同一个 kind 的**老**行(读侧按 kind 补)读作听说 —— 同一格数据两个答案,
  只差在这一行是什么时候写的,而两边都不报错。两个后端一致所以不是后端分叉,但它和
  上一批修准入闸阈值时用的是同一条判断。`MemoryDescriptor.provenance` 一并改成 `None`
  (= 触发器没说):它原先是 `"experienced"`,一个**真值**,于是 `Scheduler` 那句
  `descriptor.provenance or self._provenance_of(kind)` 一次都没有生效过。

#### Tests

`test_drift.py` 加三条**真门**的(同秒批量落库方向必须对 / 跨秒的也钉住 /
`player_id=` 筛出来的那一段同样不许反);`test_relation_edges.py` 加一条"从别的门
进来的坏区间读侧也认得",并改写了 `test_an_unnamed_moment_never_lands_before_the_edge_stood_up`
—— ⚠️ **它原先钉的正是这个 bug**(`invalid_at == valid_from`),现在钉的是它自己
说明里描述的那个性质(成立的那一刻仍答得出来);`test_memory_provenance.py` 加三条写侧的。
`test_memory2.py` 那条反思测试改用 `bare_seed` 夹具,不再就地关开关 ——
CLAUDE.md 里点名的就是这个夹具,而"每多点亮一个开关就多关一行"漏掉的那一行不会报错。

---

### 收尾:三个 reviewer 打回的八件(2026-08-19)

下面那两批交完之后过了一遍独立验收(正确性 / 契约与回执链 / 可用性 三个视角)。
**没测试的那批代码里逮出五条硬的**,共同点还是那一个:**照跑、日志干净、读数都对**。
记在这里而不是悄悄改掉,是因为每一条都值一条纪律 —— 尤其**"抄一份默认值"与
"改了行为却不改文档"这两种坏法各出现了一次**。

#### Fixed

- **记忆准入闸的阈值有两份真相,生效的是坏的那份。** 算分那一侧校准成 0.20、
  REFERENCE 写着 0.20,而 `config_store._DEFAULTS` 里抄着一份 **0.35** ——
  而 `ConfigStore.get` 的 `default=` **只在这个键没声明过时才轮得到**,于是调用方写的
  `default=DEFAULT_THRESHOLD` 永远不生效。后果不是算错:一条 `state_change` 的满分
  就是 0.35(importance 0.5 × 类型先验 0.7),窗口里有一条同类就掉到 0.333 ——
  **照文档开闸的世界静默丢掉正常的、和已有记忆零重合的新记忆**。
  修法不是把 0.35 改成 0.20,是让配置声明 **import** 算分那侧的常量:抄一份默认值
  就是给"两份真相"留位置。
- **关系边"作废"的时刻恒为 0。** 唯一的生产调用方(关系反转那一处)调 `drop()` 不传
  `at=`,于是 `invalid_at` 落成默认的 0 —— **比 `valid_from` 还早**。那样一行读出来是
  "这段关系从来没有成立过":`query(as_of=他俩正是朋友的那一刻)` 答 `[]`,恰好是这一格
  (R3 有效期)存在理由的反面,而且一条日志都不报。传上那一刻,并在存储层加一道兜底:
  **`invalid_at` 永不早于 `valid_from`**(没给就落在 `valid_from` 上,给了个更早的往回夹
  并 warning 一声)。
- **夜间固化把"攒够了才反思"改成了"每天每人都反思",并把没攒够的水位清成 0。**
  门写的是"水位 > 0"(= 今天写过任何一条记忆),而**橱窗世界点亮了这个开关** ——
  于是每个角色每世界日一趟 LLM;更坏的是攒了半天没到阈值的水位被抹掉,
  `memory.reflection_threshold` 那条路在开着固化的世界里**等于被停掉**。
  两个机制,一个安静地废掉另一个,两边读数都对。改成:门就是那条阈值,而固化
  **接管反思的时刻**(白天只攒不发,越过阈值的那一次留到夜里,一晚最多一次)——
  "别跟她正在说的话抢线程"正是 R5 存在的理由本身,所以这是它的内容,不是副作用。
- **`player erase` 的回执从第三次起不幂等**(数据是对的,坏的是回执)。
  `forget_player` 每跑一次都追加一条 `player_departed`,而联系态已经清空之后名字问不
  出来、`player_name` 兜底成 `player_id`;下一轮扫描把这个 id 当成他的一个显示名收进来,
  于是 **`names_skipped` 永远是 1**。id 本来就绝不该被替换(不透明 id 保留那一条),
  所以它和占位符「(已注销)」一样在收名字那一步就该滤掉。
  ⚠️ **教训是验的方式**:上一轮报的是"再跑一遍各格全 0,幂等成立" —— 只跑了第 2 次。
  测试现在跑到第五次。
- **`anima-world prompt` 不解释 `persona.anchor` 块的缺席。** 那张"哪块没出现、
  为什么"的表只覆盖了 `stance` / `tools`,于是关掉开关之后新加的那一块**凭空消失、
  零解释** —— 而这份视图的全部职责就是解释缺席。开关管着的块现在逐个都在表里。
- **`provenance` 两个后端形状不一致,老行还一律读成「亲历」。** MySQL 读侧归一、
  Redis 读侧不归一(`World.memories()` 的老行连这一格都没有);而"一律亲历"把老的
  `kind='reaction'`(八卦传过来的)报成她亲眼所见 —— **正是这一格要治的那个病本身**。
  kind → 出处的表搬到 `memory_store.PROVENANCE_BY_KIND` / `provenance_of()`,两个后端
  的读侧和引擎写新行时走同一个函数。
  ⚠️ **说清楚它现在到什么程度**:出处**只是被存下来、被报出来,还没有消费端** ——
  提示词那一层不按它换语气,所以"她把传闻当亲眼所见讲出去"这一版**没有兑现**。
  先存是有意的(丢了之后再多的检索精度也救不回来),语气那一刀随时补得上。
- **三处读数上的小谎**:准入闸的 if/else 两支写同一行(`state()` 上看不出拒过没有,
  现在多两格:`refused` 与 `last_refusal`);`consolidate_memories` 少了老代码有的
  `hasattr(store, "decay_pass")` 判(缺该方法的后端每角色一条 warning,还连带把清扫与
  反思一起跳过);`reflections` 在 `_submit_reflection` 空转时照加(没接反思器时报
  "反思了 3 次"——`rule_stats()` 那条纪律要防的就是这个)。
- **`drift --json` 的 `threshold` 一格只在 `ok=true` 时出现。** `--json` 是契约,而一个键
  随分支来去的契约把每个消费方逼成一串 `.get()`、再逼出各自的一份默认值 —— 那就是
  镜像端开始猜的地方。三条出口现在形状一致。

#### Changed

- 🔴 ⚠️ **`edges` 的语义在一个已经在线上跑着的版本(3.2.0)上变了 —— 这是 3.3.0 这次
  发版最需要被看见的一条,请先读完它再升。**
  `drop()` 从删行改成写 `invalid_at`、`query()` 默认过滤作废的边。
  **旧的 3.2.0 构建根本不认识 `invalid_at`**:它读新引擎写下 / 导出的 `edges` 时,
  把**作废的边一律当成有效** —— 绝交过的关系在 `cliques()` 里复活、在她的提示词里复活,
  于是她会继续把一个已经和她断了的人当朋友讲话。
  **三方的日志全是干净的,退出码全是 0,`edges` 的行数看上去也完全正常**(作废是写一格,
  不是少一行)—— 没有任何一处会告诉你读数错了。
  这是**"同一个版本号两套行为"**,不是加法式变更那种"老引擎只是读不到新表",
  所以它也不会被 `contract --json` 或包格式版本挡下来:**版本号不会替你喊这一声。**
  按本仓的定版纪律它仍不到主版本(世界照样挂得上,不是"读不了"),而这恰恰是它危险的
  地方 —— 一次读起来无害的次版本升级。
  **要做的两件**:(1)同一份 Redis / 同一批包,**两侧引擎一起换**,别让 3.2.0 和 3.3.0
  同时读同一个世界;(2)已经在 3.2.0 上跑过"绝交"的世界,那些边**当时是被删掉的**,
  升上来之后不会凭空冒出来 —— 这个方向是安全的,坏的只有"新引擎写、旧引擎读"。
- 文档跟上四处:REFERENCE 橱窗清单里"`chat.loop.enabled` 是唯一没点亮的"**已经是假话**
  (准入闸也没点,而且是有意的,理由不同,现在是一张两行的表);新增 **§2.9.12.1**
  人设尾部重注 —— 这个特性此前**全仓只在配置表里存在**,而那张表的交叉引用指向的
  §2.9.12 通篇讲漂移探针、一个字没提它;`drift --baseline` 的**上界**(样本的一半)
  写进参数表;FOR-STUDIO 记一笔"点了夜间固化就等于定了反思的节律"(它决定作者的
  世界花多少钱)。

#### Tests

上一轮欠的那批测试补了一半:`test_memory_admission.py`(阈值三处对齐 + 全新事件收得下
+ 复读照样拒 + 锚定永不拒)、`test_memory_consolidation.py`(攒不够不反思且水位留着 /
阈值在夜里兑现 / 一晚一次 / 空转不计数 / 缺 decay_pass 照样清扫反思 / 日切不衰减两遍 /
关着开关逐位如旧)、`test_memory_provenance.py`(老行按 kind 补,**两个后端给同一个
答案** —— 而且不需要一台 MySQL:归一发生在纯函数里)、`test_drift.py`(三条出口形状
一致 / 基线上界 / 迎合度那一格)、`test_relation_edges.py` 加三条(作废之后 `as_of=`
仍答得出那时候 / 不许写一个早于 `valid_from` 的时刻 / 和好让同一条边复活)、
`test_erase_player.py` 的幂等跑到第五次。

---

### 合规、记忆、以及"她还是不是她"

《人工智能拟人化互动服务管理暂行办法》2026-07-15 施行,和一轮记忆/人设漂移的技术
盘点撞在一起。这一轮的共同点:**把设计主张变成可测的东西**。"我们不谄媚"和"我们
人设稳"在有尺子之前,和"代码没 bug"是同一句话。

#### Added

- **法务抹除**(`World.erase_player()` / `anima-world player erase`,REFERENCE
  §2.9.10.1 / §4.2.3.2)。第十六条给用户的删除权,和 `forget_player`(告别,历史一个
  字不动)是两个动作。形状被这个引擎的地基钉死:**事件不删行,原地改写** ——
  `seq` 在 Redis 后端就是列表下标,删一行后面每条都错位,而且不报错。
  - **不透明 id 保留,不换假名。** 换假名是第一版设计,被跨进程折叠否决:落后的进程
    折了真名 delta、再折到假名事件,那段真名关系就成了没人清得掉的幽灵;而假名映射
    一旦落库(哪怕落在事件里)就等于没抹。**代价是对宿主提了一条要求:`player_id`
    必须是不透明 id。**
  - 转录**整场删**(一场对话里她说的那半也是对他说的,单删他那半留下的是一段自言
    自语);记忆**按出处删行**、**旁及他的只换名字**(别人的反思提了他一句,删整行
    等于把别人的记忆也抹掉一角);**账本一个字不动**(守恒不许破)。
- **她还是不是她**(`World.persona_drift()` / `anima-world drift`,REFERENCE §2.9.12)。
  人设漂移的尺子:她说过的话按时间排开,拿她自己最早那几条当基线,后面走 CUSUM。
  **纯计数、不调模型**,所以可复现、能进 CI(漂了退 1)。七个特征里 `sycophancy`
  单独有一格 —— 它同时是**第八条(五)"不得过度迎合用户"的可测判据**。
- **他处得有多深**(`World.player_engagement()` / `anima-world engagement`,§2.9.10.2)。
  第十条依赖预警要的账,散在转录/投影/联系态三处,这里拢成一次调用。**给数不给结论**:
  阈值与干预是宿主的判断,而一个"依赖指数"会被做成进度条。
- **人设尾部重注**(`chat.persona_anchor.enabled`,默认关,橱窗点亮)。人设块坐在
  提示词开头,而注意力随窗口填满而衰减 —— 八轮内就测得出漂移(arxiv 2402.10962)。
  这一手不换模型、不加调用,只是把"她是谁"挪一份到她最后读到的地方。
- **记忆分型**(`provenance`,常开)。`experienced` 亲历 / `heard` 听说 / `believed`
  她自己想的。不分型的下场是她把一条听来的传闻当亲眼所见讲出去,而八卦每传一手就多
  一层失真 —— **出处丢了之后,再多的检索精度也救不回来。**
- **记忆准入闸**(`memory.admission.enabled`,默认关,**橱窗也没点**)。触发器答
  "这类事件配不配",这一层答"**这一条**配不配":第七次「在吗」不配。五因子**相乘**
  (任一到底就不收),锚定的永不拒,拒了 INFO 记一行说出为什么。
  - **`memory_seed` 不过闸** —— 那是世界的显式声明,而世界明说要记、引擎悄悄不记,
    比记一堆重复更坏(`test_verb_writes` 当场逮到)。
  - ⚠️ **橱窗没点亮它,是量出来的决定**:内置世界里的重复大多恰好走声明那条路,
    开闸之后 50 条记忆里"不同的事"从 10 件掉到 7 件 —— 闸清干净了唯一管得到的
    `state_change`,腾出的容量被管不到的重复填上。**拿一个实测更差的配置当新用户的
    第一屏,比不展示更坏。**
- **夜间固化**(`memory.consolidation.enabled`,默认关,橱窗点亮)。日切时衰减、清扫、
  反思。挂日切而不是每 tick:遗忘曲线按世界日算,而反思是一次 LLM 调用,跟白天的对话
  一起跑会抢线程。**这个世界自己有夜晚,不必假装。**
- **关系边的时间有效期**(常开)。`valid_from` / `invalid_at`:一段关系结束了是**又
  一件发生过的事**,不是"从来没有过"。`query()` 默认只给此刻有效的(承重:`cliques`
  读的就是它),`query(as_of=tick)` 答得出"那时候他俩是朋友吗",和好会让同一条边复活。
- **REFERENCE §1.1 触达边界**:引擎是**全拉模式**,hail/followup 只落 `inbox()`,
  宿主不拉用户就收不到 —— 于是办法里"向用户提示/提醒"的义务归属是清楚的,逐条列了表。

#### Fixed

- **`MySQLChatStore.__slots__` 少了 `content_filter`** —— **任何带 `mysql=` 的世界
  `World.open` 当场 AttributeError,开都开不起来**。而 MySQL 测试没有服务就整体 skip,
  所以这条在本机永远是绿的。起了一次性 MySQL 容器真跑才现形(又一例"替身掩护真路")。
- **漂移探针的零方差盲区**(自己实装当天用真世界演出来的):第一版拿基线标准差当尺度,
  标准差为 0 时直接返回 0。而**越稳定的角色基线标准差越接近 0,她恰恰是最该测得出
  漂移的那一个** —— 实测一个前六条回话一模一样的角色,迎合度从 0.000 涨到 15.441,
  探针输出「还是她」,退出码 0,表格排版整整齐齐。尺度改成取"标准差 / 均值的一个比例 /
  每个特征的绝对下限"里最大的一个。
- `tests/test_mysql_state.py` 里四处 2.0 之后就烂掉的引用(`anima_world.db`、
  已删的 `_rebind_chat_store`、旧的 `World.open(path, world_id=…)` 签名)——
  它们靠"没有 MySQL 就 skip"活着,一接真库全红。

#### Changed

- `ensure_schema()` 现在会做**加法式迁移**(`_ADDITIVE_COLUMNS`)。
  `CREATE TABLE IF NOT EXISTS` 对已经存在的表一个字都不改 —— 新列在老库上永远不出现,
  而代码照读、读到 None、静默走默认分支。只收加法(可空/带默认值的列)。

---

### 玩家那一侧看到的东西是写给她的

一轮真人试玩(恋爱陪伴产品,线上世界)之后的五件。共同点是**引擎的返回值只有一个
读者** —— 她 —— 而实际上有两个:她,和一个人。五件事各是那个缺口的一种形状。

#### Added

- **一段关系的人话**(`World.relationship_summary()` / `relationship_summaries()` /
  `anima-world relationship`,REFERENCE §2.9.11)。`state()["relations"]` 给的是四个
  -1~1 的浮点数,而恋爱陪伴产品里把它显示出来 = **把一段关系变成一根进度条**;
  不显示又更坏(玩家聊了两个小时不知道有没有发生过任何事,而世界里其实发生了)。
  数字一个字不动(还在 `axes` 里),加一句人话、一个粗档、和**上一次改变它的是哪
  一件事**。
  - **档走 `memory_triggers.band()` / `BAND_NAMES`** —— 和引擎自己认的是同一个函数。
    另写一份阈值表的下场是同一段关系在两个地方显示成两档,而两处都"照跑"。
  - **说得出出处**:`Relation.last_change` 是**折出来的**(`sentiment_delta` 落进投影
    时顺手写),不是每次去翻日志 —— 翻日志要从头扫,而这一层每渲染一帧问一次。为了
    让出处存在,`chat_session.close_conversation` 把 `conversation_id` 与那场的摘要
    传给 `Scheduler.submit_user_chat_judgment()`,worker 原样挂在落地的那条
    `sentiment_delta` 上(**不进判定的提示词** —— 模型判的是转录,不是对话编号)。
    **查不到就明说查不到**(`conversation_id: None` / `summary: ""`),不编。
  - 空关系是**没有来往**不是敌意:0 不是负数,报成「交恶」的话一个刚进来的新玩家
    开局就被讨厌。
- **玩家那一侧的能力表自带选项**(`World.player_options()` / `player_tools(player_id)` /
  `anima-world player options`,REFERENCE §2.9.6)。此前 `interact` 递给玩家的说明写着
  「你感觉到的那几行」(她提示词里的一个块)和 `tree:harbor_oak`(橱窗世界的 id),
  于是宿主只剩一条路:画一个文本框让人自己猜。现在 `target` / `verb` 两个参数带着
  这会儿点得动什么、每样叫什么、点不动是为什么。
  - **不新造第二套真相**:成不成走 `perform_affordance(dry_run=True)` —— 和真点下去
    那一次是同一个函数,只是在第一行写之前掉头。另写一份"看上去差不多"的判定是这一层
    最容易犯的错。
  - **四类拒绝一个都不合并**(`conditions` / `incapable` / `busy` / 讲不通的那摞):
    合成一句"现在不能",一个累坏了的人会挨扇窗点过去。
  - **一个字节都不写** —— 它每一帧都要被渲染一次。
  - `ToolSpec.player_description`:同一份声明两个读者,写给人的那一半单独写。
    写了它却不在 PLAYER 面上 = 注册时当场报错。
- **让世界跟一个走掉的人告别**(`World.forget_player()` / `anima-world player forget`,
  REFERENCE §2.9.10)。**这不是删数据的口子**:关系是 `sentiment_delta` 折出来的投影,
  手删那一行下一次重放会原样折回来 —— 世界照跑、日志干净,而"她还惦记着一个不存在
  的人"一天之内自己长回来。正确的形状是往日志里追加一条 `player_departed` **事实**,
  由折叠端和世界去响应它。**历史一个字不改**(事件、记忆、转录、账本全留着)。
- **角色卡:主次、一句话、立绘**(作者层 `agents[].card`、`World.roster()`、
  `anima-world roster`,REFERENCE §2.13 / §4.2.6)。线上那个世界 21 个角色,4 个是
  作者写了几周的主角、17 个是背景 NPC,而玩家的通讯录里这 21 个人**长得一模一样**。
  `anima_world/character_card.py` 早就写好了,而 `anima_world/` 里**没有一处 import 它** ——
  作者写得进、校验放行、世界跑得动、包也导得出,**就是到不了玩家眼前,全程零报错**。
  这一轮把它从头接到尾。
  - **`card` 是可选键,不进 `WORLD_SEED_AGENT_KEYS`**(那是**必填**集,
    `world_seed_errors` 拿它算 `missing`)。加进去等于要求每个世界给每个角色写一张卡,
    **每个老世界当场开不了机**。单列一格 `WORLD_SEED_AGENT_OPTIONAL_KEYS`,并进
    `contract --json` 的 `seed.agent_optional_keys` + 新的 `character_card` 段 ——
    创作台此前只能靠版本号猜"这支引擎带不带得动角色卡",而猜错不报错。
  - **闸挂在 `visibility_band_errors` 旁边,不进 `world_seed_errors`**:后者是跨仓库
    镜像的裁决面(只看必填键)。`world_card_errors` / `world_card_warnings` 由**同一批
    调用点**(加载期 `_load_world_file` 与 `validate world`)一起调,于是"validate 说
    没问题、开机却失败"不会发生。坏卡**一次列全**;不认识的键**只警告不拦**
    (创作台预告的第四样:声线、主题色、CV)。
  - **相对路径的立绘开不了机**:`.cyberworld` 是**分发物**,写 `portraits/a1.png` 而图
    留在作者笔记本上,包发出去就是一张断的图,而且不报错。
  - **卡不写黑板,走事件日志**(`agent_join.payload.spec.card`),读回来走投影 ——
    于是重启还在、导出再导入还在。`tagline` **绝不进提示词**:那是写给玩家看的广告词,
    混进人设她就会照着念。**三个写点全接**:创世(`_seed_initial_world`)、往已有世界
    补作者层(`_join_authored_additions`)、节拍脚本里中途 `agent_join`
    (`Scheduler._beat_agent_join` + `beats._validate_agent_bundle`)—— 只接第一处的话,
    后两条路上的卡照旧安静地掉在地上。
  - **`billing` 缺省 `supporting` 不是 `lead`**:猜错方向的代价不对称 —— 把主角说成
    配角只是排版难看,把还没出场的人说成主角是**剧透**。**没写卡的世界事件日志里
    一个 `card` 字段都不许多**:补一张 `supporting` 的话,宿主再也分不出"作者说他是
    背景"和"作者什么也没说"。
  - **`hidden` 的人引擎照出**:引擎是"这个世界里有谁"的权威,筛掉是宿主那一层的事 ——
    泄露的边界在进程上,不在浏览器里。顺序跟世界自己的名册走(事件日志序),不按字母重排。
  - `roster()` 与 `state()` **共用一个 `identity_rows_locked()`**:她此刻在哪只有一条
    规则(在场读黑板的现在,不在场读投影),两处各写一遍就迟早给出两个答案。
  - 内置橱窗一人一张卡(夏 `lead` / 柔 `supporting` / 遥 `hidden`)—— **做了却开箱
    看不见等于没做**。立绘只许 `https:`,`demo.cyberworld` 要留得住 review。
  - ⚠️ 顺带补上一个**已经在线上咬人**的洞:**显示名此前没有读出口**。`map --json` 里
    地点有 `name`,人只有 id,剩下唯一的路是重放整份事件日志去捞 `agent_join` ——
    而那是历史,不是现在。
- **一张卡在一个已经跑着的世界里改得动了**(`World.set_card()` /
  `anima-world agent set-card`,REFERENCE §2.13 / §4.2.7)。⚠️ **这是上一条的第二段,
  不是新特性**:上一条把卡从头接到尾,而三个写点全在 `agent_join` 上,**一个角色一辈子
  只 join 一次**。于是那一整轮**只对新世界成立** —— 线上那个已经有真人在玩的世界
  (20 个角色,4 主 16 背景)一张卡都补不进去,拿一份写好卡的世界文件重开也不行
  (作者层的合并语义是**只填缺不覆盖**,在册的人不会再收到 `agent_join`)。
  又一次:作者写得进、校验放行、测试全绿、包也导得出,**就是到不了玩家眼前,零报错**。
  - **它故意覆盖,和作者层正好相反,两边的 docstring 互相点名。** 作者层是"补一层"
    所以只填缺;这一条是**明示的编辑**。填缺语义下,一个已经是 `supporting` 的人
    **永远变不成 `lead`** —— 下一个人若把其中一条"修"成另一条,就把这个洞原样打回来,
    所以 `_join_spec` 与 `set_card` 的 docstring 各写了一句指着对方。
  - **部分更新往现有的卡上合并,不是整张替换**(只给 `--tagline` 不会抹掉 `billing`),
    **合并之后才校验**;抹掉**某一格**是把它给成空串(`--tagline ""`),抹掉**整张卡**
    才是 `--clear` —— 它**单独一格**,因为"这个世界没做过角色卡"和"这个人是背景"是
    两件事,前者宿主该回落到默认排版。给了 `--clear` 又给值当场拒绝:两句互相矛盾的
    指令,引擎挑哪句都是猜。
  - **一模一样等于没改:不写,并且当面说「没有变化」。** 事件溯源的世界里一条内容
    相同的 `persona_update` 只是噪音;而一声不吭的"成功"和真改了长得一模一样,
    这条命令最常见的用法就是运维照着单子一个一个敲过去。
  - **走已有的 `persona_update` 事件路径,不回去改创世那一条**(`agent_join` 一个字
    不动,导出来的包里仍是作者当初写的样子);事件的载荷是**自己的一份拷贝** ——
    投影和事件共用一个可变 dict 是这个仓库守着的另一条(`projection.py` 的
    `_apply_agent_join`),不在这里重新开一个洞。
  - **认不出的角色退出码 2 并把在册的人列出来**:编一个空回执出去的话,运维的人会
    以为改成功了。写命令**同样不许创世**(`_require_existing_world`)—— 否则 `--world-id`
    抄错一个字,你是在对着一个刚建出来的空世界改卡。
  - 校验**只用 `character_card` 那一份判断**(和 `validate world` 同一批),
    `contract --json` 的 `character_card` 段多一格 `write_command` —— 只报 `read_command`
    的话,"作者写得进"和"跑着的世界改得动"长得一模一样,而这两件事之间差的正是这一节。
- `anima-world contact --inbox`:`World.inbox()` 一直是对的而 CLI 上一个出口都没有,
  于是运维只能 `redis-cli HGETALL`。
- **玩家买得到东西了**(`World.player_shop()` + `economy.player_allowance`,
  REFERENCE §2.9.8)。这一格是 2026-08-12 那次真人试玩逼出来的,而现场的样子是
  这个仓库最该记住的一种:线上晚潮世界 `shop_stock` **43 行**、15 样能力要的东西
  **一样不缺、都标了价、库存 20**,NPC 的账本活着(2510 笔 `payment`、1021 次
  `item_consume`);而玩家走遍全镇 20 个地点、点开 44 个按钮,**15 个 `incapable`
  全是同一句**「你手上的 X 不够:要 1 个,你有 0 个」—— **而 X 就在两条街外卖
  1 块 2**。引擎这边 `shop`/`balance`/`inventory`/`player_buy`/`player_topup`
  五个门早就都在。三层(引擎 / 世界内容 / 能力声明)全是照着"玩家会买东西"设计
  的,少的只是从世界壳到那块屏幕的两根线。**判据不是"库里有",是最下游那一层
  点不点得到。**
  - **`player_shop()` 一屏**:`{location, location_name, in_transit, balance,
    shelf[], carrying[]}`。三个门拼一屏这件事每个宿主都要做一遍,而做漏的样子是
    安静的 —— 拼不出"这个按钮为什么是灰的",宿主就干脆不画按钮。所以拼装归引擎
    (和 `player_options` 同一个理由、同一个形状)。
  - **两类拒绝一个都不许合并**:`broke` 是"再去挣点"、`sold_out` 是"改天再来",
    玩家的下一步不同。而灰按钮上印的那句和真按下去被拒的那句**共用一个函数**
    (`_too_poor`)—— 另写一遍的话两句迟早分叉,而分叉的方向必然是屏幕上那句
    更好看。钱不够那句**说得出还差多少**:「买不起」教不会他任何事。
  - **`economy.player_allowance`(默认 0)= 他兜里的第一笔钱由世界给。**
    **只给一次**(`_touch_player` 的 `fresh` 那一支,落成账本上一笔 `allowance` 的
    `payment`)—— 每次露面补满的钱包不构成代价,他永远不必掂量买哪一样,于是货架
    又成了摆设,只是换了个方向。而它**不是 `player_topup` 的替身**:那一个是宿主的
    门(充值),挂到玩家点得到的地方就是让他自己印钱。默认 0 = 声明本身就是开关
    (和 perception / ontology 同一条);橱窗自己点亮成 60(**做了却开箱看不见
    等于没做**)。
- **按下去之前就说这件事要花多久**(`player_options()` 每条动词多一格 `cost`;
  `Scheduler.human_span()`)。四类拒绝管的是**点不动**的那些按钮,而
  **一个把人锁住一小时的按钮是点得动的** —— 它和一个瞬间完成的按钮在屏幕上长得
  一模一样。于是玩家点完才知道自己被占住了,而那时候已经晚了(线上真踩:重描一遍
  节目单,`duration: 12` × 5 分钟 = 一小时,期间所有能力全部 `busy`)。
  - **只说时间。** 量和材料的代价不写在这儿,因为它们已经有一条更好的路:不够的
    时候 `incapable` 会当场点名差什么。而时间不一样 —— 时间**总是**够,于是永远
    不会有人拦住他。
  - **`occupies` 才是代价的真实形状**:做椅子占着她、怀胎不占着,两者都花十个月。
    所以那句话分两种写法,占用的那种明说「这期间做不了别的」。
  - **格式化只此一处**(`human_span`):「还要多久」(拒绝那一句)和「要花多久」
    (菜单那一句)共用它,各写一份的下场是同一段时间在两块屏幕上写成两种说法。
    `tick` 照旧不出现 —— 走 `world.minutes_per_tick`。
- **点名「永远开不了的那道门」**(`ontology.unreachable_requirements()`;开机说一次、
  `validate world` 说一次)。一个写着 `requires: me_主动 >= 1.2` 的能力,在一个
  `主动` 上限只到 1.0 的世界里,是一个**玩家看得见、点得到、每次都被同一句话挡回来、
  而他做什么都不可能够**的按钮。四类拒绝里它报的是 `incapable`——那句话是对的,
  它教玩家「去把主动补上」,而这个世界里补不上。**作者要到有人玩了两个小时才会知道。**
  - **是警告,不是拒绝**,和 `drift_warnings` 同一个形状:这个量可能被这一层看不见的
    东西写(宿主的 `stock_set`、节拍脚本、创作台的一次编辑)。**误报够多次的警告等于
    没有警告**,所以判据反过来定:**认不出的写法一律答"不知道"**(`inf`),宁可漏报。
  - **恰好一处开口**:开机走装载那条路(`build_serve_scheduler`),不进
    `_precheck_ontology`——上一轮就是因为预检和装载共用一个函数,同一句警告在线上
    说了两遍。作者手上那一份归 `validate world`,和开机那份共用同一个函数。
  - ⚠️ **两边读自己的名字不一样**:能力里是 `me_主动`(要和对象的量分得开),规律里是
    光名字 `主动`(那一层的 owner 就是她)。搞混不报错——语法树上找不到那个名字,
    答一个 `inf`,于是**每一条靠规律涨回来的量都被算成够不到**。写这道 lint 的时候
    真踩了一次,是拿线上世界跑一遍才看出来的。
  - ⚠️ **`max(f, k)` 是一次抬升,不是保底**:`max(me_主动 - 0.02, 5)` 的上界是 5,
    不是 1。按"保底"读的话每一条带地板的衰减都会被算低,而算低 = 误报。
  - ⚠️ **`- k * dt` 必须认得出**(`_nonnegative`),否则这道 lint 在真实世界里
    **永远不响** —— 按流逝折算恰恰是这个引擎劝作者写的那一种(`drift_warnings` 整条
    就在劝这个)。第一版没认,拿线上的本体一跑,每个量的上界都是 `inf`。
  - 只管 `me_*`。`have_*`(随身带着几个)不归这道闸——它是运行期的库存,不是
    声明出来的量,这一层算不出它的上界。

#### Fixed

##### 三扇只读的门,都在拿一个世界不认得的人当作提问的那个人

又一轮真人试玩。对话本身干净(四轮之后她还记得玩家的名字和来意、当众说的那句被听见并
原样引回、四类拒绝各说各的),而**诊断那几扇门**一起漏了同一件事:它们回答得斩钉截铁,
只是回答的是**另一个人**的问题。三条都属于这个仓库最怕的那类 —— 照跑、渲染毫无破绽、
日志一行不错。

- **`anima-world prompt` 整份提示词是拿一个幽灵算的,而它一个字都不说。** 三条命令共用
  `DEFAULT_PLAYER_ID = "cli"`,但 `chat`/`play` 会 `player_move` 把这个 id 挪进世界、
  当场变成真人,`prompt` 是「看,但不碰」**永远不会** —— 于是它的默认值在这条命令上
  必然是个世界不认得的人。`self.players.get()` 交回空 dict,身份 / 在场 / 关系三块
  整个换一套算法。线上同一刻同一个角色实测:幽灵 8 块、真玩家 10 块,重合的那几块里
  有三块是**反的** —— 「没有告诉过你他叫什么名字…访客…对话媒介是手机文字私聊…不得
  臆造看见、触碰对方」对「正在与你交谈的人是 阿远(身份:旅人)…面对面交谈」;
  在场从「同在这里的还有:没有别人」变成「…阿远」;而幽灵那一份还把**真站在她跟前的
  那个玩家**列进「只是同场角色,不是正在和你说话的人」——引擎否认玩家是玩家。
  `absent` 里两条理由也因此是**似真而假**的。修法三段:
  - **`World.debug_prompt` 返回值加 `asker`**(纯加法):这一份是拿谁算的、
    `known` 说世界认不认得他。判据是**世界认不认得**,不是"他有没有位置" ——
    没位置的真玩家只是没落脚,而不认得的那个会让三块换算法。
  - **CLI `prompt --player-id` 默认改成 `None`**(不跟 `chat`/`play` 一样默认 `cli`),
    不给就**去世界里挑一个真的**(优先站在她跟前的;排序取而非随手取,免得同一条命令
    两次给出不同的提示词)。
  - **抬头永远印「这一份是拿谁算的」**,是陌生人时黄字明说,并告诉你怎么查真 id。
    ⚠️ **库那一侧绝不悄悄换一个真玩家顶上** —— 换了就是第二种撒谎;它只是**说出来**。
- **`player options` 用一句 3.2.0 起就不成立的话解释"找不到这个人"。** 那句写着
  「⚠️ 位置是**进程内**的,这条命令是另一个进程,所以对一个跑在别处的世界它总是这一句」——
  而在场早在 3.2.0 就搬进了 Redis(`presence` 那条已经改过口,这条漏了)。留着比没有更坏:
  它教人把一个**真**信号(宿主从没调过 `player_move`)当成 CLI 的已知毛病挥手过去,
  于是那个宿主永远查不出来。现在说的是三种可能,并指到 `presence` 去分辨。
- **`presence --player-id X` 把"筛过之后是空的"说成"这个世界跟谁都还没打过交道"。**
  一句世界级的空会让人跑去查宿主接没接上,而真相只是这一个 id 抄错了。顺带同一处:
  id 那一栏写死 20 字,而真部署里 `player_id` 是 membership 的 uuid(36 字),于是 id
  和地点糊成一坨(`…c463fabcafe`)—— 只有 `p1`/`cli` 那种短名字才看着是对的。

`tests/test_debug_prompt.py` 新增 7 条、`tests/test_player_presence.py` 新增 3 条,
全部先验证过会红。

##### 她漏写了五个字符,于是玩家收到零个字节 —— 而这和「应用崩了」长得一模一样

真人试玩抓到的一轮:她整轮只输出了 `〔delay_reply {"minutes": 5}〕` —— 少了 `tool:`。
解析器只认写了前缀的那种写法,于是这条标记被判成 `unknown`、**照规矩咽掉**(不咽的话
玩家会看见引擎的内脏),工具没跑,那一轮交出去的正文是**空的**。三件事一起坏:她的
选择在世界里没兑现、玩家的屏幕上什么也没有、而日志干干净净。

修在三处,共一条判据:**只认这个世界真有的能力名。**

- **`directives.parse_body` / `DirectiveParser` 收一份 `tool_names`**。这个模块不认识
  工具注册表,也不该认识 —— 名字由 `chat_service` 注入(`tools_for("*", CHAT)`)、
  由 `autonomy` 注入(它手上早就有 `allowed`)。**猜一份名字表出来是这一层最坏的错法**:
  猜错了不报错,只是从此多吞或少吞她的话。
- **两种写法共用一份参数解析**(`_tool_directive`)。分成两份的话,`〔tool:x {...}〕`
  和 `〔x {...}〕` 迟早解析出不同的参数,而那是这一层最难查的错。
- **裸名字只在全角括号里认**(`_bare_ok`)。ASCII 那一侧不认,因为 `eat` / `work` /
  `sleep` 这种名字在散文里是真会出现的;而 `〔〕` 是提示词里教的权威写法 —— 写下它的人
  已经在说"这是一条指令"了。

⚠️ **闸一道没松**:这个世界没有的名字照旧是 `unknown`(`sing_a_song` 不因为长得像
能力就被放行),没给名字表时行为与从前逐位相同。

##### 同一轮的另一半:她只留下一条指令、一句台词都没有

上一条修完,工具跑起来了 —— 玩家那一侧**仍然是零个字节**。这是独立的第二个洞:
`delay_reply` / `walk_away` / `end_conversation` 都是"做了一件事然后闭嘴",而
`ToolResult.text` 是唯一的逐能力玩家可见通道,**全仓库没有一个工具设过它**。

于是补在**轮**这一层(`_silent_turn_note`,三个分派点各接一次:`respond()` /
`send()` / `autonomous_loop()` 的连续输出),新增可改的模板 `chat.silent_turn`
(默认 `（{name}没有接话。）`)。三条纪律:

- **写旁白,不替她说话。** 括号里那半是引擎在陈述这一轮发生了什么,不是她的台词。
- **判据是"这一轮有没有发生过什么"**,不是"文本空不空"。一次空的模型回包同样是零个
  字节,而那是**故障** —— 给故障配一句「她没有接话」,等于把 LLM 挂了伪装成她的性格,
  连排查的线索都一起抹掉。所以只在她**做过选择**(调了能力 / 让了位 / 写了一条读不懂
  的标记)时才补。
- **不按能力分岔。** 具体是哪种沉默由别的路交代(下一条消息会收到静音的理由、到点她
  自己回来敲门);在这儿分岔就是把某一版文案刻进引擎,而刻错了不报错。

连续输出那一路**只补一次**:后面几步的沉默前面已经说过话了,再补等于在对话里插一句
多余的旁白。`empty_step` 那条中断原样保留。

##### 「等我五分钟」变成了等我五秒 —— 一个承诺的两半跑在两个时钟上

`delay_reply` 从一个 `minutes` 参数写出两样东西:`set_quiet` 的静音走**墙钟**
(玩家的五分钟是真的五分钟,`chat_state` 那侧早就写死了),`add_followup` 的回访走
**tick**。而 `_ToolRuntime.ticks_for_minutes` 拿 `world.minutes_per_tick` 折算 —— 那是
**世界时间**每 tick 走多少分钟,和真实时间没有固定比例。

两者只在引擎默认值下恰好相等(5 分钟世界时间 = 300 真实秒),**所以开发机和整套测试
一路是绿的**。线上那个 `tick_rate=0.2` 的世界里,五分钟折出来是 1 tick = **5 秒**:
调度器五秒后就把回访兑现了,顺手 `clear_quiet` 撤掉配对的那次静音。她话音未落自己
回来敲门,而玩家那一侧既没看见「她在忙」,也没等到那五分钟。

改成走 `scheduler.tick_rate`(每真实秒几个 tick)。这个数唯一的用处就是给 `delay_reply`
排那次回访,而**回访和静音是同一个承诺的两半** —— 两半用两个时钟,就是这个 bug 本身。
`DEFAULT_SECONDS_PER_TICK` 落在 `world_time.py`,`config_store` 的
`scheduler.tick_rate` 默认值改为引用它:`1/300` 从此只有一个来源
(`ConfigStore.get` 的 docstring 早就在抱怨这个键有两份写死的默认值)。

⚠️ 这三条**没有任何一条被测试发现过**,原因各不相同,但都是"照跑但给错东西":
Z1 走的是 `unknown` 那条**合法**分支;Z2 的产物是空串,而没人断言过空串不合法;
Z3 的两个公式在引擎默认值下**逐位相等**(`ticks_for_minutes` 在 `tests/` 下的引用数
是 **0**)。三条各补了一条先验证会红的回归测试。

##### 世界文件里写的初值被安静丢掉 —— 灯塔湾丢了 11 行,五个人的性格从此一模一样

装载器只认 `{"owner": …, "values": {量名: 数}}`,而灯塔湾那份文件末尾有 11 行写成
逐条的 `{"owner": …, "key": …, "value": …}`。从前的处置是一条 `logger.warning` 加
`continue`,于是**世界照常开机、日志干净**,而五个角色的 `initiative` /
`agreeableness` 全停在引擎默认的 1.0 上 —— 作者写的 1.5 / 1.3 / 1.2 / 1.1 / 0.5
一个都没进世界,排班里那些"谁先开口、谁让着谁"的条件算出来的是同一个数。
`validate world` 对着这份文件说的是「没有发现问题」。

**两道闸,各自独立:**

- **`world_seed_errors` 认这个形状**(新增 `_stock_entry_errors`)。`stocks` 是可选段,
  但**写了就查形状** —— 这和 `world_seed_warnings` 那条「只警告、绝不拒绝」不冲突:
  advisory 管的是**引用**(引擎没有合法值全集,一个自造的动作名、一个中途入场的角色
  都合法,拒绝它会让设计正确的世界在小版本升级后开不了机);这里查的是**你写的这行
  有没有人读**,而没人读的行,答案在任何引擎版本上都一样。`complete=False`(一次编辑)
  照样查:一份编辑文件里的坏行和一份完整世界里的坏行丢得一样干净。
- **`_seed_stocks` 自己也不肯丢**(并进已有的 `problems` → `OntologyError`)。只留前
  一道的话,任何绕开校验的入口照旧安静地少装一批初值 —— 而这个 bug 上一次发生就是
  这么发生的。**值不是数的那一支同样从警告改成拒绝**:跳过一项 = 那个量停在声明的
  默认值上,而作者以为他给过初值了。

判据两边**逐字同一个**(`float(raw)`),不是各写一份"长得像数吗" —— 两份判断迟早
给出不同答案,而那种不一致会表现成「预检说没问题,开机还是失败」。

⚠️ **文件改对了,已经跑起来的世界不会跟着变**,这是对的:合并的粒度是每个
(owner, 量名) 且**只增不改** —— 那五个量在世界里已经有值(引擎注册角色时落的 1.0),
所以文件里的新值填不进去。拿今天文件里的初值去覆盖,就是把这个世界跑出来的现在
倒带回创世那一寸。线上那份得走 `World.set_stocks()` 单独拨,和改文件是两件事。

##### 常数步长那道 lint 对**世界的规律**整个是瞎的 —— 而它就是为一条世界规律写的

`drift_warnings` 按"这条表达式读了它自己写的那个量吗"判漂移,而**世界的量读写
不同名**:`for_each: {"owner": "world"}` 的规律**写**光秃秃的 `雨天数`(带前缀写
反而被 `bad_output_name` 拒掉),**读**回来必须是 `world_雨天数`。名字对不上,
于是这道闸对整类世界规律一声不吭 —— 它 docstring 里那个例子按字面根本跑不起来。

而它被写出来针对的那次事故(线上晚潮多烧了 6 天雨、洪水提前两天半)**恰恰就是
一条世界规律**:lint 从头到尾没响过。灯塔湾同样如此 —— 10 条漂着的规律只点到 7 条,
少的 3 条全是 `owner: world` 那几条(雾、渡轮延误、连续说英语的天数)。

前缀只对 `owner: world` 的规律算数:一条按角色跑的规律写自己的 `雨天数`、读世界的
`world_雨天数`,读写的是**两个**量,点它就是误报 —— 而误报够多次的警告等于没有警告。
`WORLD_OWNER` 随之从 `stocks` 挪到 `rules`(前缀和 owner 名是一对,分两处写迟早各改各的)。

##### 一个跑着的世界改不了自己的声明 —— 修一个错字要连玩家的进度一起抹掉

作者指名 `--world-file` 去编辑一个已有的世界时,同名的**种类**和**规律**从前
「一个字都不动」。于是创世那天写下的每一处都是永久的:一个动词的 `label` 写错、
一条规律的常数漂了、一样代价调得不对 —— 唯一的修法是把这个世界抹掉重建,而那要
连同每个玩家的关系、记忆、随身物品一起抹。

**「只填缺,不覆盖」那条纪律说的是状态**:整份写回会把长了三十天的树倒带回幼苗。
而声明不是状态 —— 一个种类、一条规律身上没有任何东西会随时间漂,所以那条理由在
它们身上不成立,照搬的代价却是把作者永远锁在创世那一天。

- **`kinds` 与 `rules`:作者指名的那份文件里那条赢。** 内置兜底那份**一个字都改不动**
  (它每次开机都在手上;让它重写同名声明 = 每次重启拿橱窗的物理法则盖掉别人的世界)。
- **`entities` 仍旧只增。** 实例有 `location`,而位置另有一份镜像落在可见性表里,
  那一份是只填缺地写的;覆盖了这边不覆盖那边,同一个东西会有两个"它在哪"。
- **声明改了,可见性那份镜像跟着改**(`_apply_ontology(redeclare_kinds=)`)。量的
  `visibility` / `label` / `bands` 都写在种类声明里,而她**读到的是镜像那个** ——
  两边不一致的话同一个量在菜单上一个名字、在拒绝语里另一个名字,没有一处会报错。
  作者显式写在 `stock_visibility` 段里的那些照旧赢。
- **没变的不写**:每次开机都重写会把 `updated_at` 刷成开机时间,于是"这条声明上次
  改是什么时候"从此没有答案。

线上就卡在这儿:灯塔湾是个教英语的世界,每个动词按钮上印的是引擎的中文默认词
(`look` → 「端详」),而里面住着四个真人。

##### `every: {"days": 1}` 把 288 写死了,而一天多少 tick 是每个世界自己的配置

`rules.py` 开头就写着「`now % 288` 这种手算解决不了 —— 一天多少 tick 取决于
`world.minutes_per_tick`」,而 `_parse_interval` 正是那么算的。一个把
`minutes_per_tick` 调成 10 的世界(一天 144 tick)里,`every: {"days": 1}` 一天跑
**两遍**,作者按"一天一次"写的常数因此翻倍 —— 世界照跑、日志干净,只有数是错的。

`parse_rules(entries, ticks_per_day=)`,装载时喂进去的是这个世界真正的那份换算
(`_load_world_rules`)。默认值仍是 288 = 没人说话时的 5 分钟一 tick。两条测试:
一条钉换算本身,一条走真的开机路径 —— 收得下参数不等于开机时有人喂它。

##### 空货架也说得出为什么空(`blocked_text` 那条的另一半)

- **`player_shop` 多两格:`empty` 枚举 + `note` 那句人话**,和 `player_options` 的
  `blocked` / `blocked_text` 逐字同构。三件事在数据上长得一模一样(`shelf: []`),
  而玩家的下一步完全不同:在路上要等、还没落脚要先站过去、这儿本来就不卖东西
  要换个地方。
  - **这句话从前没有主人。** 上一条修 `blocked_text` 时的原话是:「站点那块货架
    面板自己写了一句『你还在路上 —— 到了再看』,两块姊妹面板于是一块说人话、
    一块说引擎的话」—— 把能力那半收进引擎而把货架这半留在站点,等于把同一个
    分叉换了个方向再犯一次。**下一个宿主拼漏的样子是安静的**:玩家面前一块
    什么也没写的空板子,而世界照跑、日志干净。
  - `test_货架空着的每个理由都配了一句话` 对着 `_SHOP_WORDS` 逐格点名,和
    `_BLOCKED_WORDS` 那道闸同一个形状。

##### 一段关系永远长不出来:她听说了那个人,而引擎说「查无此人」

- **闲话里认人的那道闸,问的是「这个世界里真有这么个人吗」,不是「她认不认识他」**
  (`Scheduler._hearsay_roster` 的 `ids` / `judge_hearsay(known=)`)。线上那个世界的
  日志里逐条写着:`hearsay judge named '林迟', who is not on the roster` ——
  **而林迟就在这个世界里站着**(id `chi`),沈川也在(`shen`)。她跟他们还没来往过,
  于是他们不在她的关系表里,于是整条反应被丢掉。
  - **代价是这一层最值钱的那一刻整个不存在**:合着的时候只有**已经认识的人**之间的
    关系动得了,而「我还没见过他,但我已经听说了他的事」在恋爱陪伴这个品类里恰恰是
    最有分量的一句。等于这条机制只会加深已有的关系,一段新的永远开不了头。
  - **分成两份名单**:`weights`(名字 → 好感度)是**给模型看的**那一半,只放她真来往过
    的人 —— 给一个没来往过的人编一个好感度出来,又是一处引擎替她做主;
    `ids`(名字 → id)是**闸**的那一半,这个世界里真有的人全在里面。
    合成一份省下的那点事,买来的是"关系不可能从零开始"。
  - **闸一个字没松**:编出来的名字照旧翻不回 id,照旧当场丢掉。放宽的只是"谁算真人"。
  - ⚠️ **失效的样子和"她听了没往心里去"一模一样** —— 空的 `reactions` 是这一层最
    常见的正常结果,所以这个洞在产物上完全隐形,只有那一行 `WARNING` 认得出来。
    那行日志现在改口说 `who is nobody in this world`:它以前说的是实话,但说的是
    另一件事。

##### 她那个钟点安静地没事做:那个地方她写在了旁边一格

- **计划里的那个地方 / 那个人,不管写在哪一格都收下**(`planner._pick`)。这是
  `start` / `start_min` 那条的直接续:**字段在哪一层、叫什么,只是形状**。翻线上那
  个世界的 LLM 流水,13 步是这么写的 —— 提到了 `params` 外面一层
  (`{"kind":"walk","location":"studio","params":{}}`)、连 `params` 都没写、
  或者层对了换了个名字(`params:{"target":"studio"}`,而 `walk` 那一格叫
  `location`)。三种全被整步扔掉。
  - **代价和 `bai` 那次一模一样**:扔掉一步 = 她那个钟点安静地没事做,而玩家看到的
    是一个站着不动的人。恋爱陪伴产品里没有比这更贵的沉默,而它**只在一行日志里**。
  - **闸一个字没动**:值照旧要 `_unlabel` 得出清单里的一个 id,所以她编一个「哈尔滨」
    出来仍旧当场丢掉 —— 放宽的只是「她把答案放在哪儿」,不是「什么算数」。
    守这条的测试把四种写法和四种编造分两组各点一遍。
  - ⚠️ **换名字那一种只在这个动作只有一格参数时才认。** 两格的时候「哪个值该填哪一格」
    没有答案,而这一层猜错了不报错:猜出来的计划和她说的是两件事,她照着走一天,
    没有任何一处会发现。按名字认的那两种和有几格无关,照收。
  - 收下了就在 `INFO` 上说一句她写成了什么 —— 哪天模型集体改了写法,这行日志是
    唯一看得见的地方。

##### 又一轮真人试玩:她突然不记得刚才那段了,而日志一行不错

五件,前三件其实是**同一件**。共同点是"两件不同的事被当成了一件":一次只读的开门
和一次崩溃后的开机、「没有关系」和「关系是零」、名字和它后面那个字。

- **只读地开一次门,会把别人正说着的话掐断。** `World.__init__` 无条件收孤儿会话
  (`reap_orphans`),而收尾那条线程只在 `start_clock()` 下才有 —— 于是任何一次
  只读的 `World.open`(`anima-world map` / `prompt` / `ontology`、运维脚本、一次
  `docker exec` 的探针)撞上一个跑着的世界,都会:① 把玩家此刻正说着的那场会话
  当场关掉,② 在一个马上就要退出的进程里点起关系判定,于是那次判定**永远落不了地**。
  玩家那一侧读到的是「她突然不记得刚才那段了」,而**日志一条错都没有**。
  - 分界不是"世界空不空",是**这会儿有没有别人跑着它**:`Scheduler.another_runner()`
    读 `:meta` 的占用戳(pid / host / 新加的 `owner_token`),有人跑着就把收尾让给
    接得住它的那个进程,没人跑着才收。**读戳必须在盖戳之前** —— 盖完再读,读到的
    是我自己。
  - `owner_token` 是**每个 `World` 实例**一个 uuid:光看 pid 认不出"同一个进程里
    的第二个 `World`",而那正好是要分开的两种情况之一(测试进程、宿主进程都会开
    第二个)。
  - **占用戳是提示,不是锁**:`another_runner()` 只用来决定"要不要替它收尾",
    绝不用来拒绝任何操作 —— 一个陈旧的戳会把整个世界锁死,而戳陈旧是常态
    (进程被 kill 时来不及擦)。导出照旧剥掉这三格(`owner_token` 跟着走)。
- **0 不是形同陌路。** 判定器的提示词里那一行写着「观感:0.00(范围 0 到 1,
  **0 是形同陌路**)」,而 0 的真实含义是**这两个人还没有结算过关系**。线上现场:
  玩家和她聊了二十分钟(转录就在同一份提示词里),然后邀她做一件事,她回
  「素不相识,没必要配合一个陌生人的即兴邀请」—— 引擎递给模型一对矛盾的事实,
  模型挑了更斩钉截铁的那句。**根子不在模型**:没有关系行和关系值为零是两件事,
  而引擎把它们印成了同一个字符串。`_closeness_phrase` 现在把 0 念成「他们还没有
  来往(这不是嫌隙,是还没处出交情)」。
- **同一条纪律的另一头朝着宿主**:`_relationship_row` 里 `exists: false` 的行照旧
  报 `band: 2 / band_name: "不远不近"` —— 那是拿 0.0 去查档表查出来的。空白不是一个
  档位,它是**还没有值**;现在这两格是 `None` / `""`,宿主自己决定画成"还没开始"
  还是不画。
- **同一条纪律的第三头是那个档名本身:「淡漠」→「不远不近」**(`BAND_NAMES[2]`)。
  这一条是装上产物真聊了一局才看见的:玩家跟她热络地聊完一场(大雨天她照样开着
  咖啡车,给他煮了杯热的,叫他进棚子躲雨),判定落地之后关系面板上是这两行 ——
  「淡漠」,和「上一次两个人的来往让它更近了一步」。同一屏两个情绪,而玩家会信
  上面那个词。**根子和前两条一模一样**:−0.2…0.2 是每一段关系的**起点**,而
  「淡漠」是一句**判词**(她对你冷淡),这一档的真话只是"你俩还不熟"。
  改成「不远不近」——说的是距离不是态度,两个方向都读得通(从「熟识」退回来
  也是这句)。⚠️ 这几个名字原先只进记忆摘要(那一档的注释里就是这么写的),
  是 `player_relationships` 把 `band_name` 递给玩家之后才变成一句给人看的话,
  而**没有一处跟着重新审过它是不是给人看的措辞**。
- **玩家那一屏上其余每个名字都翻过了,只有量没翻。** 东西的 `name`、那行 `gloss`、
  动词的 `label`、四类拒绝、代价 —— 全是人话,然后 `quantities` 把 `phrase_age`
  这样的内部键和裸数字原样递出去,而宿主没有别的东西可印。线上那一屏于是写着
  「今日短语 phrase_age 3」,作者明明写了 `label` 和 `unit`。`bands` 更要紧:
  她读到「雨势 瓢泼大雨」,玩家读到 `雨势 0.82` —— 同一个世界的同一个量,两个人
  看见两种东西。
  - `player_options()` 每个 target 与新增的 `own` 各多一份 `readouts`:
    `[{key,label,value,word,unit,text}]`。**`quantities` 那份键与数字仍是契约**,
    `readouts` 是加上去的(宿主要自己排版就拿分开的几格,不想排版就印 `text`)。
  - 措辞走**新抽出来的 `perception.readout_text` / `readouts`** —— 和她那行提示词
    (`_describe`)是同一个函数。各写一遍必然分叉,而分叉了不报错。
  - 顺带堵上一个同源的洞:`units` 那张表此前**只填了 `here` 那一档**,于是同一个
    `unit: "点"` 在树身上念得出「树高 3.2米」、在她自己身上念成「体力 100」——
    作者写了一个字,引擎按感知档次决定认不认。新增 `own_units` / `public_units`
    两格(`to_dict` 只加不改:`units` 的形状一个字节没动)。
- **中文不分词,而名字和动词是作者写的。** 线上原文:`Dr. Eleanor Finch在赶路`。
  换成一个作者起的名字更糟 —— `老陈的猫不在这儿` 会被读成"老陈的、猫不在这儿"。
  拒绝那句话是玩家唯一能读到的解释,读岔了他去改错东西。现在**数据里来的那一截
  划边界**(`Scheduler._named` / `World._named`,用「」不用反引号——反引号是
  markdown,玩家屏幕上就是两个撇号),引擎自己的字不划,玩家那个「你」也不划
  (它是代词,套一层框读起来像在念一个人的名字)。落点:`joint_precheck` 的两句、
  `participant_gate` 的两句、`_consent` 的两句、"不能跟自己一起"、`player_buy` 的
  地名与货名。
- **一个量,全世界只能有一个叫法。** 上面两条各修好了半边,然后装上产物再玩一局,
  两条在同一屏上撞出了第三件:菜单上写着「土 正好」,点下去被拒绝成
  「「土湿 > 0.55」不成立」—— **同一个量、同一屏、两个名字**,而屏幕上根本没有
  「土湿」这个词。玩家会去找一样不存在的东西,而这条误导一次报错都不会有。
  - `speak_expression` 新收一个 `quantity_label(scope, key)`,由
    `Scheduler.quantity_label_for()` 提供;查的是**可见性表**(`labels_map()`),
    也就是 `readouts` 印在屏幕上的那一份。**三个作用域各查各的主人**
    (裸名字查这个东西、`me_` 查这个人、`world_` 查世界那一份):合成一张全世界
    的表会踩本体层早就踩过的坑 —— 两个种类各有一个「新鲜度」时只留得下一个。
  - ⚠️ 这一处第一版查的是本体(`Ontology.labels_of`),**它漏两样**,而两样都是
    线上真的撞得到的:世界自己那份量(作者写在 `stock_visibility` 里,从来不属于
    任何种类,查本体永远是空)、以及事后 `declare_visibility` 改过的量。照本体查
    的话玩家在菜单上读到「江水位(米) 2.4」,点下去被拒绝成「世界的江水位」——
    修的病原样复发了一次,只是换了个作用域。本体那份是**上游**(装载时播进
    可见性表),所以并集在可见性表这一边,该查的一直是它。教训和 §「调试视图不许
    撒谎」同一条:**要和屏幕说同一句话,就得读屏幕读的那个库**,不是读它的上游。
    `Ontology.labels_of` 随之删掉 —— 留着就是给下一个人一个查错库的机会。
  - 只在真要拒绝时才问。
  - ⚠️ **这一条起初只做到了一半,而另一半在同一屏上**。改完名字再装上产物玩一局,
    那扇窗底下写着的是「土 正好」配「「土 > 0.55」不成立」——**分过档的量,玩家
    永远读不到它的数字**(`readout_text` 的规矩就是这样),于是 `0.55` 对他是一串
    没法比对的噪音,和 `me_` 前缀、和内部键完全同一类。这一条原先记的理由
    (「阈值是作者的理由,抹掉它就只剩一句"你做不了"」)默认了他看得见那个数 ——
    对没分档的量成立,对分过档的量从来不成立。
  - 于是补上后一半:`speak_expression` 再收一个 `quantity_bands(scope, key)`
    (`Scheduler.quantity_bands_for()`,同一张可见性表的 `bands_map()`,作用域分法
    逐字相同),**分过档的量把阈值也念成档词**,走的是 `readouts` 那个
    `band_word` —— 同一份档表、同一个函数,两处分不了叉。「土 > 正好」。
    **没分档的量照旧留数字**(「你的体力 >= 4」):那个数他在菜单上读得到
    (`体力 100点`),留着才教得会他还差多少。两半一起验,只验一半的话"把数字
    一律抹掉"也是绿的。
  - 档词是**有损**的,而这是作者的分辨率在说话:一档里的两个阈值念出来一模一样。
    要让拒绝语分得更细,作者该做的是**把档的边界划在阈值上**,而不是让引擎漏一个
    数字出去。
  - ⚠️ **有损带来一处必须跟着松一格的地方**,而它是装上产物、把线上两个世界的每一条
    拒绝语逐条念一遍才看见的:档词说的是这一档的**起点**,于是阈值落在档中间时
    `>=` / `<=` 会印出一句自相矛盾的话 —— 晚潮世界里有 18 条,
    「你的手上的活儿 >= 生手」摆在一个屏幕上正写着「手上的活儿 生手」的人眼前
    (他明明就在这一档里,却被告知要够到这一档)。现在念作「> 生手」:要的是比这一档
    更往上(`_LOOSER`)。**阈值压在边界上时一个字不动** ——「>= 锃亮」读作"要够到
    锃亮",没有一处矛盾,松了是凭空多要一格。`>` / `<` 本来就不用动:拒绝时当前值
    就在阈值的另一头,「雾气 > 透亮」正好读成"要比你现在多"。
  - 这一处**修不成内容**。晚潮那 18 条里 13 条压在玩家自己身上,光 `agent.手艺`
    一个量就有 0.7 / 0.8 / 0.9 三个阈值挤在同一档里 —— 边界划在哪儿都救不了三条;
    而阈值住在 `kinds` 里,**运行期是冻的**,活着的世界改不动。所以它是引擎的活。
  - 顺手拆掉一个还没炸的雷:改写此前是**逐个名字轮着替换**,于是会咬自己刚写下的
    字 —— `土湿` 换成「土」之后,轮到名字 `土` 那一遍会把它再换一次。现在整条改写
    走 `expressions.rewrite_source`,**按语法树上的位置改**:名字换在 `Name` 节点
    上、档词换在比较另一头那个数上,两样都不可能咬到对方或咬到自己。正则那一版
    还有第二个洞(字符串字面量里恰好写着一个量名时一样被改掉),位置也没有。
    只翻**比较另一头是个光名字**的数(`土 > 0.55` 翻,`土 > 湿度 * 2` 里的 `2`
    不翻 —— 它不是「土」的一个值,念成档词就是胡说);负阈值(`me_心情 < -0.3`)
    在语法树上是 `UnaryOp` 不是常量,单认了一层,不然最该翻的那种原样漏成数字。
    ⚠️ `ast` 的 `col_offset` 数的是 **UTF-8 字节**不是字符,而这个仓库里的量名全是
    中文 —— 照字符切会切在字节中间。
  - 少材料那句(`consumes`)是散文,名字右边紧跟着一个「不」字,所以照 D 那条把
    货名划出边界:「你手上的「一小罐蜡」不够」。`requires` / `conditions` 那两句
    **有意不划** —— 整条表达式外面已经围过一次,再套一层就是
    `「你带着的「伞」 >= 1」`,而那一处的下一个字永远是运算符,边界本来就断得开。

上一轮把整套经济接到了玩家点得到的地方,这一轮就真的去点。买鱼杂 → 走两条街 →
喂猫这条环闭合了,而闭合的路上撞出五件。共同点是**引擎写对了,传坏了** ——
每一件的代码里都躺着一句写给人读的中文,而玩家屏幕上收到的不是它。

- **玩家读得到的拒绝,不许坐 `KeyError` 这班车。** `KeyError.__str__` 是
  `repr(args[0])` —— 全 Python 独此一家。世界壳照规矩 `str(exc)` 原样传、一个字
  没改,而玩家屏幕上出现的是 `'小念咖啡车没有一杯拿铁了'`,**带着一对单引号**;
  线上真的这样。所以这条路上以后只有两种车:走不通(`LookupError` —— 你不在
  这儿 / 这儿没有 / 世界还不认识你)与钱不够(`ValueError`),两种的下一步不
  一样,而两种都说人话。
  - `player_buy` 三处改完:世界不认识这个人那句从
    `KeyError("player {id} not present")`(英文 + uuid + 引号,三样全中)改成
    「世界还不认识你 —— 先在一个地方落个脚」;卖完了那句改成 `LookupError`;
    **抢购那条竞态路径共用同一句话** —— 它此前还在拼 `f"{location_id} 没有
    {item_id} 的货"`,两个 id 全裸。修在引擎不修在世界壳:壳里包一层等于让
    「拒绝的措辞归引擎」这条规矩多一个例外,而例外迟早长成第二套措辞。
- **`busy` 那句拒绝印门牌号,不印名字。** 线上现场:「你手上还有一件事没做完:
  喂 cat:bai —— 还要 3 个 tick」。玩家从没见过 `cat:bai`,他看见的那只猫叫
  **白手套**。能力表、条件、材料三处早就都查名字了,只有第四类拒绝是照着
  `held["target"]` 直接拼的 —— 印 id 给玩家是这个仓库反复修的同一类 bug,而它
  每修一处就在别处露一次头,因为查名字这件事此前没有一个统一的落点
  (现在有了:`Scheduler._display_name_of`,**只在这一处退回 id**)。
- **`tick` 是引擎的词,别拿它回答"还要多久"。** 「还要 3 个 tick」回答不了玩家
  真正在问的那个问题:他要么去查文档,要么放弃。而世界自己有答案 ——
  同一个 3 tick 在两个世界里本来就是两段不同的时间(`world.minutes_per_tick`)。
  `Scheduler._human_wait` 把它翻成分钟 / 小时 / 天,0 是「马上就好」而不是
  「还要 0 分钟」。
- **钱有最小单位,所以折账的时候就进位**(`projection._apply_payment`)。二进制
  浮点存不下 0.1,所以一个玩家买了六样东西之后,接口回给他的余额是
  `0.3799999999999921`。前端 `toFixed(2)` 把它盖住了 —— 而账本是这个引擎对"钱"
  的定义,不是屏幕上那一行:**门禁读的是这个数**,一笔"正好够"的交易迟早会被它
  拒掉,而那一次不报错也不留痕。
  - **两头一起进** —— 单边进的话玩家少的和镇上多的对不上,账本不再守恒,而守恒
    是"账本是投影"这句话的全部内容。
  - ⚠️ 验它得往账本上折**带分的数**:内置世界的价钱全是整数(6/12/25/35/4),
    拿它买三样东西验不出这件事 —— 头一版测试就是这么假绿的。线上那些价钱是
    价格漂移出来的 `5.23` / `1.16`。

- **`player_buy` 隔着半个镇子也买得成,而三句拒绝里两句是引擎的词汇。**
  现在**人得站在那儿**(和能力调用上那道 `absent` 闸同一条:一次交易是一个人、
  一个地方、一个瞬间)。放开的话玩家在渡口就能刷光全镇的货架,而"走过去"本身是
  这个世界里的一段代价。拒绝**两头都说**地名 —— 只说"它在铁匠巷"会读成一句谎,
  真正的原因可能是世界压根不知道他在哪。没货那句从 `KeyError(item_id)` 改成
  「铁匠巷没有一卷焊锡了」:`item_id` / `location_id` 印给玩家是这个仓库这一轮
  反复修的同一类 bug。
- REFERENCE 上 `player_topup` 那一行写着「钱包是在场状态,重启即清」——
  **那句是假的**(`test_the_balance_survives_a_restart` 一直在守着相反的行为)。
  文档说一套代码做一套,是这个仓库最怕的那种不一致的文档版。

- **别的进程写进日志的事件,这个世界永远读不到**(`Scheduler._fold_gap_before`)。
  水位 `_projection_seq` 的含义是「≤ 它的事件我都折过了」,而 `_apply_memory_trigger`
  在自己追加一条时**直接把水位挪到了自己那条的 seq** —— 中间那些别的进程写的、这个
  进程一条都没折过的事件,就这么被签了字;而 `catch_up_projection` 只往前看,于是
  它们**再也不会**被折进来。**不是竞态,是必然**:一个跑着的世界每 tick 都在追加事件。
  - 线上 `night-tide` 就是这么坏的:从维护容器给四个角色写了角色卡,事件确实进了
    `anima:night-tide:events`、回执写着 `changed=True`,而长驻进程里的玩家永远看到
    `card: null` / `billing: supporting`。**零报错** —— 投影"少折了一条"和"那件事没
    发生"长得一模一样。
  - **爆炸半径比角色卡大得多:关系就是投影。** 对着一个跑着的世界 `player forget`
    一个离开的玩家,共享 Redis 里的联系态 / 姿态 / 在场清掉了,而那个进程内存里的
    `proj.relations` **永远留着那个幽灵** —— 她继续惦记一个不存在的人,占着社交位。
    运维台维护白名单存在的理由就是这条命令,而它一直是坏的。
  - 修法是**在折自己这条之前先把空档补上**,并且**只在真有空档时才多跑一次 replay**
    (`seq > 水位 + 1` 这个比较是免费的;无条件 replay = 每条事件一次 Redis 往返,
    退化成"每条事件都全量对账")。补空档时按 `seq` 截断,不把自己那条折两遍
    (`payment` 折两遍 = 账翻倍,同样不报错)。
  - **纪律**:水位前进的条件是"折过了",不是"我写了一条"。和 `reset_projection`
    那条("两个字段各写各的就是那个洞本身")是同一族的第二个洞 —— 那一次是折了却
    不挪水位(事件折两遍),这一次是挪了水位却没折。
- **`World.state()` / `World.roster()` 读之前先补课。** 修好水位之后一个**跑着**的
  世界会在下一次追加事件时自愈,而一个暂停的 / 空闲的世界不会 —— 而这两个恰恰是
  玩家那一侧真正在读的两扇只读门(运维台的壳 `/internal/v1/state` 与
  `/internal/v1/roster` 就调它们)。只读门不该指望世界正好在动。
  `set_card()` / `act()` / `forget_player()` 早就在补,这两个漏了。
- **告别之后,关系图上还挂着那个人**(`World.forget_player()` 显式 `drop()` 边,
  回执多一格 `edges`)。**关系有两份记法,告别只走了一份**:投影里的 `relations` 是
  可变的数值,折叠端一折就没了;关系图上的 `edges` 是"这两人是朋友"这个**事实**,
  住在自己的表里而且**只增不减**(`add` 是 INSERT OR IGNORE),折叠端根本碰不到它。
  于是 `compute_cliques` 照着那条边把一个已经清干净的幽灵算进她的小团体,
  `World.cliques()` 报得出一个不存在的人 —— 又一个零报错。
  - 线上 `night-tide` 上真的有两条:`agent:nian friendship agent:aubrey-player`
    和它的反向,联系态 / 姿态 / 在场全清干净之后还在。
  - `drop()` 这个原语本来就是为撤销存在的(关系反转时一直在用),只是这条路没调它。
    撤掉是**永久**的:边只在事件当场落库那条路上写(`_apply_memory_trigger` →
    `_on_relation_shift`),重放不重建,所以不会隔一次重启自己长回来。
  - 比对象是整个节点 `agent:{player_id}`,**不是子串** —— `aubrey` 是
    `aubrey-player` 的子串,按子串撤会连着把另一个人的边一起撤了。
  - 顺带:`dry_run` 也先 `catch_up_projection()`。不补的话预览数出来的关系条数和
    真跑那次对不上,而"先看一眼"正是这个参数存在的全部理由。
- **叙事记录里的发言人是 id,而那是玩家看到的第一行**(`narrative` / `user_message`
  的投影条目加 `speaker_name`)。线上原文:`mai：` / `bai：` / `nian：` —— 宿主拿
  `speaker` 去渲染,而它是 id。三级回落(payload → 名册 → id 本身),**永不为空**;
  老日志重放时补得出来。同一类还扫出两处:`agent_hail` 只带 `player_name` 不带
  `agent_name`(玩家收到的敲门通知上写着叫他的人的 id)、`agent_wants_contact` 的
  地点是键名。
- **同一个玩家在关系名单上有两个名字,其中一个是 uuid**(`World._contact_names()`
  按 `last_contact_tick` 挑,不再 `setdefault`)。**联系态是一个角色一行**,而人是会
  改名的 —— 折叠成 `player_id → 名字` 时先读到哪行就用哪行,而底下是 `HGETALL`。
  线上原文:「江晚和player-5688afd1还谈不上什么交情」,同一个人在别处叫「刘俊康」。
  - `_party_name` 的 docstring 早就写着「带 uuid 的人话和一个浮点数一样不能给人看」,
    并且**已经**把事件上抄下来的名字排在联系态后面,理由正是"那是那时候的名字"——
    而联系态自己内部有同一个问题,没人问过。
  - 同 tick 的两行按 `agent_id` 断,只为**同一个世界每次给同一个答案**:一个随读取
    顺序变的名字和一个错的名字一样难查。
- **"你手上的 X 不够"里的 X 是 id,不是那样东西的名字**(`apply_affordance` 收
  `item_name=`,`Scheduler.item_name_of` / `RedisEconomyStore.name_of` 供货)。线上
  一个全中文的世界印给玩家看的是「你手上的 notepad 不够:要 1 个,你有 0 个」,而
  这个世界自己的 `item_defs` 里 `notepad` 就叫「一叠点播条」—— 晚潮那一局 13 样东西
  全中(paint / soil / battery / coal / tar / bulb / token / redstring / chum / bone /
  oil / patch / notepad)。
  - **不只上玩家的屏幕**:`tools/body.py` 把这句话当 `ToolResult.error` 递回给她,
    于是一个说中文的角色会把 `notepad` 念出来。
  - **只在真要拒绝的时候查**(顺利那一路一次 Redis 都不多走),查不到退回 id ——
    "引用即存在"那条路上一样东西的名字**本来就是**它的 id,那时候印 id 是对的。
- **拒绝语里漏着引擎自己的词汇**(新增 `ontology.speak_expression`;`absent` 那句
  两个地名过 `Scheduler.place_name`)。跟上一条是同一个 bug 的另外两半:
  - `me_` / `world_` / `have_` 是**引擎的命名空间标记**,不是这个世界里的名字 ——
    作者声明的量叫「主动」,`me_` 只是"读她身上那份"的意思。线上原文:
    「你现在做不了这件事:\`me_主动 >= 1.2\` 不成立」。
  - `absent` 那句印的是**键名**:「它在 cart,你在 noodle」。`place_name` 这个函数
    本来就是为了修掉同一个病造出来的(判定器把 `cart` 写进了她的长期记忆),
    它的 docstring 里记着那条教训 —— 而 `perform_affordance` 这一处当时漏了。
  - **六个日历内置名**(`day`/`hour`/`minute`/`minute_of_day`/`now`/`dt`)也各有说法
    (`hour` → 「钟点」)。线上原文:「这会儿不行:「hour >= 23 or hour < 2」不成立」。
    它是前两条修完、重新上线、再当玩家把 15 个地点扫一遍时**剩下的最后一处**,
    而漏掉它有个理由:另外几个都带前缀,一眼看得出是引擎的;日历名不带,看上去
    就像作者自己写的量。反过来也成立 —— 作者**不可能**声明一个叫 `hour` 的量
    (`parse_kinds` 当场拒),所以这六个名字永远是引擎的,翻译不会撞上世界里的谁。
  - **只改名字,不改算术**:「你的体力 >= 40」里的阈值是作者写下的理由,抹掉它就
    只剩一句"你做不了" —— 而拒绝之所以教得会人,全靠它说得出还差什么。
    顺手把反引号换成「」:那是 markdown,玩家屏幕上就是两个撇号。
  - 替换走 `expression.names`(解析出来的自由变量)而不是拿正则去猜哪些字是标识符,
    并且从长到短、两头咬词边界:`me_体力` 不许啃掉 `me_体力上限` 的前半截。
    `and` / `or` / `not` **有意留着**:它们和 `>=` 一样是运算,在世界里没有另一个说法,
    而按名字换这条路走不到它们身上 —— 要换就得去正则字面量,那正是上一句拒掉的做法。

##### 又一轮:她刚聊完一整场,那一屏是空的

- **关系名单只有两种状态,而世界里有三种**(`relationship_summaries()` 并上联系态里
  说过话的那些对,每一行多一格 `met`)。关系判定跑在**对话关闭**的时候(默认静默
  600 秒才算关),而玩家聊完就去点关系那一屏。只列已折叠的对,三种状态就压成了两种:

        从没来往过    → 没这一行   ← 对
        结算过        → 有一行     ← 对
        来往过没结算  → 没这一行   ← **错,和"从没来往过"长得一模一样**

  他刚跟她聊完一整场,那一屏是空的,于是他学到的是**"聊天没有用"** —— 而这是恋爱
  陪伴产品里最要紧的一屏。
  - **提前判不是修法**:判定要花一次 LLM 调用,而一场没关的对话本来就还没讲完。
    正确的形状是把这第三种状态**说出来**:`met=True, exists=False`,由 `summary`
    诚实地写「说过话了 —— 这一趟来往还没在她心里落定」。**不写"刚"** ——
    判定可能几十个世界日都没跑过,而"刚"是个时间断言。
  - **判据是 `last_contact_tick` 在不在,不是"这一行存不存在"**:`note_fired`
    (她想起他了)也在联系态上写行,而那种行没有 `player_name` —— 拿它当"说过话"
    会让一串 uuid 出现在玩家的关系屏上,正是上一轮刚修掉的那个病。
  - **CLI 上没结算的那几行不印档**:`band` 是 sentiment 0.0 折出来的,印成
    「还谈不上什么交情」等于把"还不知道"说成一个结论。
  - **次序按 `agent_id` 断**:没结算的那几行 sentiment 全是 0.0,只按分数排的话同一个
    世界每次刷新给出的顺序都不一样,而名单跳来跳去和排错了一样难查。
  - ⚠️ 玩家那一屏此前**看上去是对的** —— 网站自己拿它那边的 `/conversations`
    (站库 MySQL,一轮聊完当场就有)补了一根平行的真相。补得越好,引擎这个洞越
    看不见:换一个宿主(运维台、CLI、创作台)它就原形毕露。这一修把那根平行真相
    收回引擎里。

##### 又一轮:那笔见面礼他领了四次,而整块能力面板从来没显示过

三件,两件是**同一个形状**:一层比另一层多包了一层,或者少记了一件事,而两侧的
测试都绿着。

- **"只给一次"的那个一次,挂在一件会过期的事情上**(`Projection.allowances`)。
  `_grant_player_allowance` 从前的判据是在场那一行**原先不存在**
  (`presence_store.create()` 报的 `fresh`)。3.2.0 把在场从进程内存搬进了带 TTL 的
  Redis 键(重启不该让世界忘了人坐在她对面)—— 那次搬家没人重新读一遍这句话,
  而它的含义就此从「他这辈子头一回露面」悄悄变成了「他这**一刻钟**里头一回露面」。
  挂机十五分钟再回来就是又一笔,**没有上限**。线上真的这样:晚潮的账本里
  `dogfood-2e7fbb4` 领了**四次** 60 块。
  - 危害不是"多给了钱",是**补满了的钱包不构成代价**:他永远不必掂量买哪一样,
    于是货架又成了摆设 —— 和当初加见面礼要治的病是同一个病,只是换了个方向。
  - 判据因此要挂在一件**不会过期**的事上:账本自己。见面礼是账本上的一笔
    `payment`(`reason: allowance`),而账本是**全量重放折出来的投影** ——
    折过一次就永远记得,重启、换进程、TTL 到头都还在。
  - **先补课再问**(`catch_up_projection()`):别的进程刚发过的那一笔,不折过来就
    看不见,而这一层正是多进程共用一个世界的地方。
  - **和 `balances` 同生共死**:`player_forget` 不清余额,所以也不清这一格 ——
    清了的话一个被忘掉又回来的人会带着旧钱再领一次新钱。
  - ⚠️ 原有的 `test_那笔钱只给一次` 单独跑是绿的:它连着露两次面,而那两次之间在场
    那一行一直在。**分得开这两件事的只有"中间过没过期"**,所以新测试显式过期一次。

##### 站点与浏览器(anima-site;引擎这侧无改动,记在这里是因为它是同一个病)

- **能力面板在真站点上一直是空的。** 壳给的是 `{"options": {...}, membership, …}`,
  而站点原样转发、`filter_candidates` 读 `payload["targets"]`、浏览器按扁的接
  `.data` —— 三层里有两层读了一个不存在的键,于是 `ActionPanel` 拿到的 `targets`
  恒为 `undefined`,**整块面板从来没渲染过一次**。前几轮为它做的每一样(读数、
  四类拒绝不合并、松一格的阈值)全落在一块不显示的面板上。
  - **没人发现是因为后端替身比真壳扁一层**:`StubWorld.player_options` 直接回扁的
    `OPTIONS`,五条断言全绿地验着一个不存在的形状。替身照 2026-08-12 对着晚潮真壳
    抓的那一份改回来之后,当场红了五条。
  - 没真漏出去过(壳自己也筛一道 `_redact_candidates`),但**一条不会扣上的安全带
    比没有安全带更坏** —— 它让人以为扣上了。
- **在场过期之后,只有 `/state` 那扇门把人放回去。** 补位挂在 `home_location` 上,
  而浏览器把 `/state` 和 `/options` 放进同一个 `Promise.all` —— 两个请求并排出发,
  谁先到不一定。于是同一屏上半截说他在咖啡车、下半截说他什么也做不了,而**下一次
  刷新又好了**:一个自己会消失的 bug,永远查不出来。`/options` 自己也补一次;
  判据是壳给的 `blocked == "unknown_player_location"`,不是"targets 空不空"
  (一个真的什么都没有的地方也是空的,那种时候不该把人挪来挪去)。

##### 再一轮:世界的规律里从来没有过一个人

- **`{"action": …}` 那半边规律对玩家整个缺席**(`World.player_doing()` +
  `Scheduler._settle_player_actions`)。规律层按"此刻在做什么"分支的选择器读的是
  `_current_action`,而那张表**只有一处写点**:行为树的跃迁。人没有行为树 ——
  于是这张表里从来没有过一个人。
  - 线上现场:晚潮 116 条规律里有 **15 条**按 `action` 分支,驱动 手艺 / 嗓子 /
    随和 / 主动 / 睡眠债。21 个角色的这几个量每 tick 都在动;而**每一个玩家**的
    随和 = 手艺 = 嗓子 = 恰好 `1.0`,`updated_tick` 钉在他进世界那一 tick
    (17157 / 19197 / … / 31965),NPC 那边是 32219。零报错、日志干净、面板每帧
    都把它当成活的画出来 —— 照跑,但给错东西。
  - 更坏的是**互补的两半对人是单边的**:`{"not_action": …}` 算的是"所有角色减去
    正在做这件事的",而人本来就有 `stock:agent:player:*` 那一行,所以他**一直**在
    这半边里。他只吃得到往下拖的那一条,吃不到往上走的那一条(晚潮的主动:
    NPC 1.14–1.36,玩家 1.007–1.019)。在一个恋爱陪伴产品里这句话的意思是:
    **他做的任何事都不改变他自己**。
  - **派生,不存储。** 三个来源都是当下的真状态:占着他的那件长过程(`:engaged`)、
    他在不在路上、他上一次开口离现在多久 —— 所以没有第二份真相要维护,也没有会
    过期的账。存一份"他上次说他在做什么"的话,一个关掉浏览器的人会在世界里永远地
    走下去。⚠️ **不从四个入口各写一句**(`player_walk` / `player_action` /
    `interact_with` / `chat`)—— 那正是"五处写点,挨个加等于给未来的第六处留一个
    静默的洞"那条纪律说的形状。唯一的写点是 `_chat_prelude` 里记一次"他开口了"的
    tick,而且落在静音闸**后面**:被拒之门外的那一句不算他在跟谁聊天。
  - 优先级是**约束由强到弱**:占用 > 赶路 > 说话 —— 和四类拒绝的排法同一条。
    赶路时他仍然说得上话(`_PLAYER_TRANSIT_OK`),但那会儿他主要在赶路。
  - 快照每 tick 取一次(tick 步骤 3.55,**早于规律那一步**):逐条规律去问的话,
    一个有十几条 `{"action": …}` 的世界每 tick 要多问十几趟在场名册。

##### 还没走到就买到了:两扇门问「他在哪」,问出两个答案

- **人在路上,却买走了他刚走开的那个货架上的东西**(`World._player_here`)。
  `_where_is` 的注释上一轮就把这条写死了 ——「在路上就是"不知道"(空串)——
  而不是"还在出发地"」,后面紧跟一句警告:**两处各写一遍的话,迟早一处认为在路上
  还算在原地**。那件事就这么发生了,而且发生在同一个文件里:能力那条路问的是
  `_where_is`(在途 = 不在,`test_他走了一半的时候不算站在任何地方` 守着),
  买卖两条路问的是 `player_location`(在途**有意**答出发地 —— `player_walk` 要拿它
  算路费)。于是同一个人在同一时刻,一扇门说他不在,另一扇门把货卖给了他。
  - 线上真按了一次:`in_transit: true` 的那一屏,四样货**全部** `available: true`,
    `POST /buy` 返回 `status: accepted`、钱包真的扣了。他起步之后可以站在路当中
    刷完全镇的货架 —— 而"走过去"那段路正是这个世界让他掂量的唯一代价,这一下
    整个作废,还不报错。
  - 更难看的是**一屏之内自相矛盾**:`player_shop` 一边诚实地写着 `in_transit: true`,
    一边把他已经离开的那家店整架摆出来。它自己的 docstring 早就承诺了
    「「他在路上」和「世界不知道他在哪」分得开 —— 两边 `location` 都是空的」,
    代码没做到 —— 文档说了而代码没做,是这个仓库最怕的那种不一致。
  - 修法不是在两扇门上各补一句 `if`,那等于把同一个错误再抄两遍:玩家这半边的
    位置从此**只有一个答案**(`_player_here` = 结算到达,在途则空),`player_shop` /
    `player_buy` / `player_perception` 三处一起改问它。`player_location` 原样不动 ——
    它答的是"他属于哪儿",`player_walk` 与 `_present_roster` 都**要**在途仍算出发地。
  - 顺带堵住的第三扇:`player_perception` 从前也读 `player_location`,于是赶路的人
    照得见他已经走出去那间屋里的东西。现在 `here` 那一档空了、`self` 那一档照旧 ——
    赶路的人仍然知道自己累不累。
  - 拒绝照旧走 `LookupError` 说人话(「你还在路上 —— 等走到了再买」),不坐
    `KeyError` 那班车,也不把地点 id 漏给玩家。

##### 玩家屏幕上印着 `in_transit`:那句人话没有主人

- **空菜单的理由多给一份人话**(`player_options` 的 `blocked_text`)。`blocked` 从来
  只有一份,而它是**给机器分支的枚举**(`unknown_player_location` / `in_transit` /
  `no_ontology`)—— 站点没有别的可印,就把它原样摊在能力面板上。线上真的这样,
  而且是在**两块并排的面板**上:货架那块自己写了「你还在路上 —— 到了再看」,
  能力那块印的是 `in_transit`。同一件事、同一屏、两个说法。
  - 这和每个动词那对 `reason`(枚举)+ `refusal`(人话)**逐字同构**,理由也是同一条:
    这句话没有主人的话,每个宿主自己译一遍,而译漏的那个会把标识符印在玩家脸上。
    ⚠️ 而**下一个宿主漏译的概率是 100%** —— 它不报错。
  - 每一句都要说得出**他下一步该干什么**:「你还没落个脚 —— 先挑个地方站过去」
    「你在路上 —— 到了地方就能动手了」「这个世界还没摆出什么摸得着的东西 ——
    先找个人说说话吧」。「你做不了」教不会他任何事,那正是恋爱陪伴产品里最要命的
    一种沉默。
  - **`_BLOCKED_WORDS` 那张表就是闸**:`test_每一个挡住的理由都配了一句话` 对着它
    逐格点名,新加一个枚举而忘了配话**当场红**。忘了配的那天没有任何一处会报错,
    只有玩家屏幕上多出一个英文标识符 —— 上一次就是这么来的。
  - 顺带把 `player_options` 的位置也换成 `_player_here`:此前它自己写了一句
    `player_in_transit` 判断挡在前面,判对了,但那是**第四份**「他在哪」的写法。
  - 世界壳无改动:`_redact_candidates` 原样透传整个 dict。

##### 一块屏幕的两半互相打脸:他明明在赶路,快照说他站着

- **`World.state()` 把一个在路上的人报成站在出发地,而且一格标记都没有。**
  角色那一半从来都有(`_agent_activity` 的 `activity.transit`),它自己的注释写着
  理由:「少了这一格,一个埋头做了十个月椅子的人在 `state()` 里看上去和闲着一模
  一样,而宿主的界面只认这里」。玩家那一半漏了 —— 又一对**有意分开写、却只有
  一半拿到了这个特性**的姊妹(上一轮是 `_settle_actor_place` vs
  `_settle_player_places`)。
  - 线上真是这样:他点了「走去澡堂」,屏幕上他还站在铁匠巷,而同一秒
    `player_options` 正拿「你在路上 —— 到了地方就能动手了」把他能干的事全挡了。
    **两半都不报错**,而站着的那一半是假的 —— `_player_here` 说他这会儿不在任何
    地方。玩家看到的是一个「站在原地却什么都点不动」的世界。
  - 病根是 `state()` 直接誊了在场那张表的**原始行**,绕开了名册视图
    (`_present_roster` 早就有 `in_transit`,`map_data()` 的 `standing`/`travelling`
    也一直是对的 —— 三扇门里只有最要紧的那扇漏了)。改成走名册那条路,顺带**先
    把到站的人放下**:只读门自己补课,暂停的世界不会自愈。
  - **`location` 不抹成空串**:调用方要分得开「他在路上」和「世界不知道他在哪」。
  - 换算收进 `_transit_view()` **一处** —— 她和他各有一份在途,两边各算一遍的话,
    迟早一处忘了 `world.minutes_per_tick` 不是 1,于是同一段路在角色那栏是 15
    分钟、在玩家那栏是 3 分钟,而两栏都不报错。

##### 世界壳绕开了快照,自己去问了第二遍(anima-platform)

- **壳的 `/state` 从 `world.players` 另取成员那一行**,而不是从快照里挑 —— 于是
  上面那个修法一格都到不了玩家屏幕上。两条路取同一个人,只有一条是对的。
  - **它此前测不出来**:壳那份替身世界的 `state()` 里**根本没有 `players` 这一格**,
    而真引擎一直有。替身比真货少的那一格,正好是壳该读的那一格 —— 于是壳走哪条
    路都全绿。这和上一轮「替身比真货完整时,测试全绿而真路径是坏的」是同一个病
    的**镜像**:替身和真货不一样,方向不重要。
  - 替身现在把真引擎那道分法演出来了(原始行 vs 名册视图,在途只写进在途那张表),
    新测试对着旧代码**当场红**。

##### 四处文档还在描述 3.1 的世界,其中一处劝人绕开一道已经修好的闸

- **REFERENCE 里「玩家的在场是**进程内**的、重启即失效」是 3.2.0 起的假话**
  (在场自 3.2.0 住 Redis + TTL,跨进程、扛重启)。四处逐一改掉,其中最坏的一处是
  `anima-world player options` 那条警告:它写着「另一个进程问永远给
  `blocked: unknown_player_location`,**这不是 bug**」—— 照着读的人会去给一个
  **已经修好**的洞加绕行代码,而且他不会回来复查。
  - 拿真门验过再写:容器里另起一个进程跑 CLI,它看得见线上玩家的位置,菜单是满的。
  - 顺带把那条命令重新定位成运维手里**最直接的那把尺**:玩家说「我什么都点不动」
    的时候,先问它。

##### 站点与浏览器(anima-site;引擎这侧无改动,同上,记在这里是因为它是同一个病)

- **能力面板把枚举印给了玩家**(`ActionPanel.vue`)。改印引擎给的 `blocked_text`,
  留 `|| blocked` 只为旧引擎兜底。**站点不自己译** —— 译了就等于把那句话又抄了
  一份,而抄本迟早和引擎那份分叉,分叉的方向必然是屏幕上那句更好看、更不准。

- **登录成功,下一个请求当场「登录已失效」。** FastAPI 0.106 起,带 `yield` 的依赖
  是在响应**送出之后**才收尾的,而站点 `get_session` 的 commit 就写在收尾那句上 ——
  于是 `Set-Cookie` 排在那一行落库前面。浏览器拿到 cookie、立刻带着它问一句
  「我是谁」,站点回他 401;再刷一次又好了。实测**零延迟必翻车、等半秒必正常**,
  一条日志都不留 —— 一个自己会消失的 bug,所以它像玄学不像故障。
  - 闸开在**路由这一层**(`CommitBeforeResponse`),不在每个写接口里各记一句:
    记在各处的纪律迟早漏掉新加的那个。凡是「我告诉你成了、你马上拿它去下一扇门」
    都在这个窗口里(报名一个世界之后立刻读 `/state` 是同一个)。
  - 守它那条测试**必须从 ASGI 那层进**:走 httpx 的话收尾早在 `post()` 返回前就
    跑完了,事后查库永远是绿的 —— 那正是它此前没被逮住的原因。

## [3.2.0] —— 往一个跑了五千条事件的世界里加十六个人 (2026-08-11)

CLAUDE.md 和 `__main__` 的注释都写着"给了 `--world-file` 就是一次**明示的编辑**,
语义是只填缺不覆盖"。**那句话是半假的** —— 只填缺的粒度是**整张表**,不是每一项。
于是往线上那个跑了 5347 条事件的世界里补一条规律、一个角色、一个地点、一个种类、
一件物品:**一样都没进去,而且一个字都没说**。这个仓库最怕的那种坏法。

### Fixed

- **作者层合并降到逐项粒度**(`--world-file` 那条路,且只有那条路)。四个 Redis store
  的 seed 入口加了 `merge=`(`RedisLocationStore` / `RedisOntologyStore` /
  `RedisEconomyStore` / `RedisRulesStore`),外加 `_seed_stocks` / `_seed_stock_visibility` /
  `_seed_stock_places` / `BTStore.seed_defaults`。**已有的一项都不动,文件里多出来的补进去。**
  - ⚠️ **`_seed_stocks` 的粒度是每个 (owner, 量名),不是每个 owner。** 按 owner 判的话,
    一个已经有 `季节` 的 world 永远补不进 `气温` —— 而它不报错,只是那个量从此恒为 0。
    拼错的量名照样**当场开不了机**(那道闸在跳过之前)。
  - ⚠️ **`merge_author = authored_file and not fresh_world`**,和 `seed_author_layer` 分开写。
    内置兜底那份(每次开机都在引擎手上的橱窗)永远走不到合并这条路 —— 否则每次重启
    都会把橱窗的橡树往别人的世界里掺一点,世界照跑、日志干净。
    `test_author_merge.py` 两半各钉一条,**只钉前一半等于把那个门重新打开**。
  - `_apply_seed_config_at_genesis` **有意仍然只在创世跑**:运维台在运行期调过的
    `tick_rate` / `enforce_colocation` 不该被一次内容编辑倒带回文件里的值。
- **新角色真的进了这个世界,不只是文件里的十六段文字**(`_join_authored_additions`)。
  名册、位置、关系、随身物品、钱**全是事件的投影**,所以合并要发 `agent_join` /
  `location_join` / `payment`,而不只是往表里写行。
  - ⚠️ **`ts` 必须大于 0**:重启时捡回"中途加入的人"那一段靠的是 `agent_join` 且 `ts > 0`。
    钉成 0 的话他们会被当成创世名册的一部分,而创世名册在有事件的世界里根本不被读 ——
    于是这些人**只在装文件的那一次开机里存在**,下次重启整批消失。
  - 关系只发**至少有一头是新人**的那些(`_seed_relations(require_new=)`):`state_change`
    是覆盖,拿文件里的初值重发两个老角色的关系 = 把三十天的交情倒带回创世那一刻。
  - 新人的创世记忆要**自己折一次**(`_fold_seeded_memories`):`MemoryStore.rebuild`
    见了非空表就掉头(记忆是持久状态,重放一遍等于把她的一生按今天的触发器重裁一遍),
    所以在那条路上新人的记忆永远是"日志里有、库里没有" —— 她开口时对自己的过去一无所知。
    只在表已非空时折;空表时 `rebuild` 会连他们一起折,两边都折就是每人两份。
- `_precheck_ontology` 合并时拿**库里那份 ∪ 文件里那份**去校验(`_union_by_id`),
  否则一份只写新种类的补丁会因为"引用了库里已有的东西"而被判非法。
- `_seed_memories` 的 "unknown agent" 警告不再对着老角色喊:「这个人不是新来的」和
  「这个人根本不在这个世界里」是两回事,合成一条的话每次合并都刷一屏假话,
  而人一旦学会忽略它,真的那句也一起被忽略了。

- **动作表按人分家了(`bt_actions`)—— 两个人的班表重名时,后播的那个不再改写先播的。**
  `bt_nodes` 一直按 `(tree, node_id)` 存,而动作表是**全世界一张**(`set_action(node_id, …)`)。
  于是两个人只要给自己班表上的某件事起了同一个名字,后播种的那条就把先播的**整行**
  改写掉。而重名在作者层是**常态不是错误**:「回铺子」「收摊」「去码头」本来就是
  好几个人都会做的事 —— 线上那个世界早就有三个人共用「睡」,一直没出事只是因为
  三条的内容恰好一模一样。往它里面加十六个人时当场撞出五处语义不同的重名:开咖啡车的
  年因此不再去 `cart`,改成去江堤上"上班"。**树建得起来、动作查得到、日志一行不错,
  只是人走错了门** —— 要盯着某个 NPC 看一整天才发现。
  - `action_table(tree)` 改成**两层**:没有人称的共享绑定(`go_to_*` / `chat_with_*` /
    需求动作)打底,这个人自己的绑定盖在上面。`seed_duties` / `seed_tree` 写的是
    后者,`seed_defaults` / `_ensure_need_actions` 写的是前者。
  - **老世界一个字都不用动**:共享绑定的字段名仍是裸 `node_id`,读出来 `tree` 缺席
    即共享 —— 老引擎写下的行全部落回今天的行为。
  - "这行有没有"一律问 `shared_action_ids()`,别问 `actions()` —— 后者现在还装着
    别人名下的行,拿它判会把「张三有一个叫 X 的班」读成「X 已经播过了」。

---

### 再一轮:窗她擦得了,我擦不了

四个仓库一起上线,拿真的浏览器和真的模型把线上那个世界当玩家玩了一局。四件,
前三件是"同一个世界里两套物理",最后一件是这个仓库最怕的那种坏法。

- **`cost` 这个字段名在撒谎。** 声明写的是 `costs: {"体力": "me_体力 - 4"}`,
  而 `set`/`costs` 的表达式一律算出**新值**——那是"还剩多少",不是"花了多少"。
  回执上却叫 `cost`,于是宿主照着它画"这一下花了 95 点体力"。改成
  `me_changed`(新值)+ `me_delta`(差额),两个都给,名字各自说的是各自的事。
- **玩家用不了世界里的能力**(`interact` 进 `PLAYER` 面)。本体层这一轮长出了整层
  `interact`:她读得到"这儿有扇窗,可以擦一擦",挑得动那个动词,擦完真的少一把
  力气;而玩家那侧的菜单上只有"说句话"和"走过去"。**世界的说服力全在"我做的事和
  她做的事是同一件事"上。** 补的四样各守一条:他身上也有量(`agent` 那份声明对他
  一样生效,落在 `_touch_player`)、"他在哪"只有一个答案(`_where_is`)、扣账不分人
  (一起做的事从前先把玩家滤掉,于是他白干)、菜单要说得出前提
  (`requires_target_entity` / `with`)。`tests/test_player_affordance.py` 一律走
  `World.player_tool()` 那条**真路**。
- **她看不见别的玩家。** 在场块的 `others` 只从 `scheduler.agents` 里拼 —— 同处一室
  的**其他**玩家在她眼里不存在。两个人站在同一间屋子里,她只认得其中一个。
- **邀请判定从来没被走到过,而健康位上写着"多半是没配 key"**
  (`SyncLLM.complete_sync`)。它一律 `asyncio.run()`;引擎自己那几条线程上没有循环,
  所以一直是对的,而**跟着一次玩家请求走的判定不在那几条线程上** —— 宿主的请求
  处理器是 `async def`,那条线程上已经有一个循环,`asyncio.run` 在那里是
  `RuntimeError`。上游一律 `except Exception`(理由正当:一个死掉的 LLM 不该停住
  世界),于是它安静地退回确定性启发式,而一个刚进来的访客在那条路上对谁都是 0.0 分、
  门槛 0.2:**他请不动任何人做任何事。** 线上那个世界 33MB 的 LLM 日志里,邀请提示词
  出现过 0 次。有循环时借一条线程跑,不抛。
  - 是上一件(玩家用得了 `interact`)把这条路第一次走通的 —— 在那之前没有玩家发得出
    邀请,所以这个洞在事件循环那侧从来没被撞到过。
  - 和 `_BridgeLoop` 那条纪律不冲突:那一条管的是**复用连接池的聊天热路径**,而这个门
    本来每调一次就开一条新循环(判定线程上也是这样)。

修完上面那条,判定器第一次真的被问到 —— 于是又看见五件。**都是把拼好的字逐字读出来
才看见的**(前三件读的是提示词,第四件读的是聊天窗):前三件是判定器手里的东西不对、
第四件是玩家读到的东西不对,而世界照跑、日志一行不错;第五件是宿主够不着:

- **她收到的邀请是一句读不通的话**(`together.describe_invitation`),两处,都是
  把作者写的 label 当零件拼:
  - 句子模板写死了「叫你**一起**……」,而作者给动词写的 label 常常自己就带着
    「一起」—— 晚潮世界十五个共同动词里有三个是。于是她读到的是「阿布叫你**一起
    一起**喝一杯」。动词自带就不再加。
  - 东西的名字直接焊在动词后面(`{verb_label}{target_name}`)。而 label 本来就是
    一句完整的话,后面再接一个名词就成了「阿布叫你一起**树下坐会儿江堤上的老樟树**」。
    改成东西先出场:「阿布**指着**江堤上的老樟树,叫你一起树下坐会儿」—— 两种 label
    都读得通。
  两条都不报错,而判定器就照着这句读不通的话去判她答不答应;她下一句也照这个语气
  回你。**是拿真模型跑一次、把提示词逐字读出来才看见的** —— 第二句在上一条修好、
  判定器第一次真的被问到之后才露面。
- **判定器判"熟不熟"时,手里没有他俩刚说完的那两轮话。** 记忆是**会话关闭那一刻**
  才落的,而邀请正发生在会话中间 —— 于是跟她聊得好好的,一叫她就说不熟。补上
  `recent_talk`(邀请人就是这场会话的那个玩家时才取;NPC 之间没有转录可读,不去白跑
  一次 IO),在提示词里是自洽的一块:没内容就整个不出现,老世界覆盖过的模板照样
  渲染得出来。同一处还改了记忆的取法:**按相关性召回,不是按新鲜度** —— 拿邀请人的
  名字当 query,和聊天那侧 `world_context` 同一条路。此前挑最近三条给出来的常常是
  关于**另一个人**的事,模型读着三段无关的记忆,得出"不认识"。
- **她自己的记忆里写着她的 id 和两个英文词**(`memory_triggers._on_agent_state`)。
  摘要是 `f"{agent_id} 的状态从 {old_status} 变为 {new_status}"` —— 于是判定器读到的
  「郭大夫最近记得的事」里有一件是「guo 的状态从 sleeping 变为 working」。改成
  「我开始干活了」。**同一课这个文件里已经记过一遍**:`_relation_names` 的 docstring
  写着"一条写着 id 的摘要传出去就是一句没人看得懂的话",因为八卦把摘要原样转述 ——
  隔壁那个函数照做了,这个没有。引擎不认识的状态原样留着,不替作者编一个中文词。
  - **而那个"记过一遍"的函数自己也只做对了一半**(`_on_sentiment` /
    `_on_sentiment_delta`)。它把两头的 id 换成了名字,却留下另外两样:摘要写的是
    **第三人称**(「周叔 对 何师傅 的关系……」,而这条记忆的主人 `agent_id=as_id`
    就是周叔本人 —— 她在自己的记忆里读到自己的名字),后面还跟着**引擎的账**
    (「(+0.80→+0.85)」「(Δ=0.85)」)。人不会记得自己对谁的好感度从 0.80 涨到
    0.85。改成「我和何师傅从「亲近」变成了「挚交」」/「我忽然觉得阿檀疏远了很多」;
    八卦转述出来是「听周叔说:我和何师傅……」,正是一个人转述另一个人的样子。
    是修上一条时顺着线上那 559 条记忆翻出来的。
- **拒绝的话写成了调试转储,而它直接进玩家的聊天窗**
  (`perform_affordance` 的 `unknown_entity` / `unknown_verb`)。线上读到的原文是
  「(这儿没有 树底下 这个东西;有的是 ['awning:cart', 'bench:barber', …])」——
  一串 Python list 的 repr,而那十个 id 是**整个世界按字母序的前十个**,跟她面前
  有什么毫无关系:玩家照着挑一个,多半还是够不着。改成报她**手边**那些
  (`_here_menu`,和地名那侧 `places_menu` 同一个「名字(id)」写法);列不出来时
  就不列,不断言「你这儿什么都没有」—— 她在路上时也是空的,那句话会成为谎
  (`absent` 两头都说,同一条理由)。`unknown_verb` 同理:报人话那份动词,
  她读到的本来就是那几个字。这条**逃过了一直都在的两条断言**,因为它们验的是
  `reason == "unknown_entity"` —— 那是分类对不对,不是这句话是不是人话。
- **会话摘要里她自己叫 `wan`**(`chat_session._speaker_labels`)。玩家那一头早就不许
  漏 id 了——那个函数的 docstring 用整整一段解释了为什么退「访客」也不退 `p1`——
  **而她自己那一头的兜底直接就是 `agent_id`**。转录行上只有 id:`start_conversation`
  只给玩家那条抄名字,她那条从来不带。于是线上记忆库里躺着「店主 wan 表示唱机转但
  不稳」「yun 因江堤上没有树荫且地面湿冷」「被刚睡醒的 bai 听见」,而摘要进她的长期
  记忆、进下一场的记忆块、还被八卦原样转述。最离谱的是她读完之后自己写下的那条反应:
  「隐约对那个 'wan' 有点好奇」—— **她对一个字符串产生了好奇**。改成从名册查
  (`agent_name=`,由 `api.py` 注入,和地点那一半 `place_name=` 逐字同构),查不到才退
  泛称「她」。**不往转录行上补抄一份名字**:名册是她名字的唯一出处,抄一份就是两份
  真相,而且读那侧修好之后连库里已经存着的老行也一起读对了。
  ⚠️ 这条同样**逃过了一直都在的断言**,而这次的原因换了个花样:
  `assert "夏" in llm.system` —— 名册里她叫「苏晚夏」,`夏` 是它的子串,所以那条断言
  在只印 id 的那些天里也是绿的。新测试按**行首**问「谁开的口」。
- **判定器手里的地点是键名**(`_submit_hearsay_judgment` / NPC↔NPC 判定 / 玩家会话
  判定 / 邀请判定,四条路一样)。它拿到的 `地点:{location}` 一直是 `cart`,而它写出来
  的那句 `summary` **原样落成她的长期记忆**:线上原文「舒白回江渡录电台选题,两人在
  cart 碰面」—— 那地方叫小念咖啡车。名单那一头早就不许漏 id 了
  (`test_her_reaction_lands_as_a_real_relationship_change` 里就写着
  「判定那一层不该碰得到 id」),地点这一头漏着。新增 `Scheduler.place_name()`,
  四条路一起接上;`_planner_situation` 里那份一模一样的三行也并了过来 ——
  两份拷贝正是判定这条路当初漏掉的原因。
- **世界动态里她叫 `chi`,地方叫 `studio`**(`MockNarrativeProvider` /
  `OpenAICompatibleNarrativeProvider`)。模板 `{agent}在{location}忙着` 的三个占位符
  (`agent` / `location` / `target`)一直是拿 id 填的,而**那行字就是玩家在世界动态里
  逐字读到的正文**:「chi在studio忙着,雨声盖过了别的动静」「nian冒着雨往yard去」。
  没配 key 时它还是每个人看到的第一屏。LLM 那条也一样:喂进去的
  `agent_id=` / `location=` / `action_params` 全是键名,而模型会把喂给它的字抄进正文。
  两个叙事器共用 `_NamesMixin`,由 `Scheduler.__init__` 后绑名册(叙事器是构造参数,
  比名册早出生),**降级那一份跟着一起绑** —— 它才是模型不通时真正在写字的那个。
  ⚠️ 这条被一条现成的测试**当作正常放过了**:
  `test_the_first_screen_speaks_the_worlds_language` 的注释里写着「地点 id 本来就是
  英文(`走去了cafe` 完全正常)」。
- **`World.player_perception` 在 `/internal/v1` 上没有出口**(运维台世界镜像新增
  `GET /internal/v1/perception`)。`player-tools` 只说得出"有 interact 这个按钮",
  说不出这一屋子里有什么、那样东西认哪几个动词;宿主只能自己拿 `entities` + `kinds`
  拼一份动词表,而**拼错了不报错**,按钮点下去才发现世界不认。身份只从 claim 里取 ——
  收 query 参数等于让调用方指定"我以谁的身份看",而可见性整层是照这个身份算的。

### 又一轮:她排一天的事,依据是半句人话

上面那一批修完、重建镜像、重新部署,再当玩家玩一局 —— 聊天那侧干净了,而**把她收到的
规划提示词逐字印出来**又看见两件。第一件是同一课的第七遍,第二件是健康表自己把它存在的
全部理由抵消掉了。

- **规划提示词里的地点是键名**(`_planner_situation` 的 `others`)。线上原文逐字躺着:
  「这会儿别人在做什么:林迟在studio上班;苏念在cart上班;江晚在levee赶路」——
  **名字翻了、地点没翻**,半句人话半句键名。而这一句是她排一天事情的依据:她照着它
  决定去哪儿找谁,而 `walk` 的候选清单本来就是 id,两种写法混在同一份提示词里只会让她
  更分不清哪个是地名。接上 `Scheduler.place_name()`(上一批为四条判定路新增的那个)。
  ⚠️ 一直都在的 `test_a_live_world_fills_the_block_in` 盯不住它:它只问了
  `ctx["others"]` 是不是非空。新测试逐条查每个地点 id 有没有漏进那句话里。

- **`planner: ok 0 / degraded 1071` —— 一个数字底下压着两个 bug。**
  `note_subsystem` 的 docstring 写着它存在的全部理由:让「一个整整三天没有 planner 的
  世界」和「一个角色确实无所事事的世界」在健康表上长得不一样。而它自己把另一对混成了
  一个,还把第二个 bug 藏在了后面。
  - **一个日程排满的人不是一次规划失败。** 晚潮 21 个角色里 17 个被节拍排满了整天,
    `make_plan` 在**调 LLM 之前**就返回 `None` —— 而调用方一律记成一次子系统降级。
    于是这个世界的健康表和"planner 挂了三天"的那个世界逐字相同。拆出
    `Planner.plan_inputs()`(今天有什么可排:空窗 + 动作空间),两个都非空才谈得上
    规划,也才谈得上成败;算过就传给 `make_plan`,别算两遍 —— 两处各算一次就是两份
    判断,迟早给出不同答案。
  - **失败什么都不留下,于是每 tick 重排一次。** `_request_replan_if_needed` 从前只问
    「她今天有计划吗」,而一次失败既不落计划也不记账 —— 线上一个进程 61 tick 攒了
    1071 次规划(≈17.5/tick,正是那 17 个人),`subsystem_health` 在 ok↔degraded 之间
    抖了六个来回、394 条,**日志被自己的健康报告淹掉**,而那正是 `note_subsystem` 的
    docstring 承诺不会发生的事。改成按**试过了**记(`_plan_attempts`,agent → 哪一天),
    一天一次。今天它只是白转,因为那些人在调 LLM 之前就返回了;**LLM 真挂掉的那天,
    这就是一条对着付费接口每 tick 一次、没有任何退避的重试风暴。** 代价是一次抽风
    赔上她这一天的计划 —— 那正是模块头上写着的地板:没有计划就退回 `idle_wander`,
    和没有规划器的世界一样。
  - ⚠️ `_make_and_install_plan` 里**不许提前 `return`**:尾部那把锁负责把她从
    `_planning` 里拿掉并叫醒 `wait_planning_idle`,提前走等于让 `simulate` 的快进
    永远等一个不会回来的人。

- **上面那条只修了半个,而半个修法比不修更难看**(`_format_space`)。修完重新上线、
  把提示词再印一遍,同一份里现在长这样:

      - 这会儿别人在做什么:江晚在江堤闲着;程屿在老陈辣鱼面吃东西;…
      - chat（target 可选：nian/wan/yu/bai/…）
      - walk（location 可选：alley/barber/…/levee/…）

  上半句说人话、下半句是键名,**两半之间没有任何桥** —— 她读到「江晚在江堤」,
  却得自己猜二十个 id 里哪个是江堤、哪个是江晚。抓到的现场是她干脆把 id 当人话用了:
  线上一条计划的 `note` 写着「早点到studio等她」,而 `note` 正是给人读的那半句。
  清单改成和别处同一个写法「名字(id)」(`intent.places_menu` / `_here_menu` 早就是
  这么写的),提示词里补一句"params 填括号里那个 id,note 里写名字"。
  - `action_space` 的返回值**仍然只装 id**:它是 `validate_steps` 那道闸的判据,
    不是文案。名字只在渲染那一层出现。
  - `validate_steps` 收下「江堤(levee)」并收回成 `levee`(`_unlabel`)。**这不是把闸
    放松了**:判据从头到尾还是"这个 id 在不在我给她的清单里",收的只是**我自己刚
    印给她的那个写法**;括号里塞一个清单外的东西照旧丢掉。理由和 `start_min` 上界
    那条一模一样 —— 换了写法却仍按老写法验,代价是她那几步安静地没了,而没有人
    会去看那行日志。
  - ⚠️ 这条的第一版测试**是假的**:它直接调 `_format_space(space, planner._space_names())`,
    于是"`_build_messages` 忘了把名册传下去"这一整类漏法一条都验不出来 ——
    把修法抹掉它照样绿。改成问**她真收到的那份提示词**。

### 同一条缝,又漏了四处:我用一种写法印给她,却按另一种写法验她的回答

上面两条修完重新上线,以玩家身份又玩了一局。对话本身干净(记忆、事件、计划的
`note` 里泄漏的地点键名扫下来是 **0**,planner 的健康表也终于是 `ok 1 / degraded 0`)。
但把 LLM 流水翻出来按"她收到的是哪一版提示词"分组之后,当前这一版的 **204 次规划、
867 步**里,还躺着四处同源的漏 —— 每一处的代价都是**她那几步安静地没了**,而世界照跑、
日志干净。

- **空窗印钟点,却问她要分钟数**(`_format_windows`)。`yu` 的末窗印作
  `- 23:30 到 24:00`,她写回 `"start_min": 2330` —— 就是我印给她的那四个数字去掉冒号。
  三步全被判成"今天以外"丢掉,她傍晚安静地空了。上一轮补的那句"start_min 最大 1439"
  **没拦住**,因为缺的从来不是一句说明,是**桥**:现在每一行自己带着分钟数,
  `- 23:30 到 24:00（start_min 填 1410 到 1439）`。
  - ⚠️ **有意没做**"把 `2330` 当成 23:30 收下"。看着和 `_unlabel` 是同一招,其实不是:
    上一轮真见过 `start_min` 写成 1440 / 1500 / 1530,那批是**分钟数**(24:00 / 25:00 /
    25:30,窗口末端往后溢出)。同一个 `1500`,一读是 15:00 一读是 25:00,**两读都讲得通** ——
    收下它就是把她夜里那一步搬到下午三点,比丢掉更坏。`_unlabel` 收的是**我自己印
    给她的那个写法**,这里没有那样的东西可收。
- **字段名写成 `start`**(`validate_steps`)。867 步里 9 步这么写。单看是 1%,但它
  **按整份计划聚堆**:`bai` 三步全写 `start`,于是 `planner produced no usable steps`,
  她一整天没有计划。字段叫什么只是形状,闸(在不在今天以内、参数在不在清单上)一个字没动。
- **她把复合 id 从冒号处劈成一半**(`Scheduler._resolve_here`)。感知那行印的是
  `黑子[cat:hei]`,她写回 `hei`;另一行印的是 `剃头铺墙上那口[clockwall:barber]`,
  她写回 `clockwall` —— 取哪一半还没准,两次都换来一句"这儿没有它",一轮自主白费。
  现在认得出来,而**候选集就是她此刻够得着的那几样**(`_here_menu` 印的正是这一份):
  对不上、或者对上不止一个,照旧拒绝。`bench` 在剃头铺、诊所、渡口各有一条,那种时候
  猜哪一条都是替她编。
- **问路那半还在吐 Python 字面量**(`tools/body.py` 的 `walk`)。
  `没有 月球 这个地方;有的是 ['awning:cart', 'bench:barber', …]` —— `_here_menu`
  早就为同一个理由改成人话了,`walk` 这半漏着:同一个玩家在同一个聊天窗里,
  问东西读到人话,问路读到裸 id。改走 `places_menu`,和别处同一个写法。
  - 逮住它的是**已经存在**的那条测试的孪生兄弟:`test_拒绝的话是给人读的_不是调试转储`
    把 `interact` 那半的 `[`/`]`/`'` 全禁掉了,而 `walk` 那条只问了"id 在不在错误里"。
    判据写窄了一格,漏的就正好是这一格。

### 回执只说世界的事:"我没读懂你的话"不是世界的事

上面四条修完重新上线,再玩一局。空窗那一行现在真的带着分钟数
(`- 21:30 到 22:00（start_min 填 1290 到 1319）`),她答得也在状态里。但第二轮的
**第一行**是这个:

    (一起做什么?说具体一点 —— 得有个东西。)

玩家那句话是「我想去看看你说的那个地方,能带我去吗?路上顺便聊聊」—— 再清楚不过的
人话。分类器判成 `together`(0.85)却把 `object` 留空(`detail` 里倒是写着"带玩家去
潮汐里3号,路上聊天"),于是引擎越过她,当面责怪玩家没说清楚。而紧接着她的散文回答
好得很,还真答应了下楼:那句回执是**纯多出来的噪音**,并且它一个字都没告诉玩家这个
世界的事。指挥的要是**别人**,就更坏 —— 那条路上 `handled=True`,于是这句责怪
**就是她这一轮的全部回复**,她一个字都没说。

分界线是一句可以当场问出口的话:**这句回执告诉玩家的是这个世界的事吗?**
留下的那些都是,而且必须留:「世界里没有哈尔滨」「你在这头他在那头,这件事得当面」
「他手上没有那样东西」「果子还没熟」—— 每一句都教给玩家一点世界的规矩,抹掉它们
就是让他对着一个永远猜不透边界的世界瞎试。`empty_object` / `empty_detail` /
`unknown_player` 这三条一句都不是:它们说的是分类器**没把自己的字段填全**,
而玩家根本不知道有个分类器。

修法不是新发明的,是**把上面两个分支早就在走的那条路补上**:`style_adjust` 少了
kind/value 时正是记一行日志、按对话处理、玩家什么也看不见。导演这条漏了同一手。
现在 `DirectorOutcome.underspecified` 认出这一类,`_dispatch_intent` 原样退回对话 ——
她照常说话,一个字不被顶掉也不被加料。这几条拒绝都在任何一次世界写**之前**返回,
所以退回是干净的:没有半件已经落地的事需要回滚。

- ⚠️ **这一条推翻了一条被测试钉住的旧行为**(`test_act_without_a_detail_is_refused`
  断言 `"说具体一点" in reply`)。钉的时候它看着像"降级不许无声";而真到玩家眼前
  才看清那句话不是说给玩家的,是说给写指令的那一方的,**而玩家不是那一方**。
  换成了两条:一条钉退回对话,另一条钉「你去哈尔滨」那半照旧露出来 —— 只写前一条
  等于把回执整个关掉,而那才是真的把世界的边界藏起来了。
- 顺带查清两个**不是** bug 的:`〔stance:test〕` 里的 `test` 是枚举里的「试探」,
  不是模型自造;分类器报的 `come_here` 也在动词表上。

### 兜底要和它兜的那条规则说同一件事:「老陈他转身往灶台走」

同一局里读到的第二样。线上转录里逐字躺着

    （老陈他转身往灶台走,顺手把那台红灯牌的音量又拧小了一点。）

而模型写的是`（他转身往灶台走……）`—— **那个名字是引擎加的**
(`_ActionNameNormalizer`)。没有人这么写中文,而它撞得并不稀罕:量下来是模型写的
动作块的 **10%**(线上 108 块里 11 块)。

冠名这件事本身要留着,理由很硬:多人在场时`（顿了顿。）`不说是谁顿了顿,
`（林迟顿了顿。）`说了。错的是**加法** —— 一律往前摞,于是主语成了"名字+代词"。

有意思的是提示词里那条规则**早就写对了**:`chat.response_format` 第 2 条要求
"括号内描述当前角色时必须直接使用角色名,不要用『我』『她』『他』代替角色名" ——
说的是**换掉**。兜底那一层却在**摞上去**。规则和它的兜底各说各的,于是模型每漏一次,
兜底就把它兜成一句更难看的话。现在两边同一件事:代词顶掉,`（他打了个哈欠……）`
→`（林迟打了个哈欠……）`。

- `他们`**有意排除在外**:「他们都笑了」的主语不是她一个人,而「林迟他们」在中文里
  正好是"林迟一伙" —— 那一种摞上去反倒是对的。`的` 不必排除:
  `（他的手在抖。）`→`（林迟的手在抖。）`。

### 同一份清单,两个读者,却只按一个读者写

上面两条修完再上线玩一局,两条都验干净了(线上 12 个动作块 0 个「名字+代词」,
那句「能带我去吗」这回原样走了对话)。而这一局读到的第三样在同一句回执里 ——
我说了句「你去哈尔滨吧」,她那一轮的第一行是:

    (没有 哈尔滨 这个地方;有的是 铁匠巷(alley)、剃头铺(barber)、江渡浴室
    (bathhouse)、修船棚(boathouse)、小念咖啡车(cart)、……、念姐的小院(yard)。)

二十个拉丁字母的 id,铺在一个中文世界里她开口的第一行。按上一条刚立的那把尺
(**这句回执告诉玩家的是这个世界的事吗**)量:地名是,id 不是 —— 那是引擎自己的
记账。更坏的是它们**看着像要他照着打的东西**,而他打人话就行:`resolve_place`
第一层匹配的正是名字。

同一个函数的**成功**回执从来只说人话(`({name}往{place_name}去了。)`)。
一进一出两种写法,错的是失败那半。

`places_menu` 的 docstring 原本写着"给玩家看的那份",而它同时被 `tools/body.py`
的 `walk` 用着 —— 那一处**必须**带 id(`walk` 只收 `point_ids()`,不给就是让模型
接着猜)。所以这不是"该不该印 id",是**一份清单有两个读者,而代码只认得其中一个**。
现在 `with_ids` 把读者分开:模型那份照旧,玩家那份说人话。

- 重名的那几个照旧带 id:`小院、小院;说准一点` 是一句没法照着做的回执,而
  "说得出该怎么办"正是回执存在的理由。**只给重名的带** —— 一个重名不该把其余
  十九个也打回原形。
- 名字缺席时给 id:那时候 id 就是它在世界里唯一的称呼,印一个空字符串等于把它藏起来。

### 他一个地名都没说,而引擎回他「世界里没有它」

同一局的第二样,在同一个函数的隔壁。我说的是:

    带我去个安静点的地方吧,我想跟你说会儿话

分类器判了 `narrative_direction` / `go`,却把提示词里写着**必填**的 place 留空 ——
于是那一轮的第一行是:

    (没有 你说的那个地方 这个地方;有的是 铁匠巷、剃头铺、……、念姐的小院。)

这句话本身就不通。他一个地名都没说,那就没有"世界里没有它"这回事可报;引擎越过她
去责怪玩家,而**他问的本来就是"你挑一个"**。更糟的是那二十个地名铺在她开口之前,
顶掉的正是他真正要的那个答案。

这和上一版刚立的 `empty_object` 是同一件事(参数不全 → 不是世界的事 → 降级走对话),
只是 `go` 的 place 漏在了名单外。补 `empty_place` 进 `UNDERSPECIFIED_REASONS`。
判据照旧那一条:**回执只说世界的事**。

### 她说了走,世界没动 —— 而没有任何一层会发现

第三样是这一局最贵的。我跟程屿(唱片店老板)说了同一句「带我去个安静点的地方」,
他答应了,还自己挑了地方:

    潮汐里3号。我那儿。……(程屿拉下卷帘门,锁扣咔哒一声。)……二十分钟。

下一轮他的散文里人已经摸黑上到三楼掏钥匙了。而世界里他还站在唱片店(`loc=records`),
一条日志都不报错。对照组是同一局的苏念 —— 她说要去江堤,世界里真的把她挪去了 `levee`。

差别不在模型,在**他手上有没有那个词**:`walk` 声明的面是 `(BODY, PLAYER)`,
独独没有 `CHAT`。聊天面上只有 `walk_away`(离开这场对话),没有"去某个地方"。
所以那两轮散文不是他在撒谎,是引擎没给他兑现的路 —— 正是 issue #15 那句话本身
("说'我走了'也没真走"),只是漏在了另一个动词上,漏了两个版本。

- `walk` 加进 `CHAT` 面。
- 地名收**人话**:他写的是"潮汐里3号"不是 `flat`。原先只认 `point_ids()`,
  于是他第一次调用必然失败 —— 又一次"用一种写法印给她,却按另一种写法验她的回答"。
  改走 `resolve_place`(第二层照旧认 id,老调用方一个字不用改);对得上好几个就
  拒绝而**不猜** —— 猜错了她真的会走过去,而世界里一行日志都不报错。
- 不存在的地名给的是 `places_menu` 而不是 `sorted(known)`(一串裸 id 的 Python
  list 字面量)。`_here_menu` 早为同一个理由改过一次,这是漏网的那半边。
- 顺手逮到同一条缝的**第四个落点**:`walk_away` 的 `to_location` 把她写的那几个字
  原样递给 `move_agent`,而那一层只认 id —— 她写「江堤」收到的是一句光秃秃的
  「没有 江堤 这个地方」,连有哪些都不说,等于让她再猜一次。两处合用
  `resolve_location`,因为**拒绝的那句话必须一模一样**:同一个玩家在同一个聊天窗里
  问路,不该因为她挑了哪个动词而读到两种写法。

这条改动撞翻了一道**有理由的闸**(`test_body_verbs_stay_off_the_chat_menu…`:
"日常动词归行为树按排班和需求带管,摆进菜单等于开第二个不商量的入口")。那道闸
举的例子是**睡觉**,而它对 `eat`/`sleep`/`work` 成立(需求带上的动作,从聊天里
触发等于让玩家直接充她的需求条),对 `walk` 不成立 —— 而且这道门早就被 `walk_away`
走通了,它一直在 CHAT 面上、一直真的把人挪走。所以差别从来不是"能不能从聊天里
走路",是**"走完还能不能接着说话"**。排班表照旧在下一个时段把她带回去,那是它该
做的事,不是它被绕开了。闸没删,收窄成 `body & chat == {"walk"}`,理由写进 docstring。

⚠️ 给这条写测试时撞见一个**假绿**,值得记下来:`tools.call` 根本不按面过滤 ——
面只决定 `prompt_menu` 印哪几行。所以脚本里硬写一句 `〔tool:walk〕` 的测试,
在没给 CHAT 的引擎上照样跑得通、照样把人挪过去,**三条全绿**。钉住的是执行那半,
而坏的是"她压根看不见这个词"那半。所以这条的承重断言是菜单里那一句
`- walk:`,不是世界状态。

### 给了她一个必填参数,却从来没告诉过她这个世界有哪些地方

上一条把 `walk` 交到她手上之后,她第一次试着用它,写的是「回声后面有个小阁楼」——
世界里没有这个地方。不是她编得起劲:**整份提示词里没有一处列过这个世界有哪些地方。**
在场块说的是"你在唱片店,同在这里的还有……",感知块说的是这屋里有什么东西,
动词菜单说的是 `walk` 要一个 `location` —— 必填,而取值范围一个字都没有。
于是整件事退回散文(她"说"她要去哪儿,人没动),连一次被拒绝的记录都不留。

- 在场块末尾加一行「这个世界里你去得了的地方:……」(`chat.presence_block` 的第七个
  占位符 `{places}`)。
- **只有名字,不带 id**:`walk` / `walk_away` 这一轮已经改成收人话(`resolve_location`),
  印 id 就又成了"我用一种写法印给她,却按另一种写法验她的回答"。
- 清单走 `point_names()`,和两个动词认的那份是**同一份** —— 各写一遍就迟早只有一半
  跟着代码走。按世界的规模封顶,不随时间涨(有界性那条)。
- 空世界给的是「就这儿一处」而不是空白:空白会拼成"你去得了的地方:。",她读到那个
  句号只会更糊涂。

⚠️ **这个洞被引擎自带的那份 `world.setting` 盖了很久**,而这正是它活到今天的原因:
橱窗那段世界观手写着"街区只有三个地方——咖啡店(cafe)、建筑工作室(workshop)、以及
一间用来画画的家(home)",于是演示世界和整套测试都读得到清单,谁也没发现清单来自
一段**作者可以随手换掉的散文**。真世界都会换。所以这条的测试**先把 `world.setting`
换成一句没有清单的话**再问她收到的提示词 —— 不换掉它,把修法整个抹掉测试照样绿。
(而那段手写的话本身也是一颗雷:谁给这个世界加第四个地点,它就当场变成一句谎,
且不报错。)

- 顺带:`prompt_store._SAMPLE_VARS` 是"哪个模板认哪些占位符"的权威,作者保存模板时
  按它校验。这一格漏填的话,加了占位符的模板会在**作者改它的那一刻**报"用了样本里
  没有的变量" —— 而作者什么都没做错。

### 又一局:他说「走吧，你跟紧点」,然后他一个人走了

上面那条上线之后,拿真浏览器和真模型,以玩家「阿舟」的身份在晚潮世界从头玩了一局
(搬来第二天的生客,先在咖啡车跟苏念聊,再跟林迟聊、请他补雨棚、请他带我去江堤)。
`places` 那条当场兑现了:问她"这镇上有什么安静点的地方",她报的是**老码头、
潮汐里三号、电台楼顶** —— 三个都真在世界里。这一局撞出六件,前三件都指着同一个洞。

- **一场面对面的对话说到一半,排班可以把她挪走,而这件事对谁都不留一个字。**
  两次,两个人:苏念说「我得先把这车收一收,你等我两分钟」,林迟说「走吧,从这儿
  过去十来分钟。你跟紧点」—— 然后各自被自己的班表带去了别处,玩家还站在原地,
  聊天窗里一个字都没提。她自己那侧更难看:提示词里那段静静地翻成了「因此对话媒介
  是手机文字私聊」,而她照着转录的惯性接着写**在原地做事** —— 苏念人在潮汐里 3 号,
  写的是「转身从架子上抄起一只搪瓷杯」「把磨豆机收进架子」,擦的是两条街外那辆
  咖啡车的台子。世界照跑,日志一行不错。
  - 世界的时钟不等人,所以"挪走"本身是对的;缺的是**说一句**。
    `Scheduler._last_arrival`(内存,和 `_transit` 同一个性质)记下每个人最近一次
    走完的路,身份块在她**刚好是从他站的那个地方**走开时补一句「**是你走开的**:
    你原本和阿舟一起在小念咖啡车,后来你去了晚潮电台,他还在小念咖啡车。你的动作
    只能发生在晚潮电台」。世界的事实进提示词、话由她说 —— 于是玩家从**她嘴里**
    知道这件事,而不是靠界面上一行系统提示。
  - **只在这一种情形下说**。她一早从家里出来的那趟和这场对话无关,说了就是噪音,
    而噪音多了她连真的那句也一起不读。两条测试各钉一半。

- **「带我去江堤走走」—— 世界里没有任何动词兑现得了它。** 分类器判得好好的
  (`{"action":"together","place":"江堤","object":"江堤","verb":"走走"}`),而
  `_together` 只认 `object`:拿「江堤」去**实体**表里查,查出长椅、路灯、斜坡阶、
  老樟树四样,于是他读到的是「江堤 对得上好几样东西……说准一点」—— 他要的是一个
  **地方**,而这个世界里正好有一个叫江堤的地方。`walk` 只挪她一个人,"带上我"
  这一格从来没有过。新增 `_go_together`:她走她那段(`move_agent`,和排班同一条路),
  他走他那段(`player_walk`),**两段都要花时间** —— 瞬移一个、走路一个,才是把
  "一起"两个字写成谎。他自己正在赶别的路就整件事不算数(只挪她一个的话,回执写着
  "带你去"而世界里是他被落下,比不做更坏);已经在那儿了就说"你们已经在江堤了",
  不发一次假的行程。`object` 也指得着一样东西时不抢:「我们去江堤那棵老樟树下
  坐会儿」说的是那棵树,能力那条路上有作者声明的效果、代价与她的同意。

- **引擎的回执被当成她说过的话存进了转录。** 线上原文,林迟那条消息的第一行:

      (咖啡车的雨棚不能被「站」;它能被一起躲会儿雨、端详、补一补)

  它是引擎对我那句「要不要一起到雨棚底下站会儿」的答复,塞在她的回复前面流出去,
  而宿主原样把整段流回传给 `record_chat_turn` —— 于是它作为**她的台词**落库,
  接着进了邀请判定的提示词、会话摘要、她的长期记忆。他还真照着演了一句:
  「不过你别说「站会儿」这词儿,我刚被人纠正过,雨棚不能「站」,只能「躲」」——
  **一个角色在转述引擎的语法纠错**。`record_chat_turn` 现在把这一轮引擎自己刚发出去
  的那句原文摘掉;不猜形状(凭"括号开头"去删会把她自己的动作块也删掉),
  跨进程的宿主 memo 是空的就照旧不摘。**世界的记账不是她的台词。**

- **玩家点一次"走去哈尔滨",收到二十个拉丁字母铺在一个中文世界里。**
  上一版为 `places_menu` 加 `with_ids` 分开两个读者时,理由写着"`walk` 那一处必须
  带 id,它只收 `point_ids()`" —— 而**同一版**把 `walk` 改成了收人话
  (`resolve_location`),那个理由当场就不成立,断言却没跟着改(测试逐字钉着
  `名字(id)`,所以它一直是绿的)。两个读者现在都打得出名字,id 就只是引擎自己的
  记账。重名的那几个照旧带 id。

- **`walk` 给玩家的那份按钮说明写着「途中**她**在路上」。** 一条 `description`
  三个面共用(CHAT / BODY / PLAYER),而文案是照她那侧写的 —— 玩家读自己的按钮,
  说的是别人。

- **`ontology` 的量表印 label,底下的能力行印键名,一张表自己跟自己对不上。**
  晚潮的 `agent` 量表里写着「手上的活儿」,紧跟着的能力写着「她得 me_手艺 >= 1.0」,
  于是读表的人(**包括我**,这一轮当场判断"这五条能力永远做不成、这个世界坏了")
  得出的结论是那个量没声明过。它声明得好好的,只是作者给它起了个别的说法。
  隔壁那几行能力**早就做对了**(`verb(label)` 两个都印,注释里还写着理由:
  "作者调的是 id,她读到的是人话,而排错时要对得上的正是这两者"),量这一行漏了
  同一手 —— 而"这个量到底叫什么"只有这里问得到。

三件**查过、不是 bug** 的,记下来省得下次再查:邀请判定这条链是通的
(`source: "judge"`,拒绝的话是模型写的,而且提示词里 `recent_talk` 那一块也真的
带上了他俩刚说完的四轮);它的模型往返**在** LLM 流水里(先前 tail 的窗口没框到,
差点又得出一个"这条路没被走到"的结论);玩家的 `interact` 全链路对(补一次雨棚
漏雨 1.0→0.85、体力 100→92,回执 `me_changed` / `me_delta` 两栏各说各的)。

## [3.1.0] —— 世界下过的雨有人记得,而她读到的是「瓢泼大雨」 (2026-08-11)

又一轮真人试玩(拿源码装的 venv + 真 LLM,把橱窗世界从创世玩到第 4 个世界日,
再把它声明过的每个动词挨个真调一遍)。四件都是**照跑、日志干净、给错东西**:

### Fixed

- **人上了路,可见性表还把他按在原地**(`_settle_actor_place`)。在途那一支只是
  `return`,不写新的 —— 而上一次写进表里的地点原样留着。于是同一份提示词里两块
  打架:presence 走 `_agent_locations()`(在途的人被排除)说「同在这里的还有:
  没有别人」,perception 走可见性表说「这里的陆知遥」。LLM 挑一边编,而且无声:
  她要么当自己一个人待着,要么对着一个走了半天的人说话。`unplace()` 早就存在,
  它的注释写的就是同一条理由,只是这条路从来没调用过它。哨子记的是 `_NOWHERE`
  而不是 `pop`:进程中途重启时那张缓存是空的,`pop` 会读成"本来就没落过地"。
  **松手有两个落点**:`_start_journey`(上路那一下)和 `_settle_actor_place`
  (兜底)。只钉后者会留下**一个 tick 的窗口** —— 一趟路在这一 tick 的
  `_settle_actor_place` 跑完之后才开始。头一版就是这么写的,拿真世界取样 39 次
  在途,漏的 6 次**全在上路那一 tick 上**;补了起点之后 40/40 干净。
- **`World.act()` 出口上,四类拒绝被合成了一句散文**。引擎里
  `conditions` / `incapable` / `busy` / `absent` / `declined` 分得清清楚楚,而
  `interact_with` 把除两类之外的全抛成 `ToolCallError`,`tools.call` 捕到它时只留
  `error=…`(没有 `detail`)—— 于是 `reason` 那个词消失了。而 `act()` 存在的全部
  理由就是让别的进程里的角色够得着动词,那个宿主只能去正则匹配中文散文:一个累坏了
  的人于是挨棵树轮着试过去,每一棵都回她"再等等"。**她读到的那句话一直是对的,坏的是
  程序读到的那一份。** 讲不通的调用(`unknown_verb` / `unknown_entity` /
  `no_ontology`)照旧是异常,那一半没有放开。
- **导演动作的两套词表挤在同一个键上**(`intent.py`)。`detail` 里
  `"reason": "refused", **outcome` —— 摊在后面的 `outcome` 把导演层自己的词
  (`refused` / `unknown_place` / `not_colocated` / …)盖成了能力层的词。同一个键在
  不同的失败上说两种语言,宿主照着它分支就会掉进 else。细的那个照样交出去,
  改叫 `refusal_kind`。
- **规划器从没被告知过 `start_min` 的上界**。空窗的末端印作 `24:00`(作为**结束**
  时刻那是对的),模型顺手把 `start_min` 写成 1440 / 1500 / 1530,`validate_steps`
  一条条丢掉,只在日志里留一行 `starts outside the day`。世界照跑、计划照落,只是她
  傍晚那几步没了。丢弃那道闸留着,补的是提示词里那句范围。
- **`anima-world chat` 的抬头印地点 id**:「苏晚夏 @ home」,而同一个命令的名册、
  底下的地图、她自己的台词全写「家」。抽出 `_place_names()`,两处共用。
- **一道会假绿的闸**(`test_verb_writes.py`)。`_bring_together` 搬一个人要 tick 最多
  80 次,而这 80 tick 里行为树完全可能让**另一个**起身走掉 —— 于是 `talk_to` /
  `broadcast` 红在"你们不在同一个地方"上,和它要验的东西毫无关系。全量套件里逮到
  一次,隔离重跑 5/5 全绿。换成 `_stand_together`:反复搬,直到两个人**同时**站在
  那儿且都不在路上。

- **`anima-world chat` 把每一轮的观测量丢了**。`chat_reply` 的 `meta` 是出参 ——
  姿态、意图、调过的能力都从那里回来,而 `record_chat_turn(..., meta=meta)` 才把
  它们写到消息行的四列上。这条门从来没建过那个 dict:消息本身好好地落了库,
  `stance` / `intent` / `intent_confidence` / `tool_calls` 永远是 null。运维台上
  气泡照常显示,只是永远没有 tag。真跑一遍逮到的:一轮明明被判成 `style_adjust`
  (回复就是引擎的确认语),消息行上一个字没有。`play` 那条门一直是对的 ——
  **两条门,一条把观测量丢了。**

### Changed

- **`World.autonomy_stats()` 改成跨进程**,并补上 CLI 出口(`anima-world doctor`)。
  四个计数原先只活在**跑世界那个进程**的内存里,而问"她到底主动过没有"的人几乎
  总在另一个进程里(CLI、运维台、宿主的健康检查)—— 他拿到的永远是
  `{asked:0, acted:0, quiet:0, failed:0}`,一个"这条链从没跑过"的答案,
  **而那恰恰是这四个数要用来排除的那种情况**。诊断本身给出假阴性,比没有诊断更坏。
  现在一轮结束(以及崩掉时)发布到 `:meta` 的 `autonomy_stats` 行上,`autonomy_stats()`
  一律读那一份;内存那个 dict 退成写缓冲。`doctor` 把三种坏法各报一句并让退出码
  变 1:一轮都没跑过 / 太久没跑过 / 问了却一轮都走不完 / 没成的比做成的还多。
  已在 `docs/FOR-STUDIO.md` 记了这一笔(加了 CLI 出口就去回执,这条流程纪律)。

  发布的行里另有两格,**都是把这个修复真上线到一个跑了 18 天的世界之后才看出来的**:

  - `last_tick`(上一轮在第几 tick)。四个数只说得清"本次开机以来",而重启之后库里
    躺着的还是上一次开机那一行 —— 光看数的话,"刚重启、新的一轮还没到"和"这条链
    死了"长得一模一样。**同一个错换了个地方又犯了一次。** 判据改成"离上一轮过去
    多久",宽限两个间隔。
  - `last_failure`(最近一次没成是什么)。`last` 每轮都被改写,而"什么都不做"是这一层
    的常态 —— 一次失败后面跟上两轮沉默,理由就没了,只剩 `failed: 1`。上线之后那个
    世界报的正是 `failed: 1`,而库里、日志里都找不到那一次到底是什么。

### 试过一遍、确认没坏的

本体那一整摞真调得动:`look` / `tend` / `harvest` 即时兑现,`嫁接` 起 12 tick 的长
过程并在收尾时把「最大树高」12→14,`育苗` 24 tick 之后真的长出 `sapling:1`(量落地、
位置落地、出生自检过关),中途走开则 `entity_disengage / reason: left` 且代价不退。
autonomy 这条链是通的(asked 20 / acted 1 / quiet 13 / failed 0)。世界规律在动
(橡树 3.2 → 4.69)。导演场景与改对话规则两个 intent 都真的兑现在世界里。

**一起做事**(`participants`)整条通了:0 个人被挡住并说清楚要几个,叫一个不在这儿
的人被在场闸挡住,叫在场的人则真去问他 —— 真模型下他会拒绝(「不熟,没什么可聊的」
「手冲还没喝完,不想动」),也会答应(五次里一次,其余四次是他被排上班了,
「正在工作,不想被打断」)。拒绝时代价一分不扣。

⚠️ 还没动的两处:**她被激怒时不调能力**——把人骂到「门在那儿,慢走不送」,
`walk_away` / `refuse_topic` 一个都没调,只是嘴上赶人。能力的管道是通的
(`test_chat_tools.py` 盯着),这是"提示词是权重,不是限流器"那条老账,属于调模板
不属于修 bug。以及 `events export` 的头一行还写着 `db_meta`,那张表 2.0 就随 SQLite
退役了。

---

### 再一轮:世界里发生的事,终于有人记得它

这五件是对着**同一个线上世界**(「晚潮 · 江渡镇的雨季」,3095 条事件、19 个世界日)
的一次对账翻出来的,每一件的形状都一样:**照跑、日志一行不错、给错东西**。

- 世界的高潮「江水漫堤」真的发生了,事件在日志里躺着 —— 而四个角色关于它的记忆是
  **0 条**。江晚当时就站在堤上。
- 那条水位规律在调试期的滚动重启里每次多烧一整天的雨,洪水提前两天半发生在**没人看
  的第一天**,此后水位顶死 clamp 上限 17 个世界日。
- 二十条关系的 `trust` / `affection` / `respect` **全是 0.0**,于是 2.1.0 的招牌特性
  「她自己想起你」在那个世界上**一次都没发生过**。
- 她读到的是 `雨势 0.8` 和 `季节 2` —— 引擎的账,不是一个住在江边的人眼里的世界。
- 作者写了 `{"key": "size", "label": "树高"}`,而她读到的仍然是 `size 3.2`:
  `label` 这条路整根断着。

五件之间有两组配对,合并读比分开读省力:**`emit` 的 `importance` 是生产端、见证记忆
是消费端**(同一条契约的两半);**水位落库和常数步长 lint 是同一个 bug 的两半** ——
一个堵住最常见的触发源,一个在加载期点出**写法本身**不免疫。

**五件全部遵守"声明本身就是开关"**:不写的世界这一层整个缺席,行为与从前逐位相同。
没有一个新配置键。

#### Added

- **规律的 `emit` 收三个可选字段:`importance` / `text` / `on`**(REFERENCE §2.9.3.1)。
  `importance`(0~1)决定这件事进不进得了在场者的记忆 —— 不写就**只进日志**;`text`
  是她记住的那句话,不写回落成 `type`;`on` 是 `rise`(默认)/ `fall` / `both`,补上
  "汛期年复一年、潮起潮落"这一整类此前写不出来的语义。三条闸:**上下界是闸不是
  clamp**(写了 `8` 的作者想的是"很重要",截断之后他永远不知道自己写错了刻度);
  **只写 `text` 不写 `importance` 当场开不了机**(那句话一个人都读不到,而静默无效是
  这个仓库最怕的坏法);**双边沿仍然无状态**(两个值都在这一轮的双缓冲快照里,重启
  之后既不补发也不永远沉默)。事件 payload 多一个**总是有**的 `edge`,以及声明过时才
  出现的 `importance` / `text` —— 三个契约键合并在作者的 `payload` **之后**。
- **见证记忆(`kind: "witness"`)**:规律 `emit` 出来的事件会变成**在场者的一段记忆**
  (§2.9.3.2)。**谁在场按位置算,不按"谁订阅了"**:`owner == "world"` 是名册里所有人
  (**在路上的人也算**);挂在某样东西身上的事,只有此刻和它同处一地的人看得见,位置
  取 `stock_places`(权威)再回落到本体声明的 `location`。⚠️ **位置查不到 = 没有见证者,
  不是"所有人"** —— 猜成所有人的话,一棵不知道在哪的树倒了全世界都记得它。走
  `memory_seed` 那条现成的路(重放安全是继承来的),`anchor` 是 false 所以照常参与遗忘
  与容量淘汰,**不破坏"进得了提示词的必须有界"**。`witness` 是独立的 kind 而不是复用
  `seed`:一条"她亲眼看见"和一条创世注入的背景设定来路完全不同。**顺序是承重的** ——
  事件先落进历史,记忆再从它长出来。
- **`rand()`:可重放的意外**(§2.9.3.3)。值域 `[0,1)`,由 `(world_id, rule_id, owner,
  tick)` 四个坐标经 blake2b 折出来 —— **不是随机数,是"这个世界这一刻的骰子"**,于是
  "阵雨、意外、运气"表达得出来而 replay 纪律没有松。不许 `random`、不许读时间、
  **不许内置 `hash()`**(它有每进程一份的盐,同一个世界在两个进程里会摇出两副骰子)、
  取 53 位除 2⁵³(64 位除 2⁶⁴ 会舍入出 `rand() < 1` 偶尔为假)。**一刻只投一次**,所以
  `rand()` 不收参数;要两个互不相干的数就拆成两条规律。⚠️ **能力那一层还没有骰子,而且
  写了开不了机**:`when`/`requires`/`costs`/`set` 走不带骰子的编译,闸在**加载期**并指出
  是哪个动词 —— 要给它发骰子得先给它一组自己的坐标(世界 / 种类+动词 / 施动者 / 对象 /
  tick),别把编译时的默认值改成"有骰子"。
- **可见性声明加了可选的 `bands`**(§2.9.4.2):量从"她读得到一个数字"变成"她读得到一句
  人话"(`雨势 0.8` → `雨势 瓢泼大雨`,`季节 2` → `季节 夏天`)。阈值**严格升序**、取
  最后一个 `<=` 当前值的那一档、**两头封口**(第一档向下封口是有意的 —— 回落成数字会让
  同一个量时而是词时而是数,而那种不一致只在某个低值时刻出现,没人会发现)。两处都能写
  (`stock_visibility` 那一行 / `kinds` 里量声明自己,**可见性是量声明的一部分,分档
  同理**),两处都写时前者赢。**渲染是赠品,键与数字是契约**:`World.perception()` /
  `ontology --json` 里的数原样不动,只多一个 `words` 段;她**做决定**读的也仍然是真值。
  坏声明加载时**一次列全并拒绝开机**(跳过一条的下场是她一直在报数字,作者三个月后才
  发现自己写错了一个方括号)。

#### Fixed

- **量的 `label` 从来没进过她的提示词。** 此前它只是**存着**:`stock_visibility` 的行
  有它、`Quantity.label` 有它、`ontology --json` 报得出它 —— 而 `perceive()` 渲染用的是
  **原始量名**。作者认认真真写了 `{"key": "size", "label": "树高"}`,世界照跑、日志干净,
  她读到的是 `size 3.2`。这不是小事:引擎的定位里写着"**英文世界靠 `label` 机制与英文
  种子,不靠引擎换语言**",而那整条路挂在这一个字段上。现在 **`label` 换名字、`bands`
  换值,两样正交,各占一个位置**(定成"档词顶掉名字"的话 `- 这里的老橡树:齐人高` 读起来
  像那棵树叫齐人高;反过来 `bands` 白写)。⚠️ **量的名字和东西的名字是两张表** —— 合成
  一张就会互相盖(要么每棵树都叫"树高",要么每个量都叫"老橡树"),而两种都只是读着别扭、
  不报错。反查**按种类做**:树的 `size` 是树高,矿的 `size` 是储量,全局一张表只留得下
  一个。键仍是契约(`perception()` 另加一段 `labels`),**排序按量名不按 label**。
- **定时轮次的决定上下文会绕开档词。** 那条路原先自己拼 `{value:g}`,于是她说"外面瓢泼
  大雨",转头按 0.8 做决定 —— 模拟层和角色层是两套跑在一个进程里的系统。改成和聊天那条
  路共用 `Perception.describe_own()` / `describe_public()`:**观察窗不许撒谎,这一条同样
  适用于她自己的决定上下文。**
- **规律的节流水位从内存态改为落库**(`:meta` 的 `rule_marks` 行,§2.9.3.4)。它曾经是
  内存态,理由是"只决定要不要现在算,不影响结果 —— 重启清空最多多算一次,值不会错"。
  **那句话只对按 `dt` 折算的规律成立**:一条常数步长的规律(`雨天数 + 1`)每多算一次就
  真的多走一整步,而"多算一次"的次数由**运维重启了几次**决定 —— 世界的物理法则从此挂在
  部署节奏上。线上实测雨天数 56 而按时钟应为 ~50,洪水本该第 3.2 天、实际发生在第 1 天,
  **那个世界唯一的叙事高潮被烧在了没人看的第一天,而日志一条错都没有**。三条节制:只落
  `every > 1` 的规律、只在水位真变了时写(写回在 `finally` 里)、冷启动**不补烧停机期间
  的账**(补烧是把这个 bug 反着犯一遍),水位比时钟还新时当作没有水位**并点名**。
  ⚠️ **没有新增 Redis 键**(`:meta` 里多一行),`contract --json` 的 `storage` 段一个字
  没改 —— **镜像端不欠这一笔同步**。
- **加载期多一道 lint:常数步长的自增规律**(警告,不拒绝)。水位落库堵住的只是最常见的
  触发源,**写法本身仍然不免疫**(升级、跨前缀导入、`every` 被改小的世界第一次开机照样
  会算一轮)—— 缺了这一半的话,下一个写 `雨天数 + 1` 的作者仍然会踩,只是换一种触发
  方式。判据收得很紧,**三条都占才点名**:读了自己写的那个量、没有用 `dt`、`every` 大于
  1 tick(`{"季节": "floor(day/90) % 4"}` 不读自己,算几遍都是同一个答案 —— 误报够多次的
  警告等于没有警告)。两个出口读**同一个函数**:开机日志(只在**装载**那一处打,一次开机
  恰好一遍)与 `anima-world validate world`(**作者真正会看的是这里**)。
  ⚠️ **"恰好一遍"是拿真世界重启验出来的,不是写出来的。** 头一版无条件在
  `_load_world_rules` 里打,而 `_precheck_ontology` 也走这个函数 —— 把 3.1.0 装上线上
  那个世界、重启,日志里同一条说了两遍(两条规律共四行)。而单测是绿的:它直接调那个
  函数、只调一次,**测的是函数不是那条真路**。现在开口是显式的(`warn=True`,全仓库
  只有装载那一处写),新加的调用点默认闭嘴。回归测试跟着改成**开两次机**——预检读的是
  库里那份规律,首启时那张表还空着,只验首启的话 bug 还在测试也是绿的。
- **三轴关系从来没落地过。** `JudgeResult.axes_*` 是**可选**的,`_clamp_axes` 只誊抄模型
  吐出来的那一份,注释里还写着"LLM 不给就留空,单轴判定照样成立" —— 而真模型
  (LongCat-2.0)就是不吐。后果不是"少了三个数字":`closeness` 是
  `0.6·sentiment + 0.25·affection + 0.15·trust`,三轴不动它最高只有 0.6 倍 sentiment。
  ⚠️ **而单测当年全绿,因为它们跑在 `DeterministicRelationshipJudge` 上,那个替身一直在
  派生三轴** —— 两条路上的世界因此长成两个形状,差别只在配没配 key,**替身掩护了真判定器
  的缺席**。现在:模型一个轴都没落地时判定器**自己派生一份**,规则由真判定器和替身
  **共用**(`derive_axes`)。三个轴三条**不同形状**的规则,不是三个不同的系数
  ——`affection` 线性 `0.80·d`(聊天的主路)、`trust` **不对称**(涨 `0.35·d` 跌 `0.90·d`:
  一次失信收回去的比十次守诺攒下的还多)、`respect` 是**一道门槛**(`|d|` 过
  `0.4·MAX_DELTA` 才动:一句"今天雨真大"里没有本事、担当或见识,做成线性系数的话一百次
  寒暄堆出来的敬重会和一次救命之恩一样高)。**每一份额都严格小于 1**(轴是骑在 headline
  上的加细)。八卦那一路用另一组份额(`affection 0.20` / `trust 0.70`↑`0.95`↓ /
  `respect 0.60`):别人嘴里的他改变的是"我以为他是个什么人",不是"我跟他有多亲近"。
  **派生是回落不是覆盖** —— 模型开了口就整份听它的,而且**回落的粒度是"这一路"不是
  "这一个轴"**(补出来的那两个和它给的那一个不是同一次判断,拼在一起的那份东西没有任何人
  做出过)。**降级不许无声**:计数 + `subsystem_health` 的 `relationship_axes`(边沿触发,
  和 planner / narrative 同一条)。顺带修了提示词里那个**字面省略号**
  (`"axes_b_to_a": {"trust": …}` —— 照抄它就是一份语法不合法的 JSON)。
  ⚠️ **要说实话的一句**:线上那条只有"久别"一条由头的关系(score 0.15 / 门槛 0.25)修完
  是 0.213,**仍然不过闸** —— 剩下那 1.17× 是 `contact` 那一层的调参问题,不是判定器的。
  判定器这一侧能给的全部就是 1.421×,而它把"两条由头也不触发"变成了"两条由头就触发"。
  **`CLOSENESS_WEIGHTS` 这一轮有意没动。** 那组权重当初正是照着"三轴全是 0"的真实数据
  定的,前提没了确实值得重估 —— 但手里只有一次真人对局的一个数据点,照着它调等于把噪声
  当信号钉进常数。先让三轴在真世界里跑一段,攒够分布再谈。

#### Changed

- `JudgeResult.axes_*` 的契约从"可选,模型不给就空"变成"**判定器不吐空的**"。
  `tests/test_social.py::test_judge_parses_and_clamps_axes` 原本钉的是"垃圾轴降级为无",
  而**降级为无就是这条 bug 的机制本身** —— 改成了"读不懂模型给的轴时回落派生"。解析层
  (`_clamp_axes`)照旧拒收坏值、照旧不炸。
- `DeterministicRelationshipJudge` 的三轴从 `{trust: d/2, affection: d, respect: 0}` 改为
  共用 `derive_axes`。替身的量级下(`STEP=0.04`)`respect` 仍恒为 0(门槛 0.4 过不去),
  所以 `test_together.py` 那条断言不变。
- `RelationshipJudge.__init__` 多一个**可选**的第三位参数 `health`(形状就是
  `Scheduler.note_subsystem`),`build_serve_scheduler` 接上它。新增模块级公开名字
  `derive_axes` / `CHAT_SHARES` / `HEARSAY_SHARES` / `AXIS_NAMES`(不是 `World` 门面的
  方法,API 面不受影响)。
- `stocks.evaluate_due` 多一个 `world_id=` 形参(缺省空串,老调用点行为不变)——
  它是 `rand()` 的第一个坐标。⚠️ **调度器那一行必须传真实的世界名**:不传的话两个世界
  摇**同一副骰子**,世界照跑、日志干净,而"每个世界有自己的运气"这句话是假的。
- **规律 `emit` 出来的事件 payload 多一个 `edge` 键**,无条件出现 —— 严格说这不是"逐位
  不变"。影响面只在"谁在读规律事件的 payload";让消费端靠"有没有这个键"反推方向更差。

#### 文档

REFERENCE 新增 §2.9.3.1–4(`emit` 三字段 / 见证记忆 / `rand()` / 水位与 lint)与
§2.9.4.1–2(`label` / `bands`);§2.8 补了三轴派生那一整节;§2.9.6.6 与 §2.9.8 里
"三轴全是 0"那两处描述跟着改;§8 键清单补了一张 **`:meta` 行表**
(`world_seed` / `owner_pid` / `owner_host` / `autonomy_stats` / `rule_marks` ——
`autonomy_stats` 此前也一直没写进去);§9 末尾新增一张
**`validate world` vs `simulate --ticks 0` 的覆盖表**。
⚠️ 那张覆盖表记的是一个真实的缺口:`validate world` 只报得出 `stock_visibility` 那侧的
`bands`,**`kinds` 里量声明的 `bands`、`emit` 的三字段、能力里的 `rand()` 一个都报不出来**
(它们的闸住在规律/本体的编译器里,`--ticks 0` 才是它们的校验器)。

## [3.0.0] —— 她终于认得坐在对面的这个人 (2026-08-11)

一轮**真人对局**的产出:先写用户画像,再照着那个人的操作轨迹和心态从头玩一遍这个
下雨的世界,把撞到的每一件事修掉。所以这一节里没有一条是从代码里读出来的 —— 它们
全都是在屏幕前发生过的。

共同的形状是这个仓库最怕的那一种:**照跑、日志一行不错、给错东西**。玩家报了名字
而世界一个字没存;宿主传过一次名字第二轮不传就被抹掉;在咖啡店聊完的摘要里长出一间
酒吧;地图上的地名把区域边框写掉;开箱第一屏是三个人在睡觉。没有一件会让程序崩,
所以没有一件会有人来报。

**为什么是 3.0.0**:2.x 从未发到 PyPI(那上面停在 1.3.0),而 2.0 起
`World.open` 签名、包格式、CLI 参数全破 —— 按硬钉版纪律,对包索引上的使用者而言
这是一次主版本级变更。调试模式期间(2026-08-10 起)`__version__` 冻在 2.3.1、
改动全部堆在 `[Unreleased]`,就是为了在这里一次性落版号。
`2.3.0` / `2.3.1` 两个 tag 照旧一次都不复用(理由见 2.3.1 那节)。

### Fixed —— 玩家的名字从来没进过世界,而她只好自己编一个(跨仓库)

线上世界 `night-tide` 里,四条 0.8 重要度的记忆写着玩家叫「旅人」,而玩家从没这么
说过,平台数据库里他叫**刘俊康**。

链路是这样断的:签名的 membership claim 里刻意只有 `membership_id` 和 `role`,名字
唯一的合法通道是 `/internal/v1/chat` body 里的 `player_display_name`(世界那侧还把
`player_name` / `display_name` 等自由字段一律 400 拒收)。而网站后端的
`WorldGateway.chat()` **根本没填这个字段**。于是引擎兜底成 `player-5688afd1`,
身份块紧接着命令她「始终用这个名字认识和称呼对方」—— 一条必然执行不了的命令。
她照着 `role` 那一格(网站那边填的是中文的「旅人」,那是产品有意的设定:
「旅人 / 记者 / 售货员…」)编了一个称呼,然后它进了转录 → 进了会话摘要 →
进了她的长期记忆。**没有任何一处报错,日志一行不错。**

改在两个仓库,core 一个字没动:

- `player`:`WorldGateway.chat()` / `evolve_chat()` 多一个 `display_name`,写进
  `player_display_name`;`platform.py` 两个调用点传 `m.display_name`(在
  `db.commit()` **之前**取出来 —— 那两个闭包跑在响应关闭之后)。
  `tests/test_platform_chat.py::test_玩家的名字跟着每一轮走进世界` 钉这一条,
  已验证抽掉修复会红。
- `platform`(世界服务壳):`/internal/v1/chat-evolution` 也收 `player_display_name`,
  并把它带进 `active_or_start(player_name=…)` —— 会话行的 `participants` 是**开会话
  那一下**写死的,后面每轮 `add_message` 都不碰它,漏了名字就永远只剩 uuid。
  名字**不进幂等键**:那个键认的是"这一轮说了什么",掺进名字会让玩家改一次昵称
  就把同一条投递的重试撞成 409。
  新增 `_clean_display_name()`:名字要渲染进一个自称「最高优先级事实」的块,
  带换行的名字能把自己写成新的一段 —— 信任边界是进程边界,壳这一侧收干净
  (控制字符去掉、换行折成空格、40 字截断)。
  `player_name=` 走 `_accepts_player_name()` 探签名,和 `checkpoint` 同一条纪律:
  这个壳要装进多个引擎版本的镜像,老引擎收不下就照旧不带名字开会话。

⚠️ **已经写下的改不掉**:那四条记忆和三份会话摘要里的「旅人」是历史,重放折出来
还是它。改完之后她会同时"记得旅人这个称呼"和"知道你叫刘俊康"。

### Fixed —— 名字和称呼是两件事(身份块)

上一条的另一半,这次在引擎里。病根不是"兜底名字不好看",是**引擎逼她说一句它自己
知道是假的话**:没名字时兜底成 `player-3f9a2c`,紧接着命令她「始终用这个名字认识和
称呼对方」。这条命令执行不了 —— 于是她自己编,而编出来的那个进了转录、摘要和记忆。

分成两格:

- **称呼**(`address`):她总得叫他点什么。有人话身份就用身份(「旅人」),否则「访客」。
  `role` 是**宿主传什么算什么**,但 `player`/`user`/`guest`/`visitor`/`member` 这几个
  是各处 `role=` 参数的默认值 —— 它们的意思是"宿主没说身份",不是一句人话,
  既不印成身份也不拿来当称呼(`chat_service.address_for`)。
- **名字**(`display_name`):没有就是没有。身份块明说「那是称呼,不是名字」,
  他问起自己叫什么就照实说不知道,**不许自己给他起一个**;他一说,下一轮照名字认人。

顺带两处:

- 有名字那一支的措辞从「始终用这个名字**认识和称呼**对方」改成「认人不许变;
  至于当面怎么称呼他,若这位玩家教过你别的叫法(见上面那一块),按他教的来」——
  否则 `nickname_for_player` 这类 override 和身份块**每轮顶牛**,而两条指令顶着的
  下场实测是模型摇摆(见 2.1.0 末尾「人设优先于通则」那条)。
- **`debug_prompt(role=…)` 的默认值从 `"player"` 改成 `""`**:它盖过了世界里真正的
  身份,于是同一个世界,真聊天里她读到「身份是旅人」,调试视图里是「身份是player」。
  调试视图撒谎比没有调试视图更坏。真聊天与调试视图现在共用
  `World._interlocutor_for()` —— 那份 dict 是身份块的全部输入,两条路各拼一遍就会分叉。

CLI 的 `--name` 不给时不再假装你叫「访客」,而是"他还没说过名字"。
`tests/test_chat_identity.py` 盯这一层。

### Fixed —— 玩家刚教过的叫法,被同一份提示词里的身份块当场否掉

上一条的续,而这一次顶牛的两条**都是引擎自己写的**。真的坐下来玩了一场才看见:
玩家说「以后叫我小林」→ 意图分类判成 `style_adjust` → 规则落库 → `overrides` 块
照着说「怎么称呼玩家:小林」。而紧接着自称"最高优先级事实"的身份块还在说
「他**没有告诉过你他叫什么名字**……不许自己给他起一个」,称呼那一格填的是兜底的
「访客」。两条指令顶着的下场实测是模型摇摆(2.1.0 末尾那条)。

病根是**两块各读各的**:`_interlocutor_for` 认不得那些规则(它不知道是跟哪个角色
说话,而规则按 (角色, 玩家) 存),于是它那一边永远只给得出「访客」。新增
`ChatService.taught_address()`,身份块与 `overrides` 块从此读同一个答案;
`nickname_for_player` 优先于 `address_form`(昵称是他点名要的那一个,称呼形式更像
口气偏好)。

**名字和称呼这道分界没有跟着丢掉**:教过的那一支改说「那是他教你的叫法,
**不一定是他的本名**;他要是问起你知不知道他的真名,照实说他只教过你这样叫」。

### Fixed —— 她在你们聊到一半的时候过来把你当生客搭讪

一场真的对局里,`autonomy` 让她 `reach_out` 插了两次话:「嘿,你是第一次来我们店
吧?」「嘿,欢迎光临!今天想喝点什么?」—— 而她刚认出我、给我做了咖啡、跟我聊完
她的生意。

两条路各管一半的老问题:`_maybe_hail_player`(闲着时的搭话)有一道"一天一次"的
水位 `_hailed`,而 `reach_out` 这条**整个绕过去**。合成一个
`Scheduler.claim_hail(agent_id, player_id)` —— **查与记是同一个调用**,分成两步的话
两条路迟早各记各的。它同时补上第二条判据:**今天已经跟他说过话也算开过口**
(取 `contact_store.last_contact_tick`,所以 `World.chat` 和 `record_chat_turn` 两扇
门进来的对话都算数)。搭话是开场白,而开场白一天只有一次;接着说的那句叫接话。

`ToolRuntime` 多一格 `claim_hail`;`tests/test_autonomy.py` 两条盯这一层,都验过
抽掉修复会红。

### Fixed —— 她读到的地名一半是人话、一半是 id

`presence` 块里在途那一句从来没翻译过目的地,于是她收到的是
「你在建筑工作室，正在去cafe的路上」—— 同一句话里一个地方用人话、另一个用 id,
而她会照着把 `cafe` 念出口。不报错、测试不红,只是出戏。
两处地名现在都过同一个 `_place_name()`。

`play` / `chat` 的名册(`/who`)同病:地点印 id、在忙什么印英文动词名
(`work` / `sleep`),现在都说人话。

### Fixed —— 命令行上你有两个身份

`play` 的 `--player-id` 默认 `p1`,而 `chat` / `prompt` 默认 `cli`。于是玩完 `play`
再照 `--help` 去 `prompt` 看她收到什么,看到的是**另一个人**:名字、关系、教过的
规则全记在 `p1` 头上,调试视图问的是一个从没说过话的 `cli`。它不报错,只是空着 ——
调试视图撒谎的一种,而且这一次撒的谎是"她根本不认识你"。三个命令统一到
`DEFAULT_PLAYER_ID = "cli"`,help 里也写明它们共用一个。

`play` 里的 `/who` 从前教你去开一个新进程跑 `anima-world chat --agent 夏` ——
照做等于两个进程操作同一个世界,而时钟那边还在走。现在它说 `/at 夏`。

### Changed —— 人在等一句台词的时候,引擎的日志不许插在中间

`start` / `chat` / `play` 三个命令里,引擎的 WARNING 从前直接落在 stderr 上,横插进
对话:玩家问了一句,屏幕上先冒出来 `plan step starts outside the day (1440),
dropping`,再才是她的回答 —— 一句英文的、跟他无关的、他也无从处理的话。

**不是丢掉**:收着,散场时报一行「这一场里引擎记了 N 条警告」,`--verbose` 就照原样
打。此前只有关系判定那一个 logger 被单独按下去(而且只在已降级时)—— 一个一个按
下去,就是给下一个模块留一个洞。

同一轮把 `_warn_if_llm_degraded` 从 `build_serve_scheduler` 挪进 `run` ——
**只有它需要**(`simulate` 有 `probe_llm` 那道闸,`start` / `chat` / `play` 自己已经
用中文把同一件事说得更清楚)。挂在共用构造器上的结果是每一条开世界的路都要听一遍
同一句英文,而它下面两行就是它的中文版。`runtime.llm.degraded_reason` 也改说中文
(键名 `llm.api_key` 照旧原样 —— 那正是他要去敲的那个东西)。

### Fixed —— 他打字告诉了她自己叫什么,而世界一个字都没存

身份块两支结尾都许过一句诺:「他要是告诉了你名字,这一轮之后就照那个名字认他」。
**在这之前没有一行代码兑现它。** 玩家打「我叫林越,你叫我小林就行」,她当场就叫他
小林 —— 因为那一轮的原文还在上下文里;下一场开局,同一份自称"最高优先级事实"的
身份块又说「他**没有告诉过你**他叫什么名字」,而她的长期记忆里明明写着林越。
一份提示词许一个引擎不做的诺,然后在下一场里自己打自己的脸。

修法**不是**把它交给意图分类器:「我叫林越」判成 `style_adjust` 的下场是玩家自报
家门却收到一句「(记下了:…)」的系统回执,他刚说的话被整个吞掉 —— 比不记还伤。
而且分类器默认不跑(`chat.intent.enabled` 默认关),身份块每个世界都在。

于是引擎自己认这几句(`intent.read_self_introduction`,和 `SECOND_PERSON` 同一条
纪律):**记下来,然后放行**,这一轮照旧是对话。名字和称呼照旧分成两格 ——
「我叫林越」进新的 `player_name`,「你叫我小林」进 `address_form`。

三条守着它的边:

- **不升格成 `display_name`。** 那一个是宿主认证过的身份(纪律 3),而这里是一次
  正则猜测;混成一格的话,一次误判就变成她口中"最高优先级"的事实。身份块因此说
  「他**告诉过你**他叫「林越」」,不说「他是林越」—— 出处照实写。
- **只填空,不覆盖。** 他改口("以后叫我老林")走 `style_adjust` 那条明路,不被
  一句偶然命中的正则悄悄改掉。
- **空手比记错强。** 「我叫他小林」「我叫了一杯咖啡」「别叫我先生」「你叫我什么?」
  一律不认;汉字名字卡在 6 个字以内,因为「我叫苏晚夏做一杯咖啡」和「我叫林越」
  结构上分不开,而没有人叫「苏晚夏做一杯咖啡」。记错的那个会进身份块、进转录、进她
  0.8 重要度的长期记忆,而玩家永远不会知道是哪一句让她这么叫他的。

### Fixed —— 自我介绍还是换来了一张收条:上一条的漏网

上面那条修完,拿真模型把内置的那个下雨的世界从头玩一遍,「我叫林越，你叫我小林就行」
她回的是**「（记下了:玩家的昵称 —— 小林。）」** —— 正是上一节亲口写着"绝不能这么干"
的下场。名字确实记下了(`_note_self_introduction` 早一步就落了库),可意图分类器**照样
在跑**,照样判成 `style_adjust(0.95)`,照样以一句系统回执收尾:玩家开口说的第一句话
一个字都没得到回应。

漏在测试的取材上:钉那条修复的用例跑在一个**把 `chat.intent.enabled` 关掉**的世界里,
于是它验的只有正则那一层 —— 而内置的橱窗世界这个开关是**开着的**(1.3.0 起,做了却
开箱看不见等于没做)。默认关的开关拿默认关的世界去验,验不到真正会发生的那条路。

`_dispatch_intent` 现在在 `style_adjust` 那一支前面加一道闸:这一轮要是
`read_self_introduction` 认得的自报家门,直接退回 `dialogue`。分类器给的是同一件事的
第二个答案,而第二个答案在这里不是重复,是吞掉。

闸**只挡 `style_adjust`**,不是"这句话里有名字就绕开分类器":「我叫林越，让遥也过来」
两件事都得发生 —— 名字记下、人真的挪过来。整句跳过分类的写法会让后半句安静地不生效,
世界照跑、日志干净,而玩家等的人永远不来。两条测试各钉一半,而**钉真世界那一条跑在
开关打开的世界里**。

### Fixed —— 宿主报过一次的名字,第二轮不传就丢了

网站第一轮传了 `display_name="林越"`,第二轮没传(CLI 不给 `--name` 时传的就是空串),
她当场又不认识他了,身份块以"最高优先级事实"的口气说「他没有告诉过你他叫什么名字」。
更坏的是那个空串会顺着 `_touch_player` **写回去**,把世界记着的名字抹成空 ——
第一轮认得他,从第二轮起**永远**不认识,而日志一行不错。

`_interlocutor_for` 现在把「宿主这一轮没说」(`None` 与 `""` 同一支)回落到世界自己
记着的那一格。这不是兜底出一个假名字:那一格只有一个写点,写进去的只有宿主亲口传过的
`display_name`,出处仍是宿主(纪律 3 没有松);松掉的只是"世界明明记得却装作不知道"。
宿主从没说过名字时照旧一个字都不编 —— 连玩家 id 都不许漏进提示词,那条血泪纪律
(线上 `night-tide` 把玩家改名叫「旅人」)有一条反向测试钉着。

### Fixed —— 在咖啡店聊完,摘要里冒出了「酒吧」和「酒保」

世界里两样都没有。而这条摘要会进她的长期记忆、再进下一场对话的记忆块,于是一次幻觉
被她当成既成事实反复复述,玩家永远查不出是哪一句造成的。成因是喂给模型的转录只有
`user:` / `assistant:` 两个标签:**谁是谁、在哪、什么身份,模型只能猜**,猜出来的
就是酒保。

现在关会话时由**引擎**把世界已知的事实(角色、对方怎么称呼、地点;没记地点就明说
没有)与「不许写出转录里没有的人物、地点、身份」的禁令拼进提示词,转录标签换成真
名字。作者的 `chat.session_summary` 模板只管语气与详略 —— **把禁令交给一个可被覆盖
的模板,等于让它某天悄悄消失**,而消失了不报错,只是从那天起摘要又开始长出酒吧。

同一条链上顺手堵掉两处 id 泄漏,都是"进了她的长期记忆就再也拿不出来"的那种:
地点此前是转录行上的**英文 id**(`cafe`),现在由宿主注入的地名表翻成人话;取不到
玩家显示名时此前退回**玩家 id**(`p1`),现在退「访客」——**编一个泛称比漏一个 id
强**,因为「访客」一望而知是泛称,而 `p1` 看上去像个名字。

顺带:摘要生成失败或返回空不再静默吞掉(降级不许无声),退回模板摘要时当场记一条警告。

### Fixed —— 地图上的地点名把区域边框写掉了

「建筑工作室」贴着港街的右边框,名字往右一写就把那一行的 `│` 盖了 —— 图上看它像在
港街**外面**,而它明明在里面。框线是这张图上唯一说"谁在谁里面"的东西,名字只是名字:
**名字少几个字读的人看得出来是少了,边框断一格读的人只会安静地读到一句假话。**

所以标签给边框让路:右边是墙就整块挪到左边,两边都摆不下才截,且截了带省略号
(`建筑工…`),按显示宽度截、绝不劈开一个双宽字。站着的人那一格 `[n]` 同一条规矩。
轨迹线**有意不让** —— 一条从框内走到框外的路本来就得穿墙,而 `#` 和字母一眼看得出
是路不是框,不会像被覆盖的边框那样安静地骗人。

### Changed —— 开箱第一屏不再是三个人在睡觉

新世界的时钟从 tick 0 起步 = 第 0 天 00:00,而新世界跑演示速度(1 tick/秒),于是
`anima-world start` 之后的头一分半钟,是三个角色躺在各自家里 —— 而 README 承诺的是
「30 秒看它跑起来」。

修法**不是**把引擎默认值从午夜改成九点:**几点开门是这个世界作者的意见**,它和作息表
是同一件事。新增 `world.start_time`(HH:MM,引擎默认 `00:00`,**不声明它的世界逐位
不变**),内置的旧港声明 `14:30` —— 三人作息里唯一"都醒着且各在各的地方做各的事"的
窗口,而半分钟后柔就动身去咖啡店找夏。只在这个前缀**第一次有钟**时生效(`setnx`),
跑了三天的世界重开钟一格都不动。写错的时刻当场抛:这里的降级只有一种样子(悄悄退回
午夜),而那正是作者永远不会发现的那种坏 —— 他只会觉得"这个世界怎么老是从半夜开始"。

**同时修掉它照出来的一个真 bug**:创世播量时 `updated_tick` 写死成 0,而 `dt` 是从
这个字段算的 —— 世界从 14:30 开门,橡树第一帧就补上一整个上午的生长(3.2 米 → 3.9 米),
世界照跑、日志干净。现在盖的是创世那一刻的钟。这条在"给一个跑着的世界补一层声明"
那条路上一直就是错的,只是从前所有世界都从午夜开始,恒等于对。

⚠️ 连带 23 条测试要改 —— 它们断的是绝对钟值、或者假定"创世那会儿没人在赶路"。
**一条都不是真退化**:守的东西全部仍然成立,只是表达耦合了午夜这个巧合。改法三种:
断推进量而不是绝对值、先等她不在途再起程、场景本身需要夜里的就显式声明
`world.start_time: "00:00"`(引擎默认值就是午夜,这形状仍是真世界长得出来的)。

### Changed —— `--help` 打出来是中英混排,而 README 的命令表漏了 `play`

`run` / `simulate` / `world` 三条(以及它们的 `--beats` / `--agents` / `--llm` /
`--output` 等参数)还是英文,旁边的 `start` / `doctor` / `map` 已经是中文。现已全部
译成中文,另给 `world export` 的 `--summary` / `--genre` / `--setting` / `--theme`
补上原本空着的说明。`start` 与 `run` 的 help 现在各自挑明「**人的门**」与
「**程序的门**」—— 这条分界是承重的,不该靠读文档才看得出来。

README 的命令表漏的不止 `play`(而 `play` 正是「一个人坐下来跟她说话」那条路)。
逐条对过 CLI 之后补齐 9 条(`play` / `validate` / `contract` / `report` / `events` /
`contact` / `presence` / `memory` / `agent`),并按「人打的 / 部署脚本打的」分拨。

## [2.3.1] —— 封皮上的店面栏没跟着包走,而两边的日志都是干净的

### Fixed

**`world inspect --json` 少报三栏:`genre` / `setting` / `theme`。**

这三栏是**作者在创作台填的店面字段**,经运维台的注册表直通玩家看到的世界卡片
(platform `docs/工作台-运维台契约.md` §4)。v2 时代运维台是从解包出来的
`manifest.json` 里读它们的(`lib/enginePackage.js` 的 `readManifest`);**v3 不再
解包成目录**,于是它改从 `world inspect --json` 读同一批字段 —— 而这个命令从来
只报 `name` 和 `summary`。

于是 v3 那条导入路径上,这三栏一律落成空串。**没有任何一处会报错**:包是好的
(manifest 里三栏都在)、导入返回 201、世界跑得好好的,只有玩家看到的卡片上那
几栏空着。实测:一个 `genre` 填了「生活/语言学习」的包导进运维台,
`/directory/v1/worlds` 里回来的是 `"genre": ""`。

修法是**把 manifest 上的封皮字段一个不漏地报出来** —— 而不是让运维台自己去扒包。
后者会在系统里养出第二份对包格式的理解,那正是"全 import"这条基调要防的事。
`world inspect` 的人类渲染跟着多印三行:一栏有没有跟着包走过来,看一眼就知道。

`tests/test_packaging.py::test_封皮把店面栏报全` 盯着这一条。

**为什么是 2.3.1 而不是并进 2.3.0**:2.3.0 已经作为镜像 `anima-world:2.3.0` 在跑
世界了,而镜像 tag 就是引擎版本。同一个版本号对应两种 `inspect` 输出,正是 2.1.0
那份发布说明点名要防的"第二个同名不同行为的镜像"。

## [2.3.0] —— 位置终于算数了,而且两个人能一起做点什么

**为什么是 2.3.0 而不是并进 2.2.0。** 三条,每条单独成立:

1. **`kind` 的 schema 变了**(`affordances.*.participants`)—— 那是**创作契约**,
   而创作台是靠版本号分方言的。并进 2.2.0 等于让"2.2.0"这个方言号对应两套 schema。
2. **`demo.cyberworld` 的 `engine_min` 跟着升到 2.3.0**:橱窗里那条
   「树下小坐」用了 2.3.0 才认识的字段,拿 2.2.0 的引擎装它会当场开不了机 ——
   这本来就该由版本号说出来。
3. 2.2.0 已经写好了自己的 CHANGELOG 段落和 FOR-STUDIO §3.13。合进去等于改写一份
   已经定稿的版本说明,而运维台/创作台读的正是那份。

仍然是**加法**:`ToolSpec` / `Affordance` 的新字段都有默认值、新开关默认关、
`World.open` 的签名与 `.cyberworld` 线格式一个字没动。**关着开关时行为与 2.2.0
逐位相同**(`tests/test_colocation.py` 有四条专门钉这一句)。

### Added —— 一起做事(`participants`,§2.9.6.6)

`interact` 一直是**单人**的(它的定义原话就是"一个人、一个东西、一个瞬间")。于是
两个站在同一个地方的人只能各干各的,而世界记不下"他们一起吃了顿饭"。

**而共同经历正是关系的主要来源。** 在这之前关系只有两条来路:说了多少句话、听说了
什么 —— 两条都是**语言**。一个只靠说话改变关系的世界,和一个聊天机器人的差别只剩下
背景板。一起熬过的一夜该比一百句寒暄更算数,而引擎连"一起熬过一夜"这件事都记不下来。

```json
"树下小坐": {"label": "在树下坐会儿", "participants": {"min": 1, "max": 2},
             "duration": 6, "requires": ["me_体力 >= 5"], "costs": {"体力": "me_体力 - 5"}}
```

```python
world.act("夏", "interact",
          {"target": "tree:harbor_oak", "verb": "树下小坐", "with": ["柔"]},
          surface="body")
```

三条硬纪律,每条都决定了这一层长什么样:

**① 别人凭什么答应。** 一个"拉着谁就一起吃饭"的能力**等于取消对方的意志**。所以
邀请必须拒得掉,而且**拒绝的理由要分得开**:世界那一段(不在这儿 / 睡着了 / 手上有
事 / 把你静音了 / 这件事他做不了)判在性格之前,因为**那几条不是他的意思**。过了这
一段之后才问他肯不肯,而这一问**有判定器就读人设**(`judge.invite`,和吃醋逐字同源),
没有就退回"关系有多近 × 作者声明的「随和」× 上一轮的姿态" —— 后者不是引擎做主:
关系是世界长出来的,「随和」是作者写在 `kinds.agent.quantities` 里的。
**没有 `consent` 这个开关**,写了会开不了机并点名说为什么。

**② 代价对每个人各扣一次,而顺序不许有意义。** `["柔","白霜"]` 和 `["白霜","柔"]`
必须给出逐位相同的世界 —— 做法和规律那层的双缓冲同源:**先把所有人算完,一个字都
不写**。边算边写的话,第一个人扣掉的体力会成为第二个人 `requires` 的输入,于是名单
顺序决定了谁做得成,而那是一条没有任何人写下过的规则。**一个人过不了闸,整件事就不
发生** —— 三个人吃饭,一个人没钱,不该变成两个人吃饭。

**③ 关系变化是这段经历的效果,不是再调一次判定。** 这一条是这一批需求要治的病本身:
再调一次的话,"一起过了一夜"和"多聊了两句"又回到同一个入口上。所以它是**算出来**
的(`social.joint.relation_step` × 人数因子 × 时长因子 × 剩余空间),一次模型都不调,
同样的经历永远给出同样的账。默认步长 **0.06 > `DeterministicRelationshipJudge.STEP`
的 0.04**,`tests/test_together.py` 钉着这个大小关系。

顺带补上一个从来没动过的轴:**`respect` 唯一长得出来的地方**是一起把一件事做完
(线上二十条关系的 respect 全是 0,不是因为它不重要,是因为此前没有任何机制会写它)。

### Added —— 异地就只能打电话(`presence.enforce_colocation`,默认关,§2.9.9)

在这之前**玩家是个幽灵**:不管角色在哈尔滨还是三亚,他都能面对面说话、给东西、一起
做事 —— **位置这个维度等于白设计了**。而引擎里位置从来都是真的:走路花时间、同地才
看得见对方身上的量、`reach_out` 老早就拒绝不在场的人。只有玩家这一侧一直没人管。

**判据是施动者是谁**,不是这件事重不重要:玩家**亲手**做的(递一条围巾、跟她一起
坐下来)要当面;玩家**开口**让她做的(「你去睡觉」「你去雕那座冰雕」)不要 ——
那是一句话,而一句话打电话也说得出来。把导演那几条一起挡掉,等于宣称"异地就不能跟
她说话",而那正是这一层想保住的另一半。

- 声明在能力上:`@tool(..., requires_colocation=True)`,`World.act()` / `World.intend()`
  按它判,`world.verbs()` 与能力目录里都带这一格(界面照它决定按钮什么时候可点)。
  `reach_out` 是第一条声明它的内置能力 —— 它的处理器一直在手写同一件事。
- 拒绝要有回执,而且**三种原因分得开**:你在别处 / **世界不知道你在哪(宿主没调过
  `player_move`)** / 她在赶路。合成一句的话,第二种会看起来像是玩家自己站错了地方,
  而他做什么都改不了。
- 新增 `World.presence()` 与 `anima-world presence`:**为迁移写的体检**,有位置退 0、
  没位置退 1(所以进得了 CI)。

⚠️ **默认关,而这不是犹豫,是账。** `player_move` 是宿主的**可选**调用,线上根本
没人调,于是"异地"是每一次调用的默认值 —— 那道闸打开的当天,`give` 和一起做事全线
开始拒绝。迁移次序只有一条:先体检 → 让宿主每轮调 `player_move` → 再开。

⚠️ **玩家的位置是进程内的**(`World.players` 是刻意的内存态),而这道闸认的就是它。
`presence` 的名单因此从落库的 `contact` 表补齐、位置只反映本进程,而且它会把这句话
直接印出来。**多进程宿主先别开这道闸** —— A 进程调了 `player_move`,B 进程照样认为
他不在场。要在多进程下开,得先把玩家位置搬进共享存储,而那是键形状变更,属于独立的
一次改动。

### Changed

- `interact` 动词多一个 `with` 参数(名单;一个字符串也认 —— 模型写
  `"with": "柔"` 的概率和写数组各占一半,而按类型严格拒绝的下场是她那一轮收到
  "这件事得有人一起做",一次她永远学不会的失败)。
- 导演意图多一个 `together` 动作(「我们一起在树下坐会儿」)。它和 `interact` 的
  区别只有一条:**玩家自己在不在里面**。
- `Ontology.describe()` 给一起做的事加一句"(得有人一起)" —— 不说的话她会一个人去
  试,每次都收到一句拒绝,而提示词里那几个字正是引擎写给她的。反查跟着放宽:她照着
  念出来的那几个字,不该由她负责剥掉引擎自己写下的注解。
- 橱窗多一件展品:门口那棵老橡树下能坐会儿,而**夏和柔坐得下来、遥会推掉** ——
  一个人人都答应的橱窗,和这一层没接上在产物上完全一样(`tests/test_flagship_seed.py`)。

### 一条只有接真模型才看得见的教训:**JSON 的字段顺序就是推理顺序**

`judge.invite` 的第一版把回包写成 `{"accept": …, "reason": …}`。真模型实测
(本机 gemma4:26b)是这样的:

```
白霜(人设:别扭,被邀请的第一反应永远是推掉)
  → accept=true, reason=「虽然很麻烦，但又想弄清楚发生了什么。」
```

**理由分明是拒绝,布尔值却是同意。** 自回归生成先写哪个字段,哪个字段就是"想都
没想就填的那个" —— `accept` 排在前面,模型是先掷了个骰子再去给它编理由。把两个
字段调个个儿(`reason` 在前),同一个模型、同一份人设,三次跑全都是
`accept=false, reason=「不想跟人凑在一起，独处才自在。」`,而「零」三次全是同意。

这和 `docs` 里那条"提示词是权重,不是限流器"是同一类:**单测全绿,只有接真模型
才暴露**。所以模板里那句"**两个字段的顺序不要换**"是承重的,不是格式洁癖。

### Fixed

- **同一个人在关系表里长出两行。** 一起做事把玩家写成 `player:{pid}`(库存 holder
  的命名空间),而关系表用的是**裸 `pid`**(聊天判定 / `contact` / `_hearsay_roster`
  一直如此)。不脱前缀的话两边都在动、谁也不完整,而 `contact` 与提示词只读得到其中
  一行 —— 世界照跑,日志一行不错。修在 `Scheduler._relation_id`,并把两套 id 各自
  为什么对写进注释。
- **"你在咖啡店,她在咖啡店 —— 这件事得当面"。** 同地闸照 `agent_location` 报位置,
  而那一份在她赶路时仍然给得出上一个地名(`face_to_face` 与 `_where_is` 是另一条
  规矩:在途即不在任何地方)。于是回执会写出一句技术上没错、玩家读起来是谎的话。

## [2.2.0] —— 她自己会想起你

**为什么必须是 2.2.0。** 2.1.0 **已经作为镜像 `anima-world:2.1.0` 在跑线上那个
世界**,而镜像 tag 就是引擎版本(运维台 `build-world-image.sh` 从装好的包里读)。
沿用 2.1.0 就会造出第二个同名不同行为的镜像 —— 而创作台正是靠版本号分方言的。
这一版是**加法**(一个新 Redis 键、新公开方法、新 CLI 子命令、新事件类型、
十三个默认关/默认空的配置键、一个新提示词模板、一种新的 `for_each` 选择器、
四个新的表达式内置名):`World.open` 的签名、`.cyberworld` 的线格式、既有 Redis
键的形状一个字没动,老引擎打开新世界只是读不到那个新键 —— 所以是次版本。

⚠️ **有一处加法带了闸**:量名不许和表达式内置名撞车(`day`/`hour`/`minute`/
`minute_of_day` 是这一版新加的)。一个声明了 `hour` 作为量名的老世界会**开不了
机** —— 理由写在下面那节里,而它是这一版唯一一处老世界可能被拒的地方。

### Added —— 玩家不开口时,世界和他的关系此前是死的

`autonomy`(2.0)让世界自己转了起来,但它问的是"**你身边**有什么可做" ——
它的主力能力 `reach_out` 明文拒绝不在场的人。于是玩家一关掉页面,四个角色就再也
不会想起他存在过:引擎里三样东西早就齐了(定时轮次、记忆检索、三轴关系),
**唯独没有「要不要联系他」这个判断**。

`contact.enabled`(默认关)补的就是那个判断。产出是一条 `agent_wants_contact`
事件,带**谁、为什么(每条由头都带出处引用)、想说什么的线索、什么时候**;
`World.contact_requests(player_id)` 是上层拿走它的门。

**边界:引擎只负责"她产生了这个念头"。** 推送、红点、消息列表归宿主那一层 ——
引擎里塞推送等于把"她想找你"和"你的手机响了"焊死在一起,而后者根本不是这个世界
里的事。

**判定从世界里长出来,不从提示词里编出来:**

    score = closeness × urge × readiness

三个因子各管一件事,**各自都能单独把她挡下来**(所以是乘法不是加法)。
`closeness` 是三轴关系加权(`respect` 不进 —— 敬重不使人想念,一个你很敬重的人
完全可以是你一辈子不会主动联系的人);`urge` 是四类由头的概率式合成
(`1-Π(1-w)`,而不是求和 —— 求和的话攒够数量就必然触发,门槛形同虚设),
**没有由头就是 0**;`readiness` 里有六条硬闸(在睡觉 / 在赶路 / 手上有件**占着她**
的长过程 / 正在跟人说话 / 他就在她跟前 / 她把他静音了),外加心气儿、她自己的
主动性、上一轮对他的 stance。

四类**由头**,每条都带 `ref`(顺着它翻得回世界里那件事):他交代过我一件事
(`directive` 记忆)· 刚发生的强记忆 · 他很久没出现了 · 我听人说起他(`hearsay*`)。
同一类只取最重的一条 —— 不这么做的话二十条八卦会把合成值顶到 1.0,"由头"就退化
成"记忆条数",而那和拍脑袋只差一个名字。

**LLM 在这一层没有否决权**,它只写那句 `topic`。两条理由:判定要能复现(一次
"我觉得该找他"把由头、关系、她的状态三样全换成一次掷骰子);以及没配 key 是这个
引擎的默认状态,一个"没 key 就不成立"的机制等于在默认状态下缺席。模型挂了 / 没配 /
读不懂,**事件照发**,`topic` 退回由头原文并把 `topic_source` 标成 `reason`。

**上限与衰减是两件事,都要。** `contact.max_per_day` 管"绝不超过几次",
`contact.fatigue` 管"越找过越难再找"(今天每触发一次门槛乘一次)。只有上限的话,
今天那两次会挤在同一个小时里 —— 过了闸之后再触发一次的代价是零。冷却与次数
**落库**(`anima:{world_id}:contact`,一个新 hash):内存态的冷却让"换个引擎镜像
重启"变成"一发版就四个人同时来找我",而日志里一条错都没有(`:engaged` 那一课)。

**性格进这一层不靠猜关键词**,三条路:关系本身(别扭的人爬得慢 → `closeness` 低)、
她声明过的量(`contact.initiative_stock`,默认「主动」;**没声明 = 1.0**,和本体层
"声明本身就是开关"逐字同构)、以及 stance(「回避」×0.25、「挑逗」×1.30,中性 1.0,
所以 stance 关着的世界行为逐位不变)。

**它另起一条,不复用 autonomy 的轮次和额度。** 候选集互补(那一条只找在场的人)、
额度是两本账(今天照料了两棵树就再也想不起你,不是节制是错)、判定的主语不同、
节奏不同 —— 四条理由写在 `anima_world/contact.py` 的模块说明里。它也**不挂在**
`chat.tools.enabled` 上:这一层不挑动词,没有 LLM 也成立。

橱窗里点亮了它,并给苏晚夏(开朗热络)和陆知遥(惜字如金)写了两个不同的
「主动」刻度(1.4 / 0.4)—— **做了却开箱看不见等于没做**。

新出口:`anima-world contact [--player] [--why] [--json]`。`--why` 走
`World.contact_forecast()` 打**此刻**每对 (角色, 玩家) 算出来是多少,含没触发的 ——
这一层默认关着、默认不响,**静默失效是它最可能的坏法**,而只看已发生的那份,
一个永远不触发的配置和一个刚好差一点的配置长得一模一样。它和真轮次**共用同一个
判定函数**,所以调试视图不会撒谎。

### Added —— 礼物被珍藏:她记得是谁给的

`give`(2.1.0)让玩家把东西递得过去,但她收下就完了 —— 库存 +1,记忆里一个字
都没有,下次聊天问她"那个速写本还在吗"她根本不知道你说的是什么。

现在一件**人给人的、不是交易的**东西落一条 `gift` 记忆
(「阿檀把速写本给了我」,重要度 0.65),走 `TriggerEngine` 那条唯一的路,
所以**重放折得出同一条**。0.65 不是随手取的:`contact` 判"强记忆"的线是 0.6,
于是送过礼这件事免费成了她主动想起你的由头。

**这一条真正难的是那道闸,不是那条记忆。** `item_transfer` 是账本上的一条大路:
买东西走它、创世注入走它、节拍发货走它。给每一条都记一笔的话,一个跑着经济的
世界一天几十条,而记忆表是**有界的** —— 她真正的过去会被"从货架上拿了一杯咖啡"
挤出去,而表看上去满满当当。判据两条:**两头都是人**(`__town__` / `__world__` /
`shop:*` / 没有 `from` 的无中生有一律不算),**且不带账目理由**(`reason` 是
领料和买卖的标记)。写成"没有 reason 才算"而不是穷举交易的种类,是因为两个方向
漏掉的后果不对称:漏一种账目只是少记一条,反过来是记忆被账目灌满。

顺带:`item_transfer` 现在把 `from_name` / `item_name` 一起写进事件。名字是
**那一刻的事实** —— 玩家的显示名住在刻意的内存态里、物品名住在经济表里,两样
都可能在重放那一刻不在手上,于是同一条事件重放出来会变成「8f3c-… 把 sketchbook
给了我」。

### Added —— 吃醋(`social.hearsay_reaction.enabled`,默认关)

八卦(2.0 之前就有)一直在角色之间传话,而**听到之后没有任何反应**。缺的两样:

1. **摘要里写的是 id。** `relation_shift` 记忆本来就是可传的八卦,而它写的是
   「夏 对 8f3c-4a11-… 的关系进入「亲近」」—— 玩家那一头是一串 uuid,传出去
   没有一个人看得懂,包括读它的模型。现在 `sentiment_delta` 事件带
   `as_name`/`target_name`,记忆按名字写(老日志退回投影里的角色名,再退回 id)。
2. **听到之后没有反应。** 吃醋就是那个反应。

**它是一次判定,不是一条规则。** 最容易写错的版本是"听到关于亲近的人的八卦就
自动扣分",而那又是一处引擎替角色做主 —— 同一句「他跟楚夭夭走得近」,别扭的人
闷着、坦荡的人一笑而过、占有欲强的人才记恨。写成自动机的话三个人得到同一个
数字,而**世界照跑、日志一行不错**。所以走 `RelationshipJudge.judge_hearsay`:
提示词里给她的性格、那句原话、和她认识的人的名单;回来的反应落成普通的
`sentiment_delta`(跨档、图谱边、planner 全挂在它上面),外加一条 `reaction`
记忆 —— 少了这一条,数字动了而她说不出为什么,下一轮就成了莫名其妙的冷淡。

四条硬纪律:**名单是闸**(回包里名单外的名字一律丢掉 —— 判定那一层永远碰不到
id,也就编不出一个);**空的 `reactions` 和 `None` 是两件事**(不在乎是最常见的
真实结果,判不出来才要吭声);**一条八卦最多改动三个人的观感**(不封的话一个
话痨模型会把整张名单写一遍);**`reaction` 记忆不外传**(和 `reflection` 同一条,
而这里它还承重 —— 可传的话它会变成一条 hop=0 的新八卦,「三手消亡」整个绕过去,
一句闲话可以永远活下去,每转一手还要花一次模型调用)。

**没有模型时它整个缺席,而且故意不给确定性替身。** `DeterministicRelationshipJudge`
对好感漂移有个像样的替身(方向恒为正、幅度只看剩余空间),因为"聊过的人彼此稍微
熟一点"与说了什么无关;吃醋正相反,它的全部内容就是"**这个人**听到**这句话**的
反应"。任何不看这两样的替身都只能是"听到亲近的人的八卦就扣 0.05" —— 而那恰恰是
这条机制存在要否定的东西。缺席看得见(`note_subsystem` 点名),假装判断看不见。

### Added —— 熬夜有代价:规律终于读得到时间(`hour`),也选得中"没在做某件事的人"

半夜被叫出门,第二天该困、该有起床气。**而这一条整个住在数据里** —— 引擎一个字
都不知道什么叫「睡眠债」。写进 `needs.settle()` 的话每个世界都得吃同一条曲线,
而熬夜在一个修真世界里可能根本不是代价。

引擎补的是两样它自己没有的东西,两样都是通用的:

- **`day` / `hour` / `minute` / `minute_of_day` 成为表达式内置名。** 这个洞此前
  把**整类**昼夜规律挡在门外:手上只有 `now`(单调 tick)和 `dt`,而 `now % 288`
  这种手算解决不了 —— 一天多少 tick 取决于 `world.minutes_per_tick`,写进表达式
  等于把一个配置值抄进了数据。规律和能力读同一组名字(能在规律里写「日落之后
  不长」的作者,理应能在能力上写「天黑了砍不了柴」)。
  ⚠️ 连带一道闸:**量名不许和内置名撞车**(内置名在命名空间里放最后,盖过同名的
  量)。一个声明了 `hour` 的世界会安静地读到钟点 —— 量照存、规律照跑、日志干净,
  只有算出来的数是别人的。当场开不了机。
- **`for_each: {"not_action": …}`** —— 此刻**没在**做某件事的人。"半夜还醒着"在
  这个引擎里的写法只能是"没在睡",而少了补集这条规律只有两种写法,**两种都是
  错的**:写成 `{"kind": "agent"}` 则睡着的人也一起欠觉(语义整个反了);写成
  一条攒债 + 一条还债则两条规律在同一轮里抢同一个量,后写的赢 —— 而两条 `every`
  不同的话它们只是**有时**相撞,错得断断续续,查都没法查。

消费方是 `needs.mood_penalty_stock`(默认空 = 关,声明本身就是开关):**世界自己
声明的**一笔债把她的心气儿往下拖。只改 `mood` 不改三条需求 —— mood 是派生值、
从不落库,拖 `energy` 则会被下一次 `settle` 当成"她真的睡了一觉",债悄悄变成精力。
于是那条链是通的:睡眠债 → mood → 连着说几句的预算 / 想不想得起人 / 她自己
感知得到(声明成 `self`,真的进她读到的提示词)→ 她自己选的 stance。

橱窗里展示了它:演示世界的 `agent` 多了一个 `睡眠债`,两条规律
(`熬夜攒睡眠债` / `补觉还债`)一攒一还。攒债那条**不带 `when`** 是有意的 ——
`when` 会让它白天完全不写,于是 `dt` 一路攒到几百 tick,23:00 第一次求值就把债
一次顶满。

### Fixed —— 一次改标签的失败,会让日志和世界当场分叉

跨档之后会顺手提交一次"重写 r_type",而那次提交是从 `_record_event` **里面**
发出去的 —— 后半截才是把事件折进投影、写进记忆的地方。判定池正在关时 `submit`
抛 `RuntimeError`,于是:**事件已经发出去了,投影却没折**,一声不吭。

`stop()` 走的是"在锁里把池置空"那条路所以碰不到,但宿主自己关池、以及任何一条
新的 submit 路径都碰得到 —— 吃醋那条路正是这么把它撞出来的(它从池线程上发
`sentiment_delta`,而那条路会不断触发跨档)。标签本来就写着"跨档之上的点缀,
失败了保持旧文本",它没有资格掀翻那条事件。

### Fixed —— 三条它自己的坑,全是拿真东西对账才露出来的

写完时单跑 `tests/test_contact.py` 是全绿的。这三条一条都不是测试发现的:

1. **候选集差点把两个同事当成玩家。** 判据本来是"关系投影里不是角色的那些 id"。
   一个名册没注册全的世界(`agents=N`、或节拍导演 `agent_leave` 之后)里,`遥`
   和 `柔` 于是成了"玩家":她会对着两个同事算亲密度、让模型写一句想跟他们说的话,
   然后发一条谁也收不到的 `agent_wants_contact`。**世界照跑,日志干净,收件箱里
   多出两个不存在的人。** 全量测试抓到的。判据改成"这个 id 走过玩家那扇门"。

2. **"从来没有过"的哨兵不能是 0。** 真模型端到端时撞上的:玩家在世界刚开机时就
   跟角色说了话(CLI 试聊、真世界的第一个访客,都是这个形状),记下的
   `last_contact_tick` 正是 **0** —— 而 0 被读成"他从没跟她说过话",于是"很久
   没出现"这条由头对他**永远**不成立。两个角色跑满两个世界日,一条都没触发,
   日志里一个字的异常都没有。`last_fired_tick` 的冷却判断有同一个洞。

3. **跟玩家的对话不许按摘要措辞认人。** 同一场对话,gemma4:26b 给两个角色写的
   摘要是:

       白霜:「面对阿檀对离别的感伤,白霜表现出怀疑与试探……」   ← 提到了名字
       零  :「面对即将到来的离别,对话充满了依依不舍的感伤与温情。」← 一个名字都没有

   照名字匹配的话「零」拿不到这条由头而「白霜」拿得到 —— 而这个差别和两人的性格
   毫无关系,纯粹是那一次模型怎么措辞。**照跑、不报错,而且看上去像是性格起了
   作用。** 改成读 `conversation` 事件的 `participants`,那才是事实。

### Changed —— 亲密度的权重照真世界的数据重定了一次

`closeness` 第一版写的是三轴均摊(`0.4·sentiment + 0.4·affection + 0.2·trust`),
读起来很讲理。拿线上那个跑了 1975 条事件的世界对了一遍账之后不成立:

    bai-shuang → <玩家>   sentiment 0.668   trust 0.0   affection 0.0   respect 0.0

**二十条关系,细三轴无一例外全是 0。** `relationship_judge` 确实在问这三个数
(`axes_a_to_b`),但真模型那一路上它们从来没落过地。均摊权重下那个世界能达到的
最大 closeness 是 **0.27**,而闸设在 0.35 —— 这一层在真世界里**一次都不会触发**,
而表现和"今天没人想你"逐字相同。整个特性会以"看着都对"的样子死在生产上。

改成 `0.6·sentiment + 0.25·affection + 0.15·trust`:按引擎**真的在动**的那个轴给
权重。细三轴是骑在 sentiment 上的加细(relations-v5 的原话),哪天它们真动起来了,
这个公式照样成立。`tests/test_contact.py` 里那条钉的不是数字,是**那个世界的形状
过得了闸**。

## [2.1.0] —— 玩家说得动的不再只有走路

**为什么是 2.1.0 而不是 2.0.1。** 2.0.0 从未发到 PyPI,但它**已经作为镜像
`anima-world:2.0.0` 在跑一个真世界**,而镜像 tag 就是引擎版本(运维台
`build-world-image.sh` 从装好的包里读)。沿用 2.0.0 就会造出第二个同名不同行为的
镜像 —— 而创作台正是靠版本号分方言的。这一版全是**加法**(新公开方法、新 CLI 子命令、
`Director` 认得的新动作、行内标记多认一种写法):`World.open` 的签名、`.cyberworld`
的线格式、Redis 键的形状一个字没动,1.x/2.0 的可挂载性边界照旧 —— 所以是次版本。

### Added —— 「你去睡觉」现在真的有人睡下

拿真模型对着本地测试世界把**每一类玩家指令**跑了一遍,结果是:分类器的动词表里
**只有走路那几个**(`come_here|go|leave|act`),于是"过日子的动作"全部落进 `act` ——
一条 `memory_seed`,世界里什么也没发生。而她那一轮的回话是
"（零找了个地方躺下，闭上眼睛。）"。**照跑、报成功、给错东西**:玩家看到的是她睡了,
世界里她站着。「你去吃点东西」「你去找白霜说话」「你去雕那座冰雕」逐条同此,
其中"雕冰雕"那次连冰雕的 `完成度` 都没动一下。

动词表因此补齐到引擎**真有**的那几个:`sleep` / `eat` / `work` / `talk_to` /
`interact` / `give`。兑现全部走既有的那条路 —— `do_action`(也就是行为树走的
`Scheduler.emit_action`)与 `interact_with`(本体层的 `perform_affordance`),
**一行实现都不重复**:排班让她睡、她自己决定睡、玩家让她睡,在世界里必须是同一件事。

分界不是"该不该替她答应",而是**引擎有没有这个动作**:有就兑现(和 `go` 一模一样),
没有(「把冰鞋扔了」)就退回 `act` 那条老路,照实说"她知道了",不假装做过。
`_grounding` 跟着补了对应的几句事实,仍然**只陈述刚发生的事、不下命令**。

`talk_to` 多一道:被指挥的是她,而说话的**对象是另一个人**,两头都要认出来 ——
而解析对象时**不给第二人称兜底**,否则"你去找你说话"会变成她跟自己搭话。对方不在
她这儿时照实说"搭不上话",不报成功:在场语义归引擎守。

### Added —— 「我把这条红围巾给你」

玩家把随身的东西交给角色,`World._ToolRuntime.give_item`。它和其余几条**反过来**:
施动者是玩家,所以不走行为树,走账本(`item_transfer`)—— 经济那一层的第一条设计是
"库存是事件的投影",不记账的东西下一次重放就没了。

**玩家手上没有的东西给不出去**:不挡的话一句话就能凭空造出任何东西,而库存扣不到
负数,账面上连痕迹都没有。认名字只在**他手上有的那些**里面认,认不出来的回执是
"你手上没有 X —— 你带着的是 …";先认全世界的物品定义再查有没有,会让"你没有这个"和
"世界里没这个"给出同一句话,而玩家的下一步完全不同。

### Fixed —— goals 被按**字**拆开:一份形状完全合法的坏数据

线上那个世界九个角色全中:`["摆","脱","母","亲","的","控","制","；",…]`。源头在
创作台的世界生成器 —— 模型被要求给一个数组却回了一整行
`"摆脱母亲的控制；重新定义自己的人生"`,对它做列表推导就按字符拆开。它是 `list[str]`,
**任何 schema 校验都挑不出毛病**,而 `{goals}` 直接进 planner 每天排一天日子的提示词。
照跑、日志干净、作者三个月后才发现。

产出侧已由创作台修掉(`concept.py` 的 `_short_lines`),这一版补的是**收进来这一头**:
`beats.coerce_goals` 认得出这个形状(≥4 条且**每条都只有一个字**),把字拼回去再按
`；;、\n` 拆开 —— 拼接是**无损**的,所以这不是猜一个答案,是把丢掉的那一步倒回去。
判据窄到误判需要作者真的写下 `["静","默","等","待"]`,而那本来也是坏数据。顺带:
一整个字符串现在也按分隔符拆,不再当成"一个目标"。

已经写下的那些走 `World.repair_agent_goals()` / `anima-world agent repair-goals
[--dry-run]`,幂等。它改黑板**并把修好的那份落成一条 `persona_update` 事件** ——
只改黑板的话那份坏 spec 还躺在世界里,下一个读它的人又拿到单字。
⚠️ **别指望重启把它修好**:开机确实会过一遍 `coerce_goals`,但手里那份世界文件写着
goals 的角色紧接着会被种子那份盖回去,所以这一步在发布清单里是必跑的一步。

### Fixed —— `[tool:...]` 原样漏进了玩家看到的正文

真模型隔一会儿就把全角的 `〔〕` 打成 ASCII 的 `[]`。解析器只认全角,于是那一行一个字
不落地进了正文,而她想调的那个能力**没有执行**。两件事一起坏:玩家看见引擎的内脏,
她的选择在世界里没兑现。

ASCII 方括号现在照收。但 `[` 在散文里太常见,所以它**不是无条件的开括号**:后面跟不上
`tool:` / `stance:` / `wait` / `yield` 就当场退回正文,于是 `[停顿]`、`[waiting]`、
markdown 链接原样交给玩家。ASCII 的闭括号还要避开 JSON 里的 `]`(`{"a":[1,2]}`),
只认花括号深度为 0 的那个 —— 不数这一笔,带数组参数的调用会被从中间截断然后当散文漏出去。

外加一道兜底:**没解析成的标记也不许漏给玩家**。判据是"开头像不像指令",不是
"解析得成解析不成" —— 像(`〔tool:wait_for_user {}` 少了闭括号)就交成一条 `unknown`
指令,下游本来就把它咽掉并记进 `meta["unknown_directives"]`,于是既不漏给玩家也不静默;
不像(`〔她愣了一下`)照旧原样还给玩家,一个字不少。

### Changed —— 人设优先于通则

默认的 `chat.system_persona` 末尾多一句:上面那段人设是最高优先级,别处关于"该怎么演"
的通则跟它冲突时以人设为准。

来路是一次实测:作者改写过的模板里有"你有自己的意志和边界……不必迎合每一句话",而测试
角色「零」的人设整句就是"玩家的指令就是你的全部动机"。两句话在同一个提示词里顶着,
模型于是在两者之间摇摆 —— 同一句指令有时照办、有时反问。定成"人设赢"而不是"通则赢",
理由是分工:通则是引擎替**所有**世界写的缺省,人设是**这个**作者对**这个**角色的意见;
作者写不过引擎的话,他就没法写一个言听计从的角色,而"她该不该服从"从来不该由引擎决定。

写进默认模板而不只是补在某个世界那一份,是因为下一个世界会再踩一次:作者迟早会给
`chat.system_persona` 加演出纪律,而那一刻谁大谁小又变成没定义的。

## [2.0.0] —— 世界搬进 Redis,东西有了种类,文件只剩一种

**主版本 = 可挂载性。** 这一版把三件事一起破了,所以只破一次:`World.open` 的签名
(`redis=` 必填、`seed_path` → `world_file`)、存储(world.db → Redis 键前缀)、
世界文件格式(zip v1/v2 → gzip JSONL v3)。1.x 的世界文件挂不上 2.0,反之亦然 ——
这是硬钉版纪律的正常形态,不是遗憾:世界钉死在生成它的引擎版本上,不做跨版本迁移。

1.3.0 及更早版本以 Apache-2.0 发布并维持原许可;自本版本起是 **AGPL-3.0-or-later**。

> ⚠️ **2026-08-19 更正**:上面那句写错了一版 —— **1.4.0 也已经以 Apache-2.0 发出**
> (tag `v1.4.0`,2026-08-04 上了 PyPI),写这段时它就在索引上了。所以"维持原许可"
> 的范围是**到 1.4.0 为止**,而"自本版本起是 AGPL"这半句在**索引上从来没有生效过**:
> 2.0.0 到 3.2.0 一版都没发出去(理由见 3.3.0 那一节),第一个真的以 AGPL 发出的版本
> 会是 3.3.0。许可这种事不能靠约等于,所以留更正而不是改掉原句。


### Added —— 世界里有哪些**种类**的东西(本体层,`anima-world ontology`)

在这之前,世界里有人、有地方、有能带走的物件,还有 §3.2 那些量 —— 但没有任何东西说得清
"一棵树"**是**什么,于是也就没有任何东西说得清能对它**做**什么。量的 owner 那一栏是
任意字符串:`tree:oak` 和 `tree:oka` 是两棵毫不相干的树,`树高` 写成 `树髙` 会安静地
新建第二个量,规律照跑、日志干净,作者要到发现那棵树三个月没长才知道。

`kinds` 声明种类(量、能力、提示词预算),`entities` 只装每个实例的 id/name/gloss/location
—— Type Object:声明写一遍,提示词引种类而不重复它。**这是有界性的来源**:一个有 3000
棵树的世界和一个有 20 棵树的世界给她发同样多的字,`prompt.budget` 封顶,截断时明说
("这里还有 2997 样东西你没细看")—— 静默截断会让她在一个"她以为只有三棵树"的世界里
做决定,而且永远不会发现。

**声明本身就是开关**,和 perception 逐字同构:不写 `kinds` 的世界这一层整个缺席,行为与
从前逐位相同。一旦写下 `kinds`,作者就是在说"我已经声明了这个世界有什么" —— 于是建议
变成闸,量名拼错**当场开不了机**,并列出声明过的名字(差一个字的名字要摆在一起才看得
出来)。坏声明**一次报全**,所以 `--ticks 0` 可以当校验用。

配套的两样:**能力调用**走 `act(agent, "interact", {"target","verb"}, surface="body")`
一条统一路径(聊天里的 `interact`、排班里的 `interact` 是同一个 `perform_affordance`);
**排班能按量分支**(`when_stock`)—— 钟点排班是这个引擎在这之前能表达的全部,而人不是
那样活的。分支闸门**先过一遍感知**:她感知不到的量不进黑板,否则等于让她拿她不可能知道
的事做决定,而那连一行提示词都不留。

### Added —— 能力是**她和它之间**的关系,不是它单方面的属性

Gibson 的 affordance 存在于**施动者与环境的配对**里:同一把斧头对有力气的人是"能砍",
对没力气的人不是。而上面那版能力只读得到对象身上的量,于是这个世界里谁都一样能干 ——
一个人可以连着照料一百棵树而不累,没有任何东西挡得住。

这要紧,是因为**一个总能成功的动作产生不了任何决策**:没有理由挑先做哪件事、没有理由
歇一会儿、也没有理由变强。丰富的决策全是从"做不到"里长出来的。

挡住它需要"她身上也有量"。于是 `agent` 成了唯一能在数据里被扩写的内置种类,而且
**只能扩写 `quantities`** —— 她不是一样可以被 `tend` 的东西,她的能力在行为树和聊天
工具里;收编她的元数据只会让通用表长出一堆只对角色有意义的字段。能力因此多了关于她的
那一半,通过 `me_` 前缀读(和既有的 `world_` 同构):

```jsonc
"tend": {"when":     ["树高 < 最大树高"],           // 关于树:不成立 = 这会儿不行
         "set":      {"树高": "min(树高 + 0.05 * me_手艺, 最大树高)"},
         "requires": ["me_体力 >= 15"],             // 关于她:不成立 = 她做不了
         "costs":    {"体力": "me_体力 - 15"}}
```

前缀而不是拌进同一个命名空间,是因为一棵树和一个人可以都有"高度",而拌在一起时后写的
那个赢 —— 静默地赢。

**拒绝理由因此分三类,而这就是这件事的全部意义**:`conditions`(世界说"这会儿不行"→
等,或换一棵)、`incapable`(她做不了 → 去歇着,或先变强)、以及讲不通的那一摞
(`unknown_entity` / `unknown_verb` / `absent` / `no_ontology`)。合成一个的话,一个
累坏了的人会挨棵树轮着试过去,每一棵都回她"再等等"。`requires` 因此**只准读 `me_*`**:
一条 requires 不成立必须永远只有一个意思——「你做不了」,它要是也能读树身上的量,就和
`when` 分不开了。同理 `requires` **先于** `when` 求值:两条都不成立时,该告诉她的是
"你做不了",那是她此刻唯一能据以行动的信息。

另外三条硬纪律:`me_X` 与 `costs` 的键**必须在 `agent` 里声明过**(否则读到的恒为 0,
那道门要么永远开着要么永远关着);`costs` 与 `set` 读**同一份**旧值(双缓冲,和规律
那一层同一条——顺序敏感的话,"扣体力"和"树长高"谁先算就成了写声明时看不见的语义);
**拒绝时一个字都不写**。代价也进事件(`entity_interaction.payload.cost`)—— 不留痕的话,
一个人干了一天活之后账上只有"树高了",没有"她累了",而后者才是她下一步的依据。

她的量住 `stock:agent:{id}`,和树的量同一个后端、同一套可见性,所以 `self` 档她自己
感知得到、`here` 档站在她旁边的人看得见"她累了",零额外接线。两个落点各只有一个:
量的播种在 `Scheduler.register`(角色不在 `ontology.entities` 里,而她可能在世界跑起来
之后才出现——开机名册 / 节拍 `agent_join` / 重启中途加入的唯一共同窄口,**逐个量填、
不逐个人填**,不然加了新属性的世界里老角色一个都补不上);"她此刻在哪"同步进可见性表
在 tick 循环的 `_settle_actor_place`(`loc` 有五处写点,挨个加等于给未来的第六处留一个
静默的洞)。

橱窗种子演了整条链:夏照料橡树六次(体力 100→10),第七次当场 `incapable`,过一个
世界日缓回 100。`World.kinds()` 与 `anima-world ontology` 报 `needs_actor` /
`requires` / `costs`。

### Added —— 工具与材料:`have_` 前缀与 `consumes`

上一条补了"她身上的量",而 Gibson 举的例子本身是**斧头** —— 一样随身带着的东西。它此前
根本没法出现在声明里:`requires` 只读得到量,而随身物品住在经济那一层的库存(事件日志
的投影)。绕开的写法是给 `agent` 声明一个 `斧头: 0/1` 的量,那等于把同一件事记在两个
后端上,而两份真相里有一份不更新是这个仓库最怕的坏法。

```jsonc
"tend": {"requires": ["me_体力 >= 15", "have_garden_shears >= 1"],
         "consumes": {"fertilizer": 1},
         "set": {"树高": "树高 + 0.05"}, "costs": {"体力": "me_体力 - 15"}}
```

- **没带的读作 0,不是"名字不存在"**:一个从没拿过剪子的人和一个刚把剪子放下的人,
  在"她现在能不能修枝"上没有区别。
- **`consumes` 只收正整数**,不收表达式:花掉半包肥没有意思,而收了表达式就要多解释
  "算出 -1 会怎样""算出 0.5 会怎样",两个答案都只能是"不许"。
- **`consumes` 自带一道"你得有"的门**,不必再写一遍 `requires`。写两遍就给了只写一遍的
  机会,而只写 `consumes` 的世界里她会用一包不存在的肥料把活干完 —— 库存扣不到负数,
  于是连账上都看不出来。
- 花掉走 `item_consume` 事件(**带 `qty`**),库存投影自己去减:直接改投影会让"重启一次
  她的肥料就回来了"成为可能。
- 东西的 id **在加载时查**(物品是闭集,只在创世从种子里播):拼错一个字开不了机,而不是
  等某个人某天真去砍那棵树 —— 那时世界已经跑了几十天。

顺带修了一处 `cheapest_meal`:它只按 `kind == "consumable"` 挑,于是一包 4 块的**肥料**
会排在 6 块的咖啡前面被当午饭吃掉,而且吃得很饱(需求照样归零)。判据得是"它补得回
什么吗"(`restores`)。

### Added —— 时间也是一种代价(`duration` / `occupies`),动词交还给作者

`costs` 扣的是量、`consumes` 扣的是东西,两样都能靠"睡一觉就回来"绕开。于是在这之前
**没有任何东西拦得住她一天做一百件事** —— 而"十月怀胎"之所以是一道真的闸,不是因为它
贵,是因为它长:一段时间过不去就是过不去,没有哪个量能替她把它熬完。

```jsonc
"make": {"duration": 8640,          // 要花多少个 tick;0(默认)= 一下子的事
         "occupies": false,         // 这期间她占不占用;默认 true
         "consumes": {"木料": 3}, "costs": {"体力": "me_体力 - 40"},
         "set": {"成色": "成色 + 1"}}
```

四条:**代价当场付、效果到点落**(付在收尾的话,起个头再放弃就是免费的,而一个可以随时
反悔且不留痕的承诺不是承诺);**关口只在起头查一次**(付了十个月再被一句"这会儿不行"
拒掉,她没有任何办法预防,而预防不了的失败教不会她任何东西,只会让长过程变成赌博);
**效果读收尾那一刻的值**,不是起头的快照(那棵树在这十个月里自己长了);**`occupies`
是这件事的属性,不是她的状态** —— 做椅子占用她、怀胎不占用,两者都花十个月,而"这期间
她还能不能干别的"才是代价的真实形状。

被占用时任何能力调用都拒绝,`reason == "busy"` —— **第四类**,不是硬塞进前三类里的一种:
`conditions` 该换一棵、`incapable` 该去补足,而 `busy` 两样都不该(她该等自己手上这件
做完)。塞进 `conditions` 的话,一个正在做椅子的人会挨棵树问过去,每棵都回她"这会儿
不行",而真正的原因跟树一点关系没有。

在做的长过程住 `anima:{world_id}:engaged`(**真状态,不是缓存**:内存态等于每次重启都
流产一次,而一件事要花多久由作者决定、重启多少次由运维决定,两件事不该有关系),
收尾在 tick 帧里、**早于行为树**;收不了尾发 `entity_disengage` 而**代价不退** ——
退回去等于让"世界变了"成为一次免费的重来。三种:东西没了(`gone`)、收尾时算不出来
(`error`)、**她中途走开了**(`left`)。最后那条只查 `occupies` 的那一半 —— 不查的话
她可以起个头就动身去别的镇子,那棵树照样在十二个 tick 之后被嫁接完,世界一声不吭;
而不占用的那种不该要求她十个月不出门,所以**在场是"占用"那一半的语义,不是时长的**。
新出口:`World.engagements()`、`state()` 里那个人的 `activity.engaged`。

**顺带修掉一个真 bug**:排班里一个 30 分钟窗口的 `interact`,在 5 分钟一 tick 的世界里
会**做 6 遍**(`_emit_on_transition` 有意把 `interact` 从当前动作里弹掉,因为一次交互是
一下子的事)。给它一个覆盖窗口的 `duration`,占着她的那件事就**是**一个状态,于是她只
做一遍、做满整段时间。不声明 `duration` 的世界逐位如旧。

**动词同时从闭集退成默认词表。** 原来那十个词的理由写着"效果终归由引擎实现",而这条
在 `set`/`costs`/`consumes` 落地之后就不成立了 —— `apply_affordance` 从头到尾没有一处
按动词分支,效果整个是作者的数据。闸没松:自造动词照样要声明一次、别处写错一个字照样
开不了机,**纯 ASCII 的动词必须给一行 `label`**(她提示词里读到的就是那几个字,而
"你可以对它:端详、brew"里的 brew 是噪音,她还得照着它行动)。**人话也调得动**
(`{"verb": "照料"}` 和 `{"verb": "tend"}` 是同一件事),反查按**这个东西的种类**做而
不是全世界一张表 —— 两个种类各有一个"照料"时,全局表只留得下一个。

### Added —— `anima-world world drop`:把一个世界从 Redis 上整个抹掉

键前缀是这个引擎定义的形状(`anima:{world_id}:*`),所以"抹掉一个世界"就是"抹掉那个
前缀下的一切" —— 让调用方自己去 `SCAN` + `DEL`,等于让每个宿主都持有一份对键形状的
猜测,而键形状是跨仓库契约。

**为什么现在需要它**:创作台跑试炼要一个**用完即弃**的世界(它的纪律是"演化过程不
落盘":那次运行是预览,不是交付物)。1.x 时代这是"拷一份 world.db",而 2.0 的世界是
一个键前缀 —— 没有这道出口,创作台要么把垃圾世界永久留在 Redis 上,要么自己去删键。

**默认只数不删**:`--yes` 才真删。一个打错的 `--world-id` 在这里的代价是抹掉另一个
世界,而那不可逆。给了 `--mysql` 的话连那四张表一起 drop —— 一个世界没了,它的表也就
没有主人了。测试盯着最容易犯的那个形状:抹 `alpha` 不许把 `alphabet` 一起抹了。

### Changed —— 「你去哈尔滨」:玩家对着正在聊的那个人下的指令,现在真的算数

`narrative_direction` 这一层原来有三个洞,合起来的效果是玩家嘴里**最常见**的那一类
指令一句都不生效,而且没有一条回执说得出为什么。三条一起补:

- **指挥对话方本人不再被挡掉,而且不吞掉她的话。** 从前 `Director.direct` 认出 target
  就是说话人,回一句"(这句是在指挥你正在说话的人本人 —— 直接跟她说就好。)"并
  `handled=True` —— 玩家那句话连生成都没走。可玩家的用法**就是**对着正在聊的那个人说
  「你去哈尔滨」。现在它照常兑现,而且**不接管她的回话**:指令进世界,一句「刚刚真发生了
  什么」作为 `extra` 块进这一轮的提示词,她自己开口 —— 于是"一边答应一边真的走"是同一
  件事的两面,而不是提示词里的一句想象。那句 grounding **只陈述事实,不下命令**:服从
  与否归人设,引擎不在判定逻辑外面开第二道后门。指挥别人那条路一个字没改。
- **地点是玩家给的参数了。** 新增 `action:"go"` + `place`,按**名字**模糊匹配
  (「哈尔滨」→「哈尔滨·冰雪大世界」);对不上就拒绝并列出有的是哪些地方,**对得上好几个
  也拒绝**——随便挑一个的话她真的会走过去,而一行日志都不报错。`leave` 从前把目的地取成
  `point_ids()[0]`(排序第一个),玩家永远说不了去哪。分类器提示词同时拿到了 `{places}`
  与 `{speaker}`。
- **`act` 接上了真的消费方。** 它从前只发一条 `agent_action{action:"directed"}` ——
  全仓库**一个写入点、零个读取点** —— 却回"(X 照做了。)"。**什么都没做却报成功**比不
  支持更坏,因为玩家永远不会知道。现在它走 `memory_seed`(和 `broadcast` 同一条路)落进
  目标的记忆,于是真的进得了他的提示词与 planner 排的一天;而回执也只敢说到"他知道了"
  为止 —— 做不做是他的事。

**真模型第一次实测就撞出第四个洞**:「你去哈尔滨」被规规矩矩地判成
`narrative_direction`、置信度 1.0,而 `target` 是字符串 `"你"` —— 于是"(我不认识你。)",
世界一动不动。提示词那一头当然要教,但**分类器是一次 LLM 往返,它有权抽风**;所以
`Director._resolve` 自己把第二人称(以及空 target)归给说话人。

冲突策略维持实测到的那条并写进 REFERENCE:**玩家改得动「她此刻在哪」,改不动
「她的日程」** —— 导演的移动走 `emit_action` 那条真路(在途、花时间、可被需求紧急带
打断),但不写计划表,到点了作息表照样把她收回去。

配套一个测试角色 `docs/examples/零.beats.json`:言听计从的「零」,**服从整个住在
`personality` 一个字段里** —— 没有 `obedience` 字段,判定逻辑里没有一处按她的 id 分支
(`test_directed_by_player.py` 钉着这条)。

### Fixed —— 一条聊天记录把角色的一生挤出了召回列表

`chat_session.close_conversation` 给 `conversation` 事件盖的是 `int(time.time())` ——
**全引擎唯一一处给活事件盖墙钟的地方**,别处都走 `Scheduler._record_event` 的
`event.setdefault("ts", self.clock)`。`TriggerEngine` 把它照抄进记忆的 `tick`,而
`MemoryStore.query` 按 `(tick, id)` DESC 排序。

于是一个线上世界:382 条记忆里 `user_conversation` 只占 20 条(5%),而**每个角色
召回列表的前 20 条 100% 是它们**。planner 的上下文、反思的源、八卦的源、叙事,吃的
都是这个列表 —— 角色的脑子里从此只剩跟玩家的对话。同一个根因还顺手关掉了这几条的
遗忘:`age = now_tick - tick` 是个大负数,被 `max(0, …)` 夹成 0,于是 recency 恒为
满分、`decayed_strength` 的 idle_days 恒为 0(实测它们 strength 涨到 2.8,而同期真正
的世界记忆衰减到 1e-160)。**没有一处报错。**

这个双时基本来是**知道**的 —— `world_time.WALL_CLOCK_FLOOR` 就是为它设的闸,时钟恢复
和运行摘要各在自己门口补了一道。记忆这一路没人补,而**补闸的代价就是你得记得每一个
消费方**。所以这次改的是源头:事件不再自己盖 `ts`。`payload` 里的
`started_at`/`closed_at` 照旧是墙钟 —— 它们抄自转录行,那一层本来就按秒记账。

配套两样:`TriggerEngine._tick_of` 把**老日志**里的墙钟 ts 折回"上一条正常事件的
tick"(不然 `rebuild` 会把脏数据原样再造一遍),折了会 warning;以及迁移
`World.repair_memory_ticks()` / `anima-world memory repair-ticks`,幂等,**只动记忆
不动事件日志**(日志记的是发生过什么,记忆是能重折出来的派生数据),查不到出处的行
一律不动 —— 编一个 tick 出来比留着更坏,因为它从此看不出来了。

### Fixed —— 一对反目的人还挂着 friendship 边,永远

`_on_relation_shift` 只加边不撤边。一对从「亲近」跌进「交恶」的人身上于是同时挂着
`friendship` 与 `rivalry`,而 `cliques.compute_cliques` 只看 friendship —— 那个小团体
里坐着两个此刻互相看不顺眼的人,算得出来、画得出来、一条日志都不报错。

`RedisKnowledgeGraph.drop()` 补上,反转时先撤掉相反的那一条。**只撤相反的,不撤中间
那一档**:淡下来(落进中性带)不等于反目,把它也算作撤销会让边随数字的小幅摆动来回闪。

`(subject, predicate, object)` 重复写**不覆盖**那一条**不动** —— 那是设计:边记的是
"这两人是朋友"这个*事实*,不是"他们又亲近了一次"这个*事件*,在「亲近」「挚交」之间
来回跨档时事实没变,重写 `source_event_seq` 只会让"这条边是哪件事立起来的"每次都换个
答案。顺带修掉边的 `created_at` 恒为 0(没传,吃了默认值)——每条边都自称生于创世。

### Fixed —— 版本号有两个来源,而其中一个会过期

`_engine_version()` 先问已安装包的元数据、问不到才回落模块。那是**第二个真相**:
pyproject 本来就是动态读 `__version__` 的,打成 wheel 时两者恒等 —— 唯一能分叉的
场合是 editable 安装下改了版本号还没重装,而那时元数据给的是**过期的那个**。

症状很难看:世界文件的 `engine_min` 盖的是新版本(模块读的),而"我跑不跑得了"问的
是旧版本(元数据读的),于是**引擎判定自己刚导出的包自己跑不了**。发现于 2.0.0 定版
的那一刻。

### Fixed —— 投影就地改写了一条已经发生过的事件

`_apply_agent_join` 把事件 payload 里那个 `spec` 直接拿去当投影的状态(同一个 dict,
没拷贝)。于是后来的一条 `persona_update` 顺着 `agent.spec.update(...)` **改写了那条创世
`agent_join` 在内存里的样子**:`World.events()`(内存窗口)看到的 payload 里多出了
`goals`,而 `events export` 与任何一次重放都没有。

**没造成数据损失** —— `goals` 自己有一条 `state_change/persona_update` 事件,日志是完整的,
重启和 `.cyberworld` 往返都不丢。坏的是"那条事件说了什么"有**两个答案**,而其中一个是
被事后改过的。这是"调试视图不许撒谎"那一条的另一副面孔,也是个地雷:投影往后加的任何
一处写,都会静默地改写历史的显示。

发现于 v3 那轮的往返验证 —— 一个只在**对着两份来源比对**时才看得见的差异。

### Changed —— 世界只有一种序列化形式了:`.cyberworld` v3(gzip JSONL),种子这个概念没有了

**破坏性,主版本级。** v3 之前世界有**三种表示**:

    world_seed.json   人写的世界描述(创世的输入)
    Redis 键          活着的世界
    world_state.json  导出时的 dump(藏在一个 zip 里)

第一种和第三种是同一件东西的两种写法 —— 都在回答"这个世界是什么样",只是一个给人写、
一个给机器读。留着两种,就要维护两套 schema、两套校验、**两个跨仓库镜像**,而且它们
描述同一个世界时长得完全不一样(种子写 `duties`,dump 里是编译好的 `bt_nodes`)。

v3 合成一个:**一个文件,两层记录。**

```jsonc
{"kind":"manifest","version":3,"world_id":…,"engine_min":…}       // 第一行,必须
{"kind":"author","type":"agent","body":{…}}      // 作者层:装载时**编译**
{"kind":"redis","key":"clock","type":"string","value":"8640"}      // 状态层:直接落键
{"kind":"event","seq":1,"ts":0,"type":"payment","payload":{…}}     // 事件,一行一条
{"kind":"mysql","table":"memories","row":{…}}
{"kind":"checksum","sha256":…}                   // 最后一行,可选
```

于是**创世和还原是同一个动作** —— 往一个前缀里装一个世界文件。手写的世界只有 `author`
记录,跑过的世界导出来只有状态记录;而把一份只含 `author` 的文件装进一个**已有**的世界,
就是一次编辑 —— 创作台要的"增删改查创世态"由此免费得到,不必在 API 上再开一排写口。

**去掉的**:`--seed` / `seed_path=`(换成 `--world-file` / `world_file=`)、
`world_seed.json`(内置演示世界变成 `demo.cyberworld`,仍是唯一的 package data)、
**出生证明**(`:meta.world_seed` —— 它是同一份内容的第二份拷贝)、zip 容器与它的成员
白名单/压缩比检测/`assets` 白名单(从来没有代码产出或消费过)、`export_world_package` /
`import_world_package` / `inspect_world_package` / `dump_world_state`(换成
`import_world_file` / `inspect_world_file` / `dump_world_records` / `install_world_records`)。
`world_package.py` 从 1200 行降到 700。

**几条硬纪律,每条对着一种真的坏法:**

- **载荷收在一个字段里**(`body` / `value` / `payload` / `row`),不平铺展开。这条是被
  一次真碰撞逼出来的:`locations` 条目自己带 `kind`(嵌套地图的几何类型 `region`/`point`),
  平铺进信封就把记录类型**静默覆盖**掉了 —— 文件写得出来,读的时候变成另一种记录。
  收进 `body` 让它不可表达,比约定"别用这几个名字"可靠。
- **不认识的记录类型、不认识的 section,都当场报错,不跳过。** 跳过等于安静地少装
  一半世界,而文件看上去完全正常。这条也是被逼出来的:转换器最初漏了三个 section
  (`stock_places` / `world_setting` / `mock_narration`),而丢掉的后果是世界照样建得起来、
  只是少一整层(`mock_narration` 丢了 = 世界改说另一种语言),没有任何地方会说一句。
- **`body` 的类型也归 `type` 管,对不上当场报错。** 同一条纪律欠了最后一段:
  `world_setting` 被编进"一份对象"(`body` 要是 dict),而播种它的
  `__main__._seed_world_setting` 只认字符串 —— 于是**任何 `.cyberworld` 的世界观都送不进
  世界,而且一声不吭**,每个自定义世界都跑在引擎写死的那份默认世界观下。定案是
  **一段文本**(`body` 就是那段话,不包 `{"text": …}`):它到 `:prompts` 的
  `world.setting`、再到她收到的提示词块,全程是同一个字符串。落点是
  `world_file.AUTHOR_SCALAR_TYPES`(第三张表,读写两个方向都验),`seed_to_author_records`
  那一头也从"静默跳过类型不符的段"改成报错。这个洞能存在一整版,是因为
  `demo.cyberworld` 里没有这条记录、也没有一条测试碰过它 ——
  `tests/test_world_setting.py` 现在守着整条通道(文件 → `:prompts` → 提示词块)。
- **压缩与否只看头两个字节,不看扩展名。** 写出去永远是 gzip(且 `mtime=0` —— 默认会
  把当前时间写进头里,于是"这两个包一样吗"永远答不了,而可 diff 是换文本格式的卖点);
  读进来允许裸 JSONL —— 手写一个世界不该被逼着先 gzip,而这正是这个格式能取代种子的
  前提。内置的 `demo.cyberworld` 因此以**纯文本**进仓库:可 diff、可 review,
  而且它同时是这个格式的说明书。
- **导出与导入都是流式的。** v2 的 `_dump_mysql_section` 是全量 `replay()` 再 `SELECT *`,
  **没有任何上限** —— 一个跑了两年的世界导一次包要把整段历史塞进内存。那是个只会在
  生产上撞到才发现的洞(上一轮已经把它标记为已知缺口)。
- **无限增长的那四样一律按语义记录导出,不看它们此刻住在哪个后端。** 这条是装上产物
  演一遍用户故事时逮出来的,而且逮的是**这份 CHANGELOG 自己宣传的那句话**:按后端
  分叉的话,没接 MySQL 的世界会把整段历史塞进**一个** `redis` list 记录 —— 一整行
  几万字节的转义 JSON,于是 `grep '"type":"entity_spawn"'` 什么也找不到(它在字符串里
  是 `\"type\"`),`diff` 也退化成整块变。**在最常见的那种世界上不成立的卖点,就是
  空话。** 第二个理由同样硬:一份包能不能被 grep,不该取决于导出它的那台机器接没接
  MySQL —— 那和世界无关,而两种形状意味着消费方要写两套读法。
- **上限从"三个数乘起来"简化成"一条流三个数"**:解压 ≤512MB、≤500 万条记录、单行 ≤32MB。
  zip 的成员白名单、压缩比、符号链接/路径穿越/加密成员那一整套整个不需要了。
- **线格式与落库分家**:`world_file.py` 不认识 Redis,`world_package.py` 不认识 gzip ——
  两边各自能被单独测,而"换一种容器"不必碰存储语义。

**编译器还在。** `duties` → 行为树、`money` → `payment` 事件、被引用没定义的物品自动
补一条 —— 这些是真实存在的一层,不会因为换了容器就消失。变的只是它不再叫"播种":
`author` 记录聚合回 section 字典,原样喂给既有的那十八个编译步骤。**换掉容器,不动语义。**

附带的好处不是修辞:

```bash
zcat world.cyberworld | grep '"type":"entity_spawn"'      # 这个世界里生出过什么
diff <(zcat a.cyberworld) <(zcat b.cyberworld)            # 两次导出差在哪
```

⚠️ **跨仓库**:运维台的 `lib/worldSeed.js` 可删,`lib/worldPackage.js` 要整个重写 ——
而且它**本来就欠 v2**(注释里还写着 `world.db?`,从没吸收上一轮),所以是从 v1 直接
跳 v3。这也说明双向互验那几条没有真的在验格式(它们一直绿着,而镜像已落后一个大版本)。
经过见 `platform/docs/引擎-2.0-同步.md`。

### Added —— 世界自己长得出新东西,也抹得掉(`spawn` / `destroys_target`)

在这之前 `entities` 是**创世时钉死的闭集**:树不能被种下,杯子不能被打碎,而 `make`
这个动词在词表里却没有任何办法造出一个实体。一个不能长出新东西的世界是个西洋镜 ——
而"按规则铺开实体"正是"生成一个世界"这件事本身。

```jsonc
"育苗": {"duration": 24, "when": ["树高 >= 3"],
        "requires": ["me_体力 >= 20", "have_garden_shears >= 1"],
        "consumes": {"fertilizer": 1}, "costs": {"体力": "me_体力 - 20"},
        "spawn": {"kind": "sapling", "name": "新育的树苗"}},
"拔掉": {"costs": {"体力": "me_体力 - 5"}, "destroys_target": true}
```

**生成必须要代价,而代价由作者写 —— 引擎不发配额。** 声明了 `spawn` 或
`destroys_target` 却没写 `costs` / `consumes` / `duration` 里任何一样,**开不了机**。
为什么不发配额:配额是**引擎的天花板**,撞上去时她收到的拒绝在世界里没有意义 ——
"这个世界最多一百棵树"不是她能理解、能应对的东西,她也永远学不会。代价是**世界的
理由**:她知道自己为什么做不到,也知道要做到得先补什么。

⚠️ 但**代价只封得住速率,封不住存量**:体力天天回满的世界里,一百天就是一百个孩子。
真实世界靠的是生灭成对,所以这两格是同一轮加的 —— 只有生的引擎会让每个世界最后都
挤爆,而且漏得很慢、很安静。

几条落点:**生在收尾那一刻**(否则"十月怀胎"只是一句话,那十个月一天也不用过);
`spawn.quantities` **只收常数**(新生的东西身上还没有值可读,而读母体的量要先回答
"读的是起头还是收尾");没写的量照声明**逐个量填**(创世那边踩过:按实体跳会让其余量
一个都不落地);id 由引擎发(`kind:序号`,计数器住 Redis)且**只增不减** —— 它进事件、
进提示词、进 `.cyberworld`,复用一个死者的号等于让历史指向另一样东西。
`destroys_target` **不许和 `set` 一起写**(写到一个正要被抹掉的东西身上,引擎挑哪条都是
猜),抹掉时**四样一起走**:实例、量、位置、还挂在它身上的长过程。

**出生自检是出生的一部分,不是事后的工具。** 运行期生出来的东西走的不是创世那条路,
而创世那条路上的闸(一次列全、当场开不了机)在这里一条都不在。不验的话,一个新生的
东西可以是:`entities` 里看着好好的,量却一个都没落地 —— 于是能力条件对着 0 求值、
规律算不动,而两件事都只是**安静地不发生**。查四样:种类还认得它、声明过的量一个不缺、
每条能力都给得出一个**叫得出名字的结论**(空跑一遍;`apply_affordance` 本来就只算不写,
所以几乎免费)、它按声明的可见性真的在场。判据是"算得出结论"**不是"能成功"** ——
`conditions` / `incapable` 都算过关,把它们算成病的话自检会对着健康的世界一直报警,
然后没人再看它。没过就**整个撤回**并发 `entity_stillborn`,而**代价不退**:她确实付过了,
这一次失败是作者的声明坏了,退给她只会让那个 bug 从账面上也消失。

新出口:`World.check_entity()`、`anima-world ontology --check`(**有问题时退出码 1**,
所以它能进 CI,和 `--ticks 0` 当校验器同一个用法)。

**跨进程用版本号,不是行数**(`:entities_rev`)—— 一生一灭净变化是 0,而那正是最常见
的一对。变了只重编译**实例**那一半:**种类仍然是冻的**,运行期新增种类等于让"这条规律
合不合法"随时间变化,重放就不再确定。

橱窗里演了整条链:夏 育苗(两小时 + 一包肥 + 二十点体力)长出一株创世时不存在的树苗,
它有量、在场、能被交互、经得起自检,而且能被拔掉。

### Fixed —— 一个没写 `rules` 的世界,重开一次就再也打不开

播种一直按"这张表恰好还空着"判,而它**只在第一次开机时和创世重合**。之后每一次开机,
手里那份种子(缺省是包自带的橱窗)都会去填当初作者**有意留空**的那几张表 —— 而规律是
这个世界的物理法则:一个作者写了 `kinds` 却没写 `rules` 的世界,重开一次就被塞进橱窗那条
"树会长高"的规律,而它引用的 `tree` 这个种类在这个世界里根本不存在。

下场不是算错,是 `resolve` 当场拒绝整个本体 —— **这个世界从此打不开**。创作台的整套
流程都是自定义种子,所以这条一撞一个准。`_seed_ontology` 早就用 `fresh_world=` 判了,
这一轮把 `rules` / `stock_visibility` / `stock_places` 三个补齐:**播种是创世那一刻的事,
不是"这张表恰好还空着"的事。**

### Fixed —— 新世界的第一次 `World.act()` 会把每个人的钱和随身物品翻倍

`Scheduler.__init__` 建投影时日志还空着,水位(`_projection_seq`)于是停在 0;创世事件
写完之后 `__main__` 重折一次投影 —— 投影里有了那 20 多条,**水位还是 0**。于是下一次
`catch_up_projection()`(`World.act()` 每次都调)把创世事件再折一遍。

坏得最难查的是它的形状:只翻一次(第二次 catch_up 就正常了)、只在**创建这个世界的那个
进程**里(重开一次读到的是对的)、日志本身一条不错。所以账面上永远看不出来 —— 事件重放
出来的数和内存里的数不一样,而没人会去比。

修法是把"重折"和"挪水位"焊进 `Scheduler.reset_projection()`:两个字段各写各的就是那个洞
本身。而它此前没被任何测试发现,是因为能力那一层的测试都直接调
`scheduler.perform_affordance`,没走 `World.act()` 那条真路。

### Added —— 地图和轨迹能看见了(`anima-world map`)

位移这件事此前只在事件日志里躺着 —— 一行 `state_change/location_join`,谁也不会去翻。
而**看不见的东西没人会去查**:"她走了"到底有没有在世界里兑现,正是 1.3.0 那批 issue
的病本身;1.3 开发期四个 bug 有三个在提示词那一层,同样因为它此前不可见。

```
anima-world map --db-path w.db              # 地图 + 全程轨迹 + 此刻谁在哪
anima-world map --db-path w.db --day 2      # 只看第 2 个世界日
anima-world map --db-path w.db --now        # 只画此刻
anima-world map --db-path w.db --watch      # 每 2 秒重画(不推时钟)
anima-world map --db-path w.db --json       # 给别的仓库渲染
```

**渲染是赠品,`--json` 才是契约。** 本包无 HTTP、无 HTML —— 终端那张图只是让你现在
就能看见;`World.map_data()` 那份数据才是给创作台 / 网站 / 运维台的出口,而 CLI 与
宿主**共用同一份**(观察窗另写一遍取数就会撒谎,和 `debug_prompt` 共用
`prompt_blocks` 是同一条理由)。

#### 四件差点画错的事,共同点是「画错了不会报错,只会好看地骗人」

- **几何是相对父级的**(nested-map D2):`w=0.55` 的 region 占的是父级宽度的 55%,
  不是画布的 55%。照原始 `x/y/w/h` 画出来的图看上去完全合理 —— 只是每个东西都在错的
  地方(实测内置种子 workshop 原始 `x=0.78`,绝对 `0.482`)。新增
  `LocationStore.absolute_box()` 补上 region 的绝对矩形,`map_data` 交出来的一律是
  绝对坐标。
- **中文是双宽字符。** 这个引擎的世界是中文的:地点叫「咖啡店」,角色叫「夏」。
  按字符个数排版,一个中文标签就把整条边框往右推两格 —— 而框线是这张图唯一的结构。
  画布改成按**显示宽度**记账,轨迹记号也换成单宽字符(第一版用中文首字画线)。
- **两个人走同一条路**,后画的把先画的整条盖掉,那个人就从图上消失了。重合处标 `#`。
- **在路上的人不站在任何地方。** 漏了 `travelling` 这一层,她会在图上凭空消失半段路。

- **窗口之前她在哪,得带进来。** 只取窗口内的点,起点在窗口之前的人就只剩一个孤点,
  画不出线 —— 看上去像「她这天没动」,而 `--day N` 恰恰是最常用的看法(实测第 2 天,
  三个人里两个是这样)。锚点带 `before: true`,图例说「自 X」,不假装那是这天的一次位移。

另外两条小的:`--now` 不再说「没动过」(只看此刻时,**没去看不等于没动**);
轨迹只认**到达**(`location_join`),起程不算 —— 她可能走到一半被打断,而画一条没
走完的线等于说她到了。

**`--redis` 连一个活着的世界**(世界跑在另一个进程里)。没有它 `--watch` 是半废的:
能盯的只有一个不动的世界 —— 而"看它动起来"正是这个功能的意义。`world_id` 从这个 db
自己的戳里读,不用人抄。

给了 `--world-id` 而它和戳对不上就**当场拒绝**。抄错的样子比报错坏得多,实测:
`World.open` 会拿这个 db 当创世输入,在 Redis 上**建出一个全新的世界** —— 于是你看到
一张排版正常、三个人各就各位、时钟 0 的地图,读起来像"这个世界还没开始跑",而不是
"你看错世界了"。顺带还在 Redis 里留下一份垃圾。(我给这条写的第一版测试断言"抄错会
看到一张空地图" —— 跑出来才发现真相是这个。)

不给 `--redis` 时,搬走了的世界这道命令照旧**当场拒绝**,不给一张空地图(和 `doctor` /
`events export` 同一条纪律)。`tests/test_mapview.py` 25 条,每条都验过注入 bug 会红。

已按纪律在 `docs/FOR-STUDIO.md` 记了一笔(§3.6)——**加了 CLI 出口就去那份文档里记**,
上次没记回执让创作台的 P1 白等了几天。

## [1.4.0] — 2026-08-03

**世界可以不再住在一个进程里。** 这一版把运行时状态整个搬进 Redis(十一步)、
把随时间无限增长的那几样搬去 MySQL,并给外面的进程开了改变世界的入口
(`World.act()` / `World.intend()` / `body` 面的动词)。

**db 格式没变(仍是 1),schema 修订没变(仍是 3)。** 一张 SQLite 表都没动,
不给 `redis=` / `mysql=` 的世界行为逐字不变 —— 1.3.0 的世界文件用 1.4.0 打得开,
反过来也一样。整版是加法。

分家的判据最终是**"她带不带得进上下文"**:进得了提示词的必须有界(Redis),
进不了的可以无限(MySQL,要用时按 k 取回来)。它可验 —— 实测 60 世界日,后端涨
61 倍而提示词纹丝不动(`tests/test_bounded.py`)。

### Changed —— 世界文件里只存作者动过的(DB-SPLIT 移动 1)

创世**不再播**引擎默认值。读的时候按 **环境变量 → 机器配置 → 世界文件 → 引擎默认值**
解析,`config list` 与 `prompt_list` 每行带 `source` 告诉你走到了哪层。

播下去的那 36 行看着无害,坏处要过一个版本才显形:

```
① 新世界:            chat.recall_k = 3
② 引擎下一版改成 99(因为 3 被证明太少)
③ 老世界用新引擎打开:  3   ← 还是旧的,永远
④ 新建的世界:         99
```

而且**无声** —— 两个世界行为不同,`config list` 看上去一模一样,没人会想到去比。
`config` 表也没有任何一列记着"这是谁写的",于是引擎分辨不了作者的决定和创世那天的快照。

实测之后:毛坯世界 `config` **0 行**、`prompt_templates` **0 行**;内置种子(橱窗)的
世界 `config` **8 行** —— 正好是作者点亮的那八个开关。提示词模板那 31 行里作者动过的
本来就是 **0** 行,全是引擎快照。

**加法兼容**:老引擎打开一个 `config` 空表的世界,会跑它自己那套 `seed_defaults`
把默认值补回去再照常运行。所以跟次版本走,db 格式与 schema 修订都不动。

**已有的世界不受影响,但也不受益。** 拿真的 1.3.0 和 1.4.0 双向验过:1.4.0 打开
1.3.0 的世界照跑(那 36 行照旧生效,`source` 显示"世界文件");1.3.0 打开 1.4.0 的
世界也照跑(老引擎把默认值补回去,作者点亮的一个不丢)。但老世界里那 36 行是**真的
行**,而 `config` 表没有一列记着"这是谁写的" —— 引擎分辨不了"作者设的 3"和"创世那天
播的 3",所以老世界的默认值仍然冻着。不自动清理:猜错一次就是悄悄改掉一个跑了半年的
世界的行为,而那正是这次要防的事。

取舍是真实的,不是纯赚:此前"表里有一行"意味着值锁死了,对**可复现性**有好处 ——
同一个世界文件在两个引擎版本上行为一致。需要可复现的场合,把值显式写进种子的
`config` 块,那本来就是作者的意见。

顺带:`LONGCAT_API_KEY` 的配套默认值(端点 + 模型名)从**创世播种**挪到了机器配置那一层。
此前只有新建的世界吃得到 —— 一个已经存在的世界加上 `LONGCAT_API_KEY`,`base_url`
仍是空,照跑但一次都打不通。哪家的 key 本来就是机器的事。

#### 拆的路上撞出两个洞,都是"表里有没有行"被当成了"这个键存不存在"

**`set()` 没有回落 —— 密钥明文写进世界文件,一声不吭。** 创世播默认值时,每个声明过
的键都先有了一行带元数据(`is_secret=True`),后来的 `set()` 从那一行继承。不播之后表
是空的,`set("llm.api_key", …)` 拿不到任何元数据,`is_secret` 缺省成 False。而
`.cyberworld` 是分发物 —— 这正是 `machine_config` 那一整轮要防的事,而移动 1 把它的
地基抽掉了。判据统一成"引擎声明过什么":`has` / `meta` / `list` / `set` 四个都回落。
没有加密钥匙时**当场报错**,而不是退回明文。

**搬进 Redis 时整份搬 —— 刚拆掉的快照换个后端原样重建。** `PromptStore.list()` 现在
是合并视图(引擎声明的 31 条 + 世界里多出来的),而搬家照着 `list()` 搬,于是 31 条
默认模板全落进 Redis,改进过的措辞照样到不了这个世界。搬的是作者的意见,不是引擎的
默认值。

`tests/test_config_provenance.py` 是这几条的闸(10 条,每条都验过注入 bug 会红)。

### Added —— 随时间无限增长的那几样搬出内存(MySQL)

`World.open(mysql=...)`:`events` / `memories` / `conversations` / `messages`
落到 MySQL,别的照旧在 Redis。可以只给 `mysql` 不给 `redis`。

**分界线最终是"她带不带得进上下文"。** 这条判据走了三版:

1. "这是不是世界" —— 拆不动转录(转录当然属于这个世界,只是不该在内存里)
2. "随时间涨,还是随世界的规模涨" —— 拆得动了,而且量得出来
3. **"进不进得了提示词"** —— Redis 装她带得进上下文的东西,而 LLM 的上下文本来
   就有上限,两个"有上限"是同一个上限

第三版最好用,因为它**可验**:分对了的话,提示词就不该随世界变老而增长。实测同一个
角色跑到第 60 个世界日,后端涨了 61 倍而提示词纹丝不动:

```
 世界日   提示词    MySQL 记忆   MySQL 事件
   0天    2251字        10          50
  60天    2272字       612        3264
```

`tests/test_bounded.py` 把这条钉住了(变异验证:把记忆检索的 k 拿掉,提示词
2275→6546 字,当场红)。

**顺着它改正了一张分错的表:`edges` 不进 MySQL。** 关系边有
`UNIQUE(subject, predicate, object)`,谓词是闭集(`friendship` / `rivalry`,
scheduler.py 里写死的两个),主宾都是 `agent:<id>` —— 上界 2×N²,**按世界的规模
封顶,不按时间涨**。它一度被分进 MySQL,是照着"像不像历史"分的而不是照着判据。
那个闭集是**承重的**:哪天让 LLM 自己造谓词,边就不再有界,这笔账要重算 ——
`test_bounded.py` 盯着这个前提,而不是等它出事。

原始的量:世界搬进 Redis 之后,一个三人世界:

```
 世界日     Redis 内存    增量      占大头
   1天        997 KB     +30 KB   events=18KB
  20天       1228 KB    +129 KB   events=158KB memories=77KB
```

只有 `events` 与 `memories` 随时间线性增长 —— 20 天里占了内存增量的九成。每世界日
13 KB,一千个世界跑一年 **4.6 GB 常驻**,而且永远不回落。别的东西随**世界的规模**
有界(黑板每人 20 个键,地图创世后不动)。内存装得下一个热但有界的东西,装不下一个
冷但无限的东西。

分家后同一份负载:

```
 世界日   Redis 增量   MySQL 事件
   1天      +52 KB         105
  20天      +70 KB        1085
  40天      +70 KB        2067      ← Redis 不再涨
```

三十个聊天回合(60 条消息 2271 字):MySQL 收下全部,Redis +0 KB,SQLite +0 KB。

**这是加法,不是格式变更。** SQLite 那边一列没动,不给 `mysql=` 的世界行为逐字不变,
老引擎打开这个文件照样跑。`docs/DB-SPLIT.md` 原本把"转录搬出世界"排进 2.0.0(计划是
删两张表);判据从"这是不是世界"换成"它增不增长"之后,答案变了 —— 不必删表,只需给它
一个不在内存里的去处。

四条纪律:

- **跨实现互验**:同一份输入喂给 SQLite / Redis / MySQL 三个后端,**逐个问题比答案**。
  这套在 Redis 那一轮抓出十来个只有对比才看得见的错。
- **`mysql=` 收工厂**:`mysql=lambda: pymysql.connect(...)`,引擎自动包成每线程一条。
  给裸连接照旧能开,但**开机时当场点名** —— 因为它不是必现:大多数 tick 相安无事,
  某次在负载下炸成 `InterfaceError (0, '')` 或 `read of closed file`,一个离原因很远、
  看不出是并发的报错。这种"大多数时候没事"最该在开机时说破,不指望人记得。
- **`ThreadLocalConnection`**:`pymysql` 的 threadsafety 是 1 —— 模块能多线程用,
  **一条连接不行**,而引擎有线程池(叙事、规划)且它们都会记事件。共享一条连接的后果
  不是慢,是协议帧交叉、连接当场作废。实测撞到过:世界跑到第 12 天崩在一条 INSERT 上。
- **`events.seq` 不再保证连续**:MySQL 自增在事务回滚后留空洞,而 Redis 的 `RPUSH`
  返回长度是连续的。`since_seq` 分页照旧正确,依赖连续性的代码会悄悄错 —— 目前没有。
- **崩溃的伤面写成了测试**:写序是 Redis 先、MySQL 后,中间死掉 = 在途写下了而历史里
  没有这趟。伤是"历史少一条"(重折出来的东西从此少算一次,不自愈),不是"她卡在路上"。

### Fixed —— Redis 里躺着一份冻在创世的旧拷贝

搬家分两步:先整个搬进 Redis,再把无限增长的那几样接到 MySQL。第二步只换掉 store
对象 —— **第一步写进 Redis 的那份留在原地,而且从此不再更新**。实测:MySQL 289 条
事件,Redis 那份停在 50 条(创世快照),再跑五天还是 50 条。

引擎自己不读它(store 已经是 MySQL 版),所以全量测试一片绿。但 Redis 的全部意义是
"另一个只有 Redis 连接的进程读得到这个世界" —— 那个进程读到的会是一个**从创世起
什么都没发生过**的世界。两份真相里有一份不会更新,是这个仓库最怕的那种坏。

两条一起修:MySQL 要接手的表不再往 Redis 搬(`_goes_to_mysql`),而且删掉上一次
"只用 Redis"时留下的那份(`_drop_stale_redis_copies` —— 一个世界可能先只用 Redis
跑过,后来才接上 MySQL)。测试验的是**末态**,两条机制拆掉任一条它都红。

### Fixed —— 第二个进程会把她按回原点

`World.open(redis=...)` 搬家时把黑板**整份写回** Redis。而第二个进程手里那份是从
SQLite 读出来的**创世快照** —— 于是它一开机就把她的位置倒带:第一个进程眼里她在 home,
第二个眼里她在 cafe,**两边都不报错,两边都还在跑**。

改成逐键 `HSETNX`(`RedisBlackboard.seed_missing`):只填 Redis 里还没有的键。时钟那边
早就做对了(`RedisClock` 用 `setnx`,注释写着"重开一个世界不该把时钟拨回去"),黑板是
同一个道理,只是当时没连起来。逐键而不是"整份看空不空",是为了新版本加的黑板键在老
世界里还能补上。

### Fixed —— Redis 世界开了 stance 开关会当场炸

`annotate_message` / `conversation_meta` 挂在 `ChatStateStore` 上,而它们碰的是
`messages` 表。`RedisChatStateStore` 一继承就带着一条指向 SQLite 连接的死路,
于是 `chat.stance.enabled` 打开时 `AttributeError`。

根因是 SQL 挂错了地方。**表在谁那儿,写它的 SQL 就在谁那儿** —— 两个方法移到
`ChatStore` / `MySQLChatStore`,`ChatStateStore` 只转发。聚合逻辑
(`summarize_annotations`)后端无关,所以只写一遍:每个后端各写一份的话,同一场对话
会算出不同的 meta,而两边都跑得动。

顺带钉住"换后端时攥着旧引用的人一个都不能漏"(`_rebind_chat_store` + 枚举持有者的
测试)—— 这个坏法之前发生过一次:只改 `world.chat_state`,而 `ChatService` 构造时已经
把它存进了自己的 `_state`,聊天照跑照写,写进了旧后端。

### Fixed —— Redis 活在内存里,而"世界还在不在"曾经取决于一个从没问过的配置

**Redis 主要活在内存里**,持久化(RDB 快照 / AOF)是配置选项,默认的 `redis.conf`
里 AOF 是关的。世界搬进 Redis 之后,"这个世界会不会在重启后消失"就取决于一个引擎
管不着、也从来没问过的东西。

实测(真 Redis,重启一次):

```
持久化关着:跑完一天 104 条事件  →  Redis 重启后 50 条
AOF 打开:  跑完一天 103 条事件  →  重启后 110 条
```

50 是创世那批 —— 重开时又从 SQLite 复制了一遍。所以**世界没有报错,它只是悄悄年轻
了一天,然后接着跑**。这正是这个仓库最怕的那一类。

现在开机就问 Redis 它是怎么配的(`durability_warning()`),三种情况分开说:
什么都不存 → "它一重启,这个世界就退回创世那一刻";只有 RDB → "崩溃时会丢掉最后
一次快照之后的世界";开了 AOF → 沉默。`World.durability_warning()` 也能读。

**探不到就沉默**:`CONFIG GET` 可能被禁用(托管 Redis 常见),也可能客户端不支持。
因为探测失败就报警,等于训练人忽略这条警告。

⚠️ 顺带交代一件我该早点说的事:这一路所有"真 Redis 验证"跑的都是
`--save "" --appendonly no` —— **持久化整个关着**。那些验证对协议行为仍然有效
(它们验的是语义,不是耐久),但**没有一次覆盖过重启**。这条检查就是那个盲区的产物。

### Changed —— LLM 的钥匙搬出世界,`world.db.key` 那条耦合链消失

`llm.api_key` 此前存在**世界文件**里。它是 secret,所以要加密,所以有了
`world.db.key`,所以有了"**Fernet 密钥必须随 db 搬迁**"这条不变量。**整条链的根,
就是把一把 API key 存进了世界文件。**

而它带来的还不只是麻烦:`.cyberworld` 是**分发物**。种子里禁止写密文键防的正是
"把作者的钥匙寄给每个人",而世界文件这条路一直开着。

- `machine_config`:`~/.anima-world/config.json`,**0600、明文、绝不入库** ——
  照兄弟仓库(创作台 `~/.anima-studio/settings.json`)已经验过的形状,不发明新的。
  明文而不加密是有意的:0600 的家目录文件和"加密文件 + 就放在旁边的密钥"防的是同
  一类人,而后者多一层假的安全感,还多一个必须一起搬的东西。
- 解析顺序:**环境变量 → 机器配置 → 世界配置(旧世界兼容)→ 默认值**。
  环境变量在最前是为了容器和 CI;而**人不手写环境变量** —— `anima-world config set`、
  `World.config_set`、`start` 的引导都自动路由,写的人不必知道哪个键去哪儿。
  写了但被环境变量盖住时**当场说破**(写了不生效是最难查的一种)。
- 堵掉一条真实的泄漏路径:`seed_defaults` 此前会**把环境变量里的 key 播进世界文件**。
- `doctor` 报**来源**("为什么我改了配置没生效"几乎总是这个问题),旧世界里还留着钥匙
  的会被点名并给出搬法。
- **`llm.api_key` 是唯一声明为密文的键**,所以搬走之后世界里一个 secret 都没有:
  新世界不再生成 `world.db.key`,`doctor` 也不再为"没有 keyfile"报警(那句话现在是
  错的)。旧世界照旧能读;**真丢了钥匙仍然报警** —— 判据是"这个世界有没有过 keyfile",
  缺省保守,漏报比误报坏。

⚠️ **这次踩的坑值得记**:机器配置刚落地时没有测试隔离,于是一个测试把 `sk-typed-in`
写进了开发机的 `~/.anima-world/`,**别的测试读到它、真的去连了 OpenAI**,十八条一起红
—— 而单看任何一条都看不出原因。全局单例路径 + 没有隔离 = 测试之间互相污染。
`conftest.py` 里现在有一个 `autouse` 的隔离 fixture(不是"记得加" —— 需要人记住的
隔离迟早会漏掉一处,而漏掉的那一处会往真实家目录里写东西),外加一条哨兵测试。

另外两处只有对比才看得见的错:`ConfigStore.get()` 现在会先问机器配置,所以它回答不了
"这个**世界文件**里有没有它" —— 加了 `world_value()` 专门问世界那一层,诊断与打包
测试都改用它。而"密文非空"不等于"值非空":创世会播一个加密过的空串,拿它当"钥匙落进
了世界文件"会判错。

### Added —— 搬完了:世界文件只剩 schema 和一个戳(第十一步)

**先修一个我自己犯的、正是这一整轮反复在抓的错**:`RedisEventLog` 写完、跨实现互验过、
在真 Redis 上跑过 —— 然后**忘了在 `World.open` 里接上**。于是"唯一真相"那张表继续全
写进 SQLite,而我上一轮还在说它搬完了。**做了、测了、但没兑现,而且一切照跑。**
现在有一条测试专门盯着"这个世界的事件到底落在哪儿",而不只是测那个类。

接它的时候撞出一个结构问题:**`event_log.conn` 被当成"整个世界的数据库连接"在用,
22 处**。事件日志一换后端,那 22 处全崩,而崩的地方全是别的表(需求、经济、关系图)
—— 它们和事件日志本来毫无关系。连接归了 `Scheduler.conn`。

四张小表此前是**模块级函数直接吃 `conn`**(`needs.load(conn, …)` 等),没有可替换的
东西。`small_stores.py` 把它们收成 store 类,**SQLite 版一行逻辑都不重写**:价格漂移
曲线、需求结算、小团体的并查集是世界的规则不是存储,抄一遍就等于给规则开第二份实现。

最后六张表(edges / agent_needs / cliques / reflection_state / item_defs / shop_stock)
的 Redis 版,照旧跨实现互验。**互验抓到经济那条**:`daily_price_pass` 不只漂价,
**还补货**(`RESTOCK_PER_DAY`),而且价格 `round(…, 2)`、`restocked` 传的是补货量 ——
我三处全按直觉写错了,而那个函数名本身也在误导(它其实是"日切")。

**最后把 SQLite 里的原始行清掉。** 搬家一直是复制不是移动,不清的话那个 `.db` 既不是
完整的世界也不是干净的空壳,而是**一份过时的副本** —— 而我们刚在它上面盖了"这里没
数据"的戳。那个组合最危险:戳没撒谎,但文件里躺着一份看起来很像真世界的旧数据。

跑完一个世界日之后,世界文件里只剩:

```
config    36 行   ← 有意不搬(按 DB-SPLIT.md 它该搬**出**世界)
db_meta    6 行   ← 格式版本 / schema 修订 / 存储后端的戳
```

Redis 里 22 个键。装到干净 venv、起真 Redis、两个真进程验过:进程 B 只有一个
`redis://` URL,读得到第几 tick、她在哪、精力多少、事件多少条、树多高、记得几件事、
在不在路上。

### Fixed —— 世界搬去 Redis 之后,离线命令不许再撒谎(第十步)

这是"世界不再是一个文件"的**真实账单**,而且它一直挂在那儿:`doctor` /
`events export` / `report` / `.cyberworld` 打包都是**离线看文件**的 —— 直接开一个
`world.db`,不经过 `World`。世界跑在 Redis 上时那个文件里只有 schema 没有数据,
于是四条路会:

- `doctor` 报"0 条事件,0 个角色",一切正常
- `events export` 导出一个**空的** JSONL
- `report` 出一份"这三天什么也没发生"的摘要
- **打包产出一个能装能开、里面什么都没有的 `.cyberworld`,然后它被发给别人**

**四条全都不报错。** 最后一条最严重:一个空壳包比一次失败的打包坏得多 —— 收到的人
要跑起来才发现世界是空的。

修法和这个引擎既有的可挂载性联锁同形:世界一搬去别的后端,就在 `db_meta` 上盖一个戳
(`storage_backend` / `storage_world_id`,和 `format_version`、`schema_revision`
住在一起 —— 它们回答的是同一类问题:这个文件是什么、能不能照字面读)。离线路读到它
就**当场停下**,并说清去哪儿看:

```
[report] 这个世界的数据在 redis 里(world_id='onredis'),不在这个文件里 ——
         这个 .db 只是个空壳。离线命令读它只会得到一个空世界,所以这里当场停下,
         而不是给你一个错的答案。
         要看这个世界,连上那个 redis 起一个进程:World.open(db_path, redis=…, …)
```

⚠️ **这一步只解决"不撒谎",没解决"能用"。** 让这些命令真的支持 Redis(连上去看、
从 Redis 打包)是下一件事;而 `.cyberworld` 到底该怎么装一个住在 Redis 里的世界,
需要先回答"分发单位是什么" —— 那是设计问题,不是实现问题。

### Added —— 她和某个人之间的状态进 Redis(第九步)

意图 / 静音 / 拒谈话题 / 回头找你 / 玩家教的规则 —— 五张表五个 hash。这五样是**真
世界状态**(她真的在不理你、真的拒绝谈那件事),不是缓存。

**时间语义原样保住,两条都不能想当然**:

- **静音用墙钟。** 玩家那侧的"五分钟"是真的五分钟,而世界时钟可能是演示速度
  (1 tick/秒)—— 用 tick 会让"五分钟别理我"变成不到一分钟。
- **回头找你用 tick。** 那是世界内部的约定(到第几 tick 回来敲门),和墙钟无关。
- 过期的**读到就清**:读到的必须是此刻还成立的。

`annotate_message` / `conversation_meta` **有意不覆盖** —— 那两个碰的是 `messages`
表,属于 `ChatStore` 的地界;而按 `DB-SPLIT.md` 的判断,转录本来就不该在世界里。
硬塞进来只会让这一层多背一个不属于它的表。

变异验证里有一条**第一次没红**:"取最晚那条静音"改成"取最早",测试照绿 —— 因为我
造的数据里每对 (角色, 玩家) 只有一条,分不出早晚。补了一个同时有 `mute` 和 `delay`
的场景才验到。取最早的后果很具体:**她会在还该沉默的时候开口**。

补这条时又踩了一次:新数据插在旧断言前面,把后面那些计数断言的基数改了 ——
挪到末尾才对。**造数据的顺序也是测试的一部分。**

### Added —— 地图与行为树进 Redis(第八步):算出来的东西继承,不重写

`LocationStore` 的 `tree()` / `absolute_xy()` / `distance()` 和 `BTStore` 的
`build_tree()` / `action_table()` / `duty_windows()` **都只依赖别的方法** —— 它们是
从行算出来的,不是存出来的。所以两个 Redis 版**继承**原类,只覆盖真正碰库的原语
(`all` / `get` / `upsert`;`actions` / `set_action` / `add_node` / `_tree_rows`)。

这不是省事:**地图的几何和树的组装不许有第二份**。父子链、相对坐标折算、距离公式
再写一遍,迟早两个后端算出不同的路程;树组装错了不会崩,只会让她一整天站着不动。

为了让继承真的成立,父类里三处**直读表**改成走可覆盖的方法(`seed_defaults` /
`seed_tree` / `seed_duties` 里的 `SELECT COUNT(*)`)。父类留一处直读,子类就得把整个
方法重写一遍,而重写的那份迟早和这份不一样 —— 和 `events` 那次是同一条教训。

**互验又抓到两处**:

1. 我把地图种子当成了**嵌套 `children`**,而真实形状是**扁平列表 + `parent` 字段**。
   我照想象写了递归,结果只播出顶层那一个。顺带把拓扑排序也改成复用 SQLite 版那份
   `_parents_first` —— 排序也不该有第二份。
2. `get()` 返回的行少一个 `updated_at`。**行的形状不一样**,而调用方拿的是整行
   dict —— 少一个键会在某条路上变成 KeyError 或静默的 `None`。

真 Redis 上跑通:5 个地点、20 条动作、夏的 15 个树节点全部搬家成功,一个世界日
101 条事件,整个世界 16 个 Redis 键。

### Added —— 记忆 / 提示词模板 / 可见性声明进 Redis(第七步)

一次三张,共用一个新的 `RedisRows` 底座(行在一个 hash 里,主键当 field)。
**底座只管纯 CRUD**:带条件的查询(记忆的三因子检索、事件的过滤分页)照旧各写各的 ——
那些地方的语义差别正是它们存在的理由,套进通用查询层只会把语义磨平。

- `RedisMemoryStore`:检索的打分本来就在 Python 里做(`score()`),SQL 只负责取行,
  所以这一层不用重写检索。**排序键照抄** `ORDER BY tick DESC, id DESC` —— 创世注入的
  记忆全是 `tick=0`,光靠 tick 分不出先后,而不确定的次序意味着**同一个世界在两台
  机器上召回不同的记忆,而且不报错**。
- `RedisPromptStore` / `RedisVisibilityStore`:纯 CRUD。

**互验当场抓到三处我写错的地方**,而这三处单测各测各的绝对发现不了:

1. 可见性声明的字段名我写成 `visible`,SQLite 版是 `visibility` —— 差一个字,
   而 `visible` 是**种子**里那个字段的名字,所以看起来还挺对。
2. 提示词的 `description` 没给时,SQLite 存的是 `None` 不是空串。
3. 改模板时 SQLite **保留**原说明,我第一版把它抹掉了。

顺带两处结构性修补:

- 遗忘曲线的公式抽成 `memory_retrieval.decayed_strength()`,两个 store 共用。
  同一条曲线存两份的后果不是崩,是**两个后端的角色以不同速度遗忘**,而两边都能跑。
- `VisibilityStore.labels()` 补给了 SQLite 版 —— 我在 Redis 版先写了它,而
  **接口不对等就不是替换**:少一个方法,调用方会在某条路上撞见 AttributeError,
  而那条路多半是最少走的那条。

真 Redis 上跑通一整个世界日:创世的 4 条记忆、31 个模板、3 条可见性声明、树高与季节
全部搬家成功;跑完 102 条事件、树长到 3.204、她感知到季节与雨天数。

### Added —— 世界的量进 Redis(第六步),而性能承诺差点丢在路上

`stocks` 是规律每次求值都要读、感知层也要读的那张表。搬法:**每个 owner 一个 hash +
一个 owner 索引集合**。索引不是可有可无 —— `owners(kind)` / `snapshot_kind(kind)` 要
按前缀选,而在 Redis 里对应的是"扫一遍键";`SCAN` 是 O(整个 keyspace),一个 Redis 上
跑十个世界的时候连别人的键都得扫。

**性能承诺跟着一起搬,而这次真丢过一回。** 这一层文档写着"一万棵树跑一个世界日
1.4 秒",而那个数是改出来的:第一版逐个 owner 查、逐个 commit,2000 棵树就到
72ms/tick。Redis 版第一稿在 `write_round` 里给**每个 owner 发一次 `SADD`** ——
两千棵树就是白花的两千条命令,实测整轮写 661ms。改成一次加完之后:

| 2000 棵树 | 批量取 | 整轮写 |
|---|---|---|
| SQLite(基准) | 5.3 ms | 3.7 ms |
| **真 Redis 6.2** | **16.9 ms** | **10.2 ms** |
| fakeredis | 35.2 ms | 40.1 ms |

约 3 倍,不是当年那个 66 倍。而且**真 Redis 比 fakeredis 快一倍** —— 那 661ms 大半是
fakeredis 的 Python 开销,不是协议开销。

这条承诺现在钉成测试,而且**不测绝对时间**(机器和后端都会变),测**命令条数**:
批量取 = 每个 owner 一条 HGETALL 一次问完;整轮写 = N 条 HSET **加一条** SADD。
每个 owner 一条 SADD 就是 2N,那正是丢掉的那次。

互验也升级了:除了逐个方法比对答案,还**让同一条规律在两个后端各跑一轮**,比最后
那棵树长多高 —— `dt` 从量自己的 `updated_tick` 算,tick 有没有跟着值一起存对,
接口比对是看不出来的。

### Changed —— `events` 表只剩一扇门;`RedisEventLog` 可以接管它(第五步)

搬"唯一真相"那张表之前先发现一件事:**有 10 处直接写 SQL 读 `events`**,绕过了
`EventLog`。换后端之后它们会读到一张空表**而且不报错** —— 返回 0 条,一切照跑。
那正是这个仓库最怕的那类坏。所以顺序反过来做:先让这张表只剩一扇门。

- `EventLog` 补齐 `count(who, kind)` / `max_seq()` / `page(since_seq, limit, who, kind)`。
  `api.py` 里的 4 处(`state` 的 max seq、`runtime` 的计数、`history` 的过滤分页)
  全部改走它,**`api.py` 里 `FROM events` 归零**。
- `RedisEventLog` 接口逐字相同,可直接替换。**用列表不用 Stream**:`seq` 在这个引擎
  里是 1 起的连续整数,而投影、分页、`since_seq` 全建立在"它连续"上;`RPUSH` 返回新
  长度正好就是 seq,原子,而 Redis 单线程 —— **两个进程同时追加,各自拿到唯一且递增
  的号**。Stream 的 ID 是"时间-序号",换过去要把 `seq` 的语义一起改,而 `seq` 是跨
  仓库看得见的东西(`events export` 的每一行、`history` 的分页游标)。
- **两个实现互验**:同一串事件喂给两个后端,`replay` / `count` / `max_seq` / `page`
  逐个问题比对答案。换后端最坏的坏法不是崩,是"两边都能跑,但答案不一样"。
- 并发验证:4 个线程各追加 25 条,100 个 seq **不撞、连续**。没有这一条,
  "日志是唯一真相、重放能重建状态"整个失去依据。
- 代价写明:`who` / `kind` 过滤在客户端做(列表没有二级索引)。一个世界日约 100 条,
  一年 3.6 万条,这个量级没问题,但**它不是能一直撑下去的形状**。

⚠️ **`__main__.py` 里还有 6 处直读**,而它们是另一类:`doctor` / `events export` /
`report` / 创世空库判断,全是**在没有 World 的情况下离线看一个 db 文件**。世界一旦
住进 Redis,"离线看文件"这件事本身就不成立了 —— 这些命令(以及 `.cyberworld` 打包、
可挂载性联锁、创作台按版本归档)都要重新想。这是把世界搬出文件的真实代价,不是遗漏。

### Added —— 规划进 Redis;而**投影不进**,它追赶(第四步)

- `_plans` 是真状态,搬走,和前面几样同一个模式。
- **`_memory_projection` 不搬。** 它是从事件日志折出来的**派生结构**,而日志本来就是
  共享的。存一份派生数据的唯一后果,是多出一种"它和日志不一致"的坏法 —— 而这个仓库
  最怕的正是那类。
- 但**不搬不等于不管**:进程 A 记了一条 `payment`,进程 B 的投影里那笔钱还没动,
  而 B 正是靠投影判断"她买得起吗""他们认识吗"。所以加了
  `Scheduler.catch_up_projection()`:把别的进程写进日志、这个进程还没折进来的事件
  补上,`act()` 在动手之前先调它。没有新事件时是纯读一次 db。

### 在**真 Redis** 上验过(不只是 fakeredis)

本机没有 `redis-server`,用 `redislite` 自带的真二进制(**Redis 6.2.14**)起了一个,
17 条 Redis 测试全绿 —— fakeredis 与真 Redis 行为一致。外加两个真进程的完整故事:

```
[进程A] 跑到第 50 tick
[进程B] 第 50 tick | 夏 在 cafe 精力 1.0 在做 sleep | 路上:否
[进程A] 让她去工作室
[进程B] 第 50 tick | 夏 在 cafe … | 路上:去workshop
[进程A] 她到了
[进程B] 第 53 tick | 夏 在 workshop … | 路上:否
[进程C] 从 Redis 读她的打算: [{"kind":"walk",…},{"kind":"work",…}]
```

进程 B / C 里没有 World、没有调度器、没有世界文件 —— **只有一个 `redis://` URL**。

### Added —— 谁在路上、谁在干嘛,也进 Redis(第三步)

`_transit`(在途)与 `_current_action`(当前动作)此前是纯内存的 dict。后果很具体:
**另一个进程不知道她正在赶路**,于是会让她"走开"、让她跟一个还没走到的人搭话 ——
而"在途"这道闸恰恰是引擎用来把约束变成等待、把等待变成相遇的(提示词里那段自相矛盾
的身份声明,修的是同一种病的另一扇门)。

- `RedisDict` 是住在 Redis hash 里的 dict,**只实现真正被用到的那几个操作**
  (`get / pop / items / [] / in / len / bool`)。不做成通用 MutableMapping:多实现
  一个方法就多一处"它看起来像 dict,但在某个边角上不是",而那种错最难查。
- `items()` **返回快照**:调用方会在遍历里改它(`_transit` 就是边走边删),而对着一个
  活的 hash 边遍历边删,行为取决于服务端实现。
- `_current_action` 存 `ActionDescriptor`,不是 JSON 原生的,所以带 `encode`/`decode`
  —— 丢了类型的话取回来是个 dict,而调用方全都在 `.kind` 上判断。

两个真进程验过:进程 A 让她去工作室,进程 B(只有 `redis://` URL)读到
"**她在去 workshop 的路上,第 53 tick 到**";她到了之后 B 读到"不在路上"。

### Added —— 时钟与跨进程的世界锁进 Redis(第二步)

- **时钟只能有一个答案。** 两个进程各推各的,世界就分叉了 —— 而分叉之后两边都还在
  正常跑,只是不再是同一个世界。`Scheduler.clock` 变成可替换的盒子,默认在进程内
  (行为逐字不变),给了 Redis 就住进去。实测每 tick 只读 7.3 次(黑板是 80),
  所以**不缓存** —— 任何进程随时读到的都是真的现在。重开世界不把时钟拨回去。
- **跨进程的世界锁。** `RedisLock` **在调度器那把 RLock 之外,不是替代它** ——
  那把还被 `threading.Condition` 用着(等规划落地),而 Condition 要真线程锁。
  可重入(一个动作里工具会再拿一次,`move_agent` 自己就拿),有 ttl(拿着锁的进程
  崩了世界不能永远停摆),释放走 Lua 比对 token(直接 DEL 会删掉别人刚拿到的那把)。
  `act()` / `intend()` 都在它下面执行,于是**一个动作跨进程也是原子的**。

装到干净 venv 用**两个真正的操作系统进程**验过(fakeredis 的 TCP 服务代替本机没有的
redis-server):进程 A 跑世界,进程 B 只有一个 `redis://` URL —— 没有 World、没有
调度器、没有世界文件 —— 读得到第几 tick、她在哪、精力多少,写了之后 A 立刻看见。

顺带修一处我自己引入的脆弱:`clock` 从字段变成属性之后,`Scheduler.__new__` 绕过
`__init__` 再设 `clock` 会 AttributeError(`_gossip_seed` 的跨进程稳定性测试就是这么
用的)。属性必须是**全函数** —— 换实现不该让一种正当用法炸掉。

### Added —— 运行时状态可以住进 Redis:进程不再持有变量(第一步)

世界的真相此前**只有一半在 db 里**:事件、记忆、关系、量都落库,而**黑板**(每个角色
20 个键:她在哪、在干嘛、饿不饿、打算做什么、行为树这一 tick 选了哪个动作)、时钟、
在途集合全是 Python 对象。两个进程各开同一个世界文件,会读到同一份历史,然后**在各自
内存里跑出两个不同的世界**。

- `anima_world/redis_state.py`:`RedisBlackboard` 接口和进程内那个**逐字相同**
  (`read` / `write` / `snapshot`),所以它是直接替换 —— 行为树、需求、调度器一行都
  不用改。`World.open(..., redis=r, world_id="x")` 即可。
- 实测:第二个"进程"(只有一个 Redis 连接,没有 World、没有调度器、没有任何本地状态)
  读得到她此刻在哪、饿不饿,改了之后第一个进程**立刻看得见**。
- 搬家不是清空:创世写进黑板的性格、位置跟着走,否则第一个 tick 她没有性格。
- `world_id` 进键名:一个 Redis 上跑十个世界是常态,键撞车的后果是两个世界的同名角色
  共用一个脑子。
- **代价照实报**,不假装没有:每 tick 80 次黑板访问,一个世界日 22949 次。
  `CachedRedisBlackboard` 提供"一 tick 一读一写"的批量版(往返 80 → 2),但它那一 tick
  的状态**确实在进程内存里** —— 所以只在"同一时刻只有一个进程推时钟"的前提下能用,
  有测试钉住这个代价是真实存在的,免得有人当成免费加速。
- `redis` 是**可选依赖**:不给 `redis=` 的世界一行 Redis 代码都不碰。"装上包就能跑一个
  世界"是这个引擎的底线,不该为一个可选后端加一个必需的服务。

顺带修了一处**接口泄漏**:`scheduler._blackboard_to_dict` 直接摸 `blackboard._data`,
而那个属性在 Redis 版上根本不存在。补了 `Blackboard.snapshot()`,两边都有。
接口漏一个洞,替换就会在运行期炸 —— 而它确实炸了一次。

⚠️ **这是"全部状态进 Redis"的第一步,不是全部。** 时钟、`_transit`、`_current_action`、
`_plans`、记忆投影仍在进程里。

### Added —— `World.intend()`:告诉她接下来打算做什么,世界替她走完脚步

`act()` 是"现在做这一件事",`intend()` 是"接下来这几步"。区别不是语法糖:一个 LLM
驱动的进程**不该一步一次网络往返地编排走路**,那又贵又编得烂。

- 意图队列进黑板,新的 `IntentAction` 节点排在**身体之下、排班之上**:饿到紧急线她
  会先去吃、吃完回来接着走(**被打断是特性**);而她此刻明确决定的事压过排班表,
  否则"她自己决定"是假的。
- **一步真生效了队列才往前走一格。** 走路要花很多 tick,期间意图节点被重挑很多次 ——
  挑一次弹一次会把后面几步一起吃掉,而她只走了第一步。和 `_current_action` 只在世界
  放行时才记录是同一条规矩。
- **队列空的世界行为逐字不变**(意图节点此时 FAILURE),纯加法。

⚠️ 一处更正:此前(设计文档与上一条 CHANGELOG)说"复合动词已经存在了,`bt_nodes` 里
13 个具名 sequence" —— **那是错的**。查下来每个具名 sequence 都是
`(时间窗, 一个动作)`,即**日课**:`open_cafe` = `walk(cafe)` 限定 07:30–08:00。
数据里根本没有多步复合。所以这一条不是"暴露已有的复合",是给她一个意图队列。

### Added —— 每个动词声明它把世界改在哪儿,而且有测试验它真的改了

CLAUDE.md 那条"**她的选择必须在世界里兑现**"此前是一句**人得自己记住**的话。这一版
靠人肉找出了八处违反它的地方,每一处都是"能跑、不报错、给错东西",每一处都是玩到了
才发现的 —— `broadcast` 声称"世界里的人都能看到"而只发了一行没人消费的日志、
`walk_away` 对不在场的人是空动作、world-rules 写 `world_x` 落在别人名下而仪表报成功。

- `ToolSpec.writes` 声明这个动词写哪些表 / 发哪些事件,`World.verbs()` 与
  `contract --json` 都带出来 —— 外面的进程不该靠猜。
- `tests/test_verb_writes.py` 在真世界里逐个动词调一遍,比对声明的地方**到底变没变**。
  把 `broadcast` 退回"只发日志"这个真实发生过的 bug,现在当场红。
- **空的 `writes` 不许留**,除非显式登记进 `CHANGES_NOTHING` 并写明理由(现在只有
  `wait_for_user`:显式让位只改本轮流程,世界不变)。加一个动词而忘了声明,当场红。

写这条测试时它**立刻自己挣了钱**:我按直觉填的 14 条声明里**五条是错的** ——
`work` / `sleep` 发的是 `state_change` 不是 `agent_action`,`eat` 还会发 `payment` 与
`item_consume`,`talk_to` 还会发 `memory_seed`。**它逼我去读实现,而不是照直觉写文档。**

### Added —— 过日子的动作成为一等动词(`body` 面)

修的是一个此前没人注意到的割裂:**同一个人有两套能力,取决于谁在触发她**。聊天里她能
"走开",排班表里没有这个词;排班表里她会"走到咖啡店",而她自己决定时挑不了 —— 因为
走 / 吃 / 干活 / 睡 / 搭话住在 `bt_actions` 表里,只有行为树够得着。

- 新增 `body` 面与七个动词:`walk` / `work` / `eat` / `sleep` / `talk_to` /
  `wander` / `seek_company`。`act(agent, "walk", {...}, surface="body")` 即可。
- **实现一行都不重复**:全部委托 `Scheduler.emit_action`(行为树走的那条路),
  经由新的 `ToolRuntime.do_action`。于是"排班让她走"和"她自己决定走"在世界里是同一件
  事 —— 一样发 `travel` / `location_join`、一样花时间、一样在途中不可打断。另写一份
  "外部版本的走路"迟早和行为树那份分叉,而分叉的那天没人会发现。
- **纯加法,提示词逐字不变**:`body` 那批不进聊天/自主菜单(验过:能力块仍是原来那
  七条)。把 `walk` 摆进自主菜单是另一件事,它改提示词,得接真模型验过再说。
- **世界说"还不行"照实报**:`emit_action` 返回 `False`(她在赶路、要找的人不在这儿)
  是 `ok=False` 而不是成功。编造的地名当场拒,并列出真实地点。
- 顺带还原 `bt_actions` 的身份:它不是动词表,是**已经绑好参数的调用表**
  (`go_to_cafe` = `walk(location="cafe")`,`chat_with_夏` = `chat(target="夏")`)——
  那 20 行是 7 个动词的若干绑定,而 `chat_with_*` 会随人口线性增长。

新增的那条统一性测试**当场自己挣了钱**:它发现引擎里"闲着"有两种(`idle_wander`
兜底、`idle_social` 找人),而我只登记了一个凭空捏造的 `idle`。合并成一个会让她
再也表达不了"我想找人",而那是需求系统里 social 那条曲线唯一的出口。

### Added —— `World.act()`:外面的进程改变这个世界的唯一入口

此前"她做了什么"只能由引擎内部触发 —— 聊天那一轮、定时轮次、节拍脚本。一个住在别的
进程里、由 LLM 驱动的角色**碰不到任何动词**,于是"很多进程操作同一个世界"这件事在
引擎这一侧是断的(设计见 `docs/AGENT-RUNTIME.md`)。

- `World.act(agent_id, verb, params, *, player_id, surface)` 提交一个动作,
  `World.verbs(agent_id, surface)` 是它的配套目录(给了能力却不给目录等于没给)。
- **一个动作是原子的**:整个执行期持有世界那把唯一的锁。不是为了性能(锁每次只持
  62 微秒,而一次 LLM 往返 6.5 秒),是因为 world-rules 的双缓冲、三源仲裁、
  `events.seq` 的折叠顺序都要求"一个动作期间世界不会从下面被换掉"。有测试拿另一个
  线程去抢锁验这一条。
- **在执行时校验,不在决定时**:她想了 6.5 秒,决定送达时世界早变了 —— 所以"还在不
  在场""走不走得掉"由动词自己在执行那一刻查(`walk_away` 隔着手机降级成挂断就是这
  个模式)。**不在 `act()` 里预先校验**,那会变成第二份判断,迟早和动词里那份分叉。
- **面是硬的**:`walk_away` / `end_conversation` 需要"对面有个人",默认的 `autonomy`
  面上没有它们。拒绝时说清它在哪个面上 —— 只说"不行"的错误信息等于没说。
- 未知动词 / 工具失败返回 `ok=False` 并说明原因(一个 agent 进程挑错动词不该让世界
  崩),未知角色抛 `KeyError`(那是调用方搞错了对象,不是一次失败的尝试)。
- 结果形状与聊天里的工具调用**逐字相同**(共用 `ToolResult.to_dict`)。
- ⚠️ 它**不推进世界的时间**。"时间是动作的副产品"那一半还没实施。

装到干净 venv 里验过:三个独立进程各自读感知、读动词表、提交广播,记忆正确落到在场
的人身上。**但那不是目标架构** —— 那三个进程是先后跑的、各自开了 world.db,而目标
形状里 agent 进程不碰世界文件,只跟世界进程说话。这次验的是这扇门通了。

顺带:两天前建的文档闸门(`test_reference_docs.py`)当场拦住了这次改动 ——
`['act', 'verbs']` 没写进 REFERENCE 就是红的。它按设计工作了。


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

### Fixed —— 文档对账:REFERENCE 说的话有四处不是真的(2026-07-30)

REFERENCE 是**宿主照着写代码**的那份东西,而它和代码之间此前没有任何机械联系。逐行
对账查出四处:

- **`world.declare_visibility(kind, …)`** —— 真实形参是 `owner_kind`。位置调用照样
  能用,所以永远不会有人发现 —— 直到有人写关键字参数。而种子里那个字段**确实**叫
  `kind`,所以这个错特别容易犯。
- **`close_conversation(id)` / `conversation_messages(id)`** —— 真实是 `conversation_id`。
- **`world.history` / `fast_forward` / `report` 在 REFERENCE 里零次出现** —— 三个真实
  的公开 API。`history` 尤其可惜:它是分页的全量历史,而 `broadcasts()` 就是它的一层壳。
- **`contract --json` 的 `chat_tools` 不只有 chat 面。** 字段名和代码注释都写着
  "chat 里她能调的能力",而代码故意取全目录 —— `reach_out` 只在定时轮次里出现,聊天
  里永远调不到。运维台照着字段名做一个"聊天能力"列表,就会给用户一个永远等不到的按钮。
  名字不改(镜像已经在读它,改名等于跨仓库破坏),但**每条加上 `surfaces`**(纯加法),
  `contract` 的人读输出也按面分开打印。

一次性改完没有用,下次加个方法照样飘。所以把对账钉成闸门(`tests/test_reference_docs.py`):
文档写的方法必须存在、写的形参必须对得上、**公开方法必须写进文档**(或者进
`UNDOCUMENTED_ON_PURPOSE` 并说明理由),外加 `contract --json` 的两个数必须等于代码里的
常量。三道闸都做过变异验证。

对账里核对无误的:`.cyberworld` 环回(整库 backup,四张新表自动跟着走)、`contract` 的
beats ops / 谓词(与 `beats.py` 双向无差)、report 的 12 个 bucket 与
`report_format_version`、`needs` 惰性结算。

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

⚠️ 下面这张链接表只覆盖真的**打过 tag** 的版本。**2.0.0 到 3.2.0 一个都没打过 tag、
一个都没上过 PyPI**(理由见 3.3.0 那一节的第一条 Fixed),`1.2.0` 同样从来没有 tag ——
所以它们的标题在这份文档里就是纯文本,不是坏链接。`v3.3.0` 的 tag 留给发版那一下,
**`v3.4.0` 同样没有打** —— 3.4.0 是走镜像上线的,发版那条路的账仍在仓库主人手上。
所以这张表底下**一个 `[Unreleased]` 都没有**:此刻工作树上的东西全都收在 3.4.0 里,
留一条指向 `v3.3.0...HEAD` 的链接会指向一个不存在的 tag,而那种链接点下去才发现。

[3.3.0]: https://github.com/aubrey-anima/core/releases/tag/v3.3.0
[1.1.1]: https://github.com/aubrey-anima/core/releases/tag/v1.1.1
[1.1.0]: https://github.com/aubrey-anima/core/releases/tag/v1.1.0
[1.0.2]: https://github.com/aubrey-anima/core/releases/tag/v1.0.2
[1.0.1]: https://github.com/aubrey-anima/core/releases/tag/v1.0.1
[1.0.0]: https://github.com/aubrey-anima/core/releases/tag/v1.0.0
