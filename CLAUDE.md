# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 先读总图,以及你的角色

开工前先读 `/home/super/src/vibecoding/Anima正式版/docs/ANIMA-分工与协作.md` ——
四个仓库各自管什么、每条跨仓库契约的权威在哪、协作纪律,都定义在那里。
本文件只讲本仓库内部;凡是涉及别的仓库的判断,以总图为准。

**在这个目录里开的会话是 ANIMA 的统领**:既管引擎本身,也管整个系统的跨仓库
架构 —— 契约怎么定、职责怎么划、发版节奏、仓库间的争议裁决。涉及两个以上仓库的
设计决定在这里做,做完落进总图或对应仓库的同步记录。两条随之而来的责任:
**交付要回执**(加了 CLI 出口去 FOR-STUDIO 记一笔,诉求文档的状态要更新),
**改线格式要牵头同步镜像端**(契约表在总图里)。

## 愿景与三条随之而来的纪律

**anima-world:多 agent 共同生活的世界引擎。** 以形式本体论为地基(声明什么存在、
能对它做什么、要付什么代价),让 agent 的每个决定从世界里长出来而不是从提示词里
编出来。长期迭代的开源项目,主版本即世界的可挂载性。
**定位不是"多 agent 协作框架"**(那是 AutoGen / CrewAI 的赛道:消息编排、任务分解)——
这里的协作是从共享的世界里长出来的,这个差别写进一切对外表述。

- **特性可以超前于消费方,但不许超前于愿景。** tool/platform 用不用某个特性,不是
  引擎做不做它的判据;判据是"它让她的哪个决定更像人"。而超前不等于无人问津:每个
  特性必须过三道已有的闸 —— 橱窗里展示它(`test_flagship_seed`)、有 CLI/API 出口、
  进 REFERENCE。三道都过不了的不是超前,是死代码。
- **中文优先,演示与模板必须统一。** README、文档、演示种子、示例里的量名/动词/
  实体名一律中文(和橱窗种子一致:`体力`/`树高`/`照料`),别在文档里写一套
  `stamina`/`height` 的平行英文示例 —— 两套示例迟早只有一套跟着代码走。API 标识符
  照旧英文(`World.open`)。英文世界靠 `label` 机制与英文种子,不靠引擎换语言。
- **许可是 AGPL-3.0-or-later**(仓库自 2.0 起;⚠️ **但已经发出去的最后一版是 1.4.0,
  它是 Apache-2.0** —— 换许可那一版从没上过索引,所以此刻 PyPI 上一个 AGPL 的
  anima-world 都没有,收不回来的那些全是 Apache 的。3.3.0 会是第一个)。
  选 AGPL 而不是 GPL 是因为世界引擎最常见的用法是被网络服务包着不分发,GPL 管不到
  那种用法。改依赖、vendor 代码时注意许可兼容(AGPL 项目里不能收 GPL 不兼容的东西)。

## 这个仓库是什么

**anima-world —— ANIMA 世界引擎**,可发布的 pip 包。**只做引擎**:跑世界、快进、打包成
`.cyberworld`。创作已拆成独立的桌面程序 `anima-studio`(`../tool`),原因见下。

**引擎是纯库,没有 HTTP。** 任何要用世界的模块 import 本包,通过 `anima_world.api.World`
的函数操作世界;世界住在 **Redis**(键前缀 `anima:{world_id}:`),`world_id` 就是
世界的名字。SQLite/world.db 已整体退役(2026-08):没有世界文件,只有键前缀。

`README.md` 是本仓库最准的文档,`docs/REFERENCE.md` 是逐函数/逐命令的参考,改架构前先读。

## 在 ANIMA 系统里的位置

ANIMA 是多个**互相独立**的仓库。协作方式:**Python 宿主 import 本包**(pip 钉版),
非 Python 端与跨版本场景走 **CLI 子进程 + `.cyberworld` 文件**:

```
任何 Python 宿主(如网站后端)──import anima_world.api──▶ Redis 上的世界(宿主进程驱动)
anima-operator(运维台,Node)──CLI / .cyberworld 文件──▶ 本包
anima-studio(创作台)──子进程──▶ 本包的某个版本 ──▶ .cyberworld
```

本机上兄弟仓库在 `../platform`(运维台)、`../player`(网站)、`../tool`(创作工作台)。

**创作台为什么独立**:一个世界文件钉死在生成它的 core 版本上。工具要能同时持有多个引擎
版本并精确指明用哪个,就不能住在其中任何一个里面 —— 它给每个版本装一个隔离 venv,
全部交互走子进程,**永不 import 本包**。所以本仓库里不该再出现任何创作代码。

## 对外契约:本仓库是权威,别人持有镜像

跨语言协作走文件格式,这三个模块是权威定义 —— **改它们的线格式 = 改跨仓库契约**,
必须同步改镜像端:

| 权威模块 | 内容 | 谁镜像了它 |
|---|---|---|
| `anima_world/world_file.py` | `.cyberworld` **线格式**(v3,gzip JSONL) | 运维台 `lib/worldPackage.js` 要整个重写 |
| `anima_world/world_package.py` | 落库那一层(dump / install / import / inspect) | 同上 |
| `anima_world/world_seed.py` | **作者层**的 schema 校验(种子这个概念没了,闸还在) | 运维台 `lib/worldSeed.js` **可删** |
| `anima_world/beats.py` | 节拍脚本严格校验 | (无镜像;创作台经 CLI 委托校验) |
| `contract --json` 的 `storage` 段 | 存储契约:Redis 键前缀 / MySQL 表与表前缀(db.* 段随 world.db 退役) | 运维台镜像要改读 `storage.*` |

`docs/FOR-STUDIO.md` 是给创作台的能力说明 —— **它那侧的判据是"有没有 CLI 出口"**
(库里有而 CLI 上没有,对它等于不存在)。教训是流程的:创作台的
`docs/引擎接口诉求-试炼与试聊.md` 提了两条,我早就交付了却没回执,他们的 P1
白等了几天。**加了 CLI 出口就去那份文档里记一笔。**

v3 那次跨仓库破坏**运维台已经认账**(2026-08-12 核实):`lib/worldPackage.js` 是
v3 的**只读**镜像(写那一半有意不实现 —— 导出要遍历 Redis 里的类型化键并按语义
拆段,那是引擎的活),`lib/worldSeed.js` 已删,双向互验照 v3 重写了。经过在
`platform/docs/引擎-2.0-同步.md`。

⚠️ **新欠的一笔在我们这边**:3.2.0 的 `storage` 段多了 `volatile_keys` 与
`presence`。运维台已加 `STORAGE_CONTRACT` 常量,并让 `test/contract.test.js` 对着
真门 `contract --json` 逐格 deepEqual —— **这一格以后再改,它那侧会当场红**,
镜像本该是这个样子。反过来 `inspectWorldFile()` 已经报 `volatileKeys` 而我们的
`world inspect --json` 不报:同一个包,两边给出不同的清单。

节拍脚本的严格校验有个硬要求:**坏脚本必须在加载时当场报错,不能流到世界启动**。
**显式指定的世界文件同规矩**(`WorldFileError`):作者层只读进空库一次,静默降级成
内置演示世界不可挽回;只有内置那份才降级(装坏了也得能开机)。

Python 侧的对外接口是 `anima_world/api.py` 的 `World` 门面(加上 CLI)。它是宿主应用
依赖的 API 面:**只加不改**,破坏性变更等于跨仓库破坏。
`tests/test_reference_docs.py` 是这条纪律的闸门:**加公开方法就必须写进
`docs/REFERENCE.md`**(或进 `UNDOCUMENTED_ON_PURPOSE` 并说明理由),文档写的形参名
必须和真实签名对得上。REFERENCE 是宿主照着写代码的那份东西,而它和代码之间原本没有
任何机械联系 —— 一次人工对账就查出四处不实。

## 常用命令

```bash
pip install -e ".[dev]"

python3.13 -m pytest -q               # fakeredis,无需真 Redis;addopts 已屏蔽 ROS 插件
# 最近一次实跑:**1788 passed / 19 skipped,365 秒**(2026-08-20 第八轮,`.venv/bin/python`)。
# ⚠️ 这儿原先写着 1774 —— **秒数一直是对的,件数烂了十几件**,而两个数并排放着,
# 没有一处会因为其中一个过期而报错。**这个数每加一批测试就变,别把它当判据**;
# 它唯一的用处是**别往下掉**:掉了就是有测试悄悄不跑了(collect 报错、夹具坏掉、
# 整个文件被 skip),而屏幕上照样是一片绿点。

# 要真 MySQL 的那些(默认 skip)。**`mysql=` 那条路的替身只有真 MySQL** ——
# store 级三方互验全绿而真 MySQL 上一开就炸,已经吃过一次(`MySQLChatStore.__slots__`)。
docker run -d --rm --name tmp-mysql -p 127.0.0.1:13499:3306 \
    -e MYSQL_ALLOW_EMPTY_PASSWORD=yes -e MYSQL_DATABASE=anima_test mysql:8.4
ANIMA_TEST_MYSQL=127.0.0.1:13499 python3.13 -m pytest -q    # 那道门在 tests/_realmysql.py
docker rm -f tmp-mysql
python -m build                       # → dist/*.whl + dist/*.tar.gz
python -m twine upload dist/*         # 发布

# 世界住在 Redis:每个命令都吃 --redis URL(默认 $ANIMA_REDIS_URL 或
# redis://127.0.0.1:6379/0)与 --world-id(默认 $ANIMA_WORLD_ID 或 world);
# 可选 --mysql DSN 让无限增长的四样进 MySQL。

# 给人用的三个命令
anima-world start                     # 引导配 LLM → 创世 → 前台运行;新世界用演示速度
#   创世 = 装内置的 demo.cyberworld;换一个世界用 --world-file(--seed 已移除)
anima-world doctor                    # 体检:Redis 持久化、密钥、真调一次 LLM、时钟翻译成人话、
#   自主链通没通、**要花时间的长过程有几件真做完了**(按事件日志数,不是按本次开机)
#   ⚠️ 退出码是**总账**:它数的是这一趟里"需要处理"的项数之和,所以一个长过程一件没丢的
#   世界照样可能退 1(比如这台 Redis 没开 AOF)。别拿 `doctor` 的退出码当单项判据。
anima-world config set llm.api_key sk-…   # 机器键自动进 ~/.anima-world,世界键进 :config

# 给改提示词的人用的(run_prompt / World.debug_prompt)
anima-world prompt --world-id w --agent 夏   # 她收到的提示词,逐块带来源 + 少了哪块为什么
# 它和真聊天共用 `ChatService.prompt_blocks` —— **调试视图另写一遍拼装就会撒谎**。

anima-world map --world-id w --day 2 --agent 夏   # 地图 + 谁去了哪儿;--now 只看此刻
# **渲染是赠品,`--json` 才是契约**。只读命令对不存在的 world_id 一律拒绝 ——
# 抄错名字会当场创世,你看到的是一张"排版正常、时钟 0"的地图(5ce6aed 的教训)。

anima-world ontology --world-id w [--kind tree] [--json]   # 有哪些种类、什么量、能干什么
# **能力表只有这里问得到**:`stocks` 给得出数字,数字不告诉你 `tend` 这个词存不存在。
# 猜一份动词表出来是这一层最容易犯的错 —— 猜错了不报错,按钮点下去才发现世界不认。
anima-world ontology --world-id w --check          # 出生自检:量落地了吗、能力算得出结论吗、在场吗
# 有问题时退出码 1,所以它能进 CI(和 `--ticks 0` 当校验器同一个用法)。

# 给部署/脚本用的
anima-world run --world-id w                          # 前台宿主,Ctrl-C 停
anima-world simulate --world-id w --ticks 288         # 无头快进;--ticks 0 = 只创世
anima-world world export --world-id w --output my.cyberworld \
    --package-id my-world --name "我的世界"
anima-world world import my.cyberworld --world-id w2  # 目标必须是空世界
anima-world world drop --world-id w --yes             # 整个抹掉一个世界(不带 --yes 只数)
anima-world world inspect my.cyberworld               # 只读第一行:要哪个引擎、多大
anima-world validate world my.cyberworld              # 不建世界就查作者层
# `.cyberworld` 是 JSONL:`zcat x.cyberworld | grep '"type": "entity_spawn"'` 真的能用。
# ⚠️ **冒号后那个空格是承重的**:记录用 `json.dumps` 默认分隔符写出去,所以
# `'"type":"entity_spawn"'`(这里和 README 里原本就是这么写的)**一条都匹配不到** ——
# 而 grep 找不到时退出码 1、屏幕上什么都没有,和"这个世界确实没生过东西"长得一模一样。
# ⚠️ **"gzip" 是写出去那一半的规矩,不是读进来那一半的**:`world export` 永远写 gzip
# (且 `mtime=0`,保证可 diff),而装载器**只看头两个字节**,裸 JSONL 照收 —— 手写一个
# 世界不该被逼着先压缩。所以包里自带的 `anima_world/demo.cyberworld` 就是**裸文本**
# (有意的:一个 review 不了的二进制块不该是新用户看到的第一眼),对它 `zcat` 会退 1,
# 得用 `cat`。一句"`.cyberworld` 是 gzip JSONL"照着敲会在唯一一个人人手上都有的
# 文件上失败。
```

嵌入到应用里(主要用法):`from anima_world.api import World` →
`World.open(world_id, redis=redis.Redis.from_url(url, decode_responses=True), mysql=None)`。
**`decode_responses=True` 是契约的一部分**:裸 bytes 客户端下量名变成 `b'树高'`,
规律静默失配。

## 关键不变量

- **会过期的断言必须带日期,否则它会以"现状"的身份被一直引用下去**(2026-08-20 立)。
  这个仓库的 docstring 是**病历不是现状**:大半在讲一个已经治好的病。可病历里那句
  「今天线上根本没人调 `player_move`」写的是 3.2.0 那会儿的实况,站点 2026-08-13
  前后就接上了,而那句话一个字没改地躺在 `intent._colocation_refusal` 里 —— 直到
  一个验收员照它推出「这扇门在线上一次都不会开」,并把这个假结论写进了验收报告。
  **代价是别人替我们错了一轮。** 规矩:凡是写「今天/目前/线上/一次都没」的句子,
  当场补上**量它的日期与版本**;实况过期了**别删,标注**(删掉等于把当初的判断
  依据也一起删了,下次没人知道这条闸为什么默认关着)。判据很好用:
  **这句话半年后还成立吗?不成立就必须带日期。**
- **`start` 是人的门,`run`/`World.open` 是程序的门**:`start` 会引导配置 LLM、给新世界
  换成演示速度(1 tick/秒);`run` 和 `World.open` 一概不做。改 onboarding 时别把这
  两条路径搅在一起。
- **世界住在 Redis 里,`redis=` 是必填**:黑板/时钟/在途/当前动作/规划/需求/意图/
  关系图/姿态/量/转录(无 MySQL 时)全在 `anima:{world_id}:*` 下,很多进程可以同时
  操作同一个世界,`act()`/`intend()` 在一把跨进程的世界锁(`:lock`)下执行。
  **创世与重连共用一条纪律:只填缺,不覆盖** —— 黑板 `seed_missing`、时钟 `setnx`、
  每个 seed 函数空 store 才播;整份写回等于拿创世快照倒带她。这条踩过两次:黑板搬家
  漏过它(第二个 `World.open` 悄悄把她挪回 cafe),`_WorldView` 用投影"恢复"位置又
  踩了一次(投影是重折出来的**过去**,黑板上可能躺着别的进程写下的**现在**)。
  **黑板接入必须晚于事件重放**(`build_serve_scheduler` 尾部),否则重放会把 Redis
  里的现在盖回创世值。**记忆投影仍在进程里,是有意的**:派生数据存两份只会多一种
  不一致的坏法,重折廉价且必然正确(`catch_up_projection`)。
  **一个进程一个引擎版本;信任边界是进程边界**(`api.py` docstring 的三条纪律)。
  开机会点名这个 Redis 的持久化(`durability_warning`):AOF 关着的 Redis 一重启,
  世界退回创世那一刻,而且不报错。
- **给了 `mysql=` 的世界把无限增长的那几样交给 MySQL**(`{world_id}_` 表前缀):
  `events` / `memories` / `conversations` / `messages`;没给 MySQL 时它们住 Redis
  (转录走 `RedisChatStore`),接受随时间增长的账。归 MySQL 后 Redis 里的旧拷贝
  会被删掉 —— 两份真相里一份不更新,是这个仓库最怕的坏法。**判据最终是"她带不带得进上下文"**:Redis 装的是
  她此刻要带进提示词的东西,而 LLM 的上下文本来就有上限 —— 两个"有上限"是同一个上限。

      进得了提示词的  →  必须有界  →  Redis
      进不了提示词的  →  可以无限  →  MySQL(要用时按 k 取回来)

  这条比"冷热"和"增长性"都好用,因为它**可验**:分对了的话提示词就不随世界变老而涨。
  实测 60 世界日,后端涨 61 倍(事件 50→3264),提示词 2251→2272 字。
  `tests/test_bounded.py` 是这道闸。加新表时问一句:**她会把它带进提示词吗?**
  会 → 那它必须有界。⚠️ **`edges` 一度被分错**(照着"像不像历史"分的):它有
  `UNIQUE(subject,predicate,object)` 且谓词是闭集,上界 2×N²,**按世界的规模封顶** ——
  它属于 Redis。而那个闭集是承重的,放开谓词就要重算这笔账。
  ⚠️ 而**有界性是渲染器的属性,不是存储的属性**:本体层进来之后世界里可以有一万棵树,
  它们住 Redis 还是 MySQL 一样把提示词撑爆。所以"每个能进提示词的类型必须声明一个带
  上限的选择器"这条归渲染那一层(`perception.perceive` 的 budget、`Ontology.budget_of`),
  不归分家那张表。**截断了必须吭声**(`Perception.overflow` → "你没细看"):不说的话
  她在一个"她以为只有三棵树"的世界里做决定,而她永远不会知道自己被骗了。
  ⚠️ **`mysql=` 传工厂,别传裸连接**(`mysql=lambda: pymysql.connect(...)`,引擎自动
  包成每线程一条):`pymysql` 的 threadsafety 是 1 而引擎有线程池,共用一条会让协议帧
  交叉、连接作废 —— **而且不是必现**,所以开机时当场点名,不指望人记得。
- **LLM 的钥匙住在这台机器上,不住在世界里**(`machine_config`,`~/.anima-world/config.json`,
  0600)。解析顺序:环境变量 → 机器配置 → 世界配置(旧世界兼容,`doctor` 点名)→ 默认值。
  **人不手写环境变量**:`config set` 与 `World.config_set` 自动路由,`start` 的引导直接写它。
  理由是 `.cyberworld` 是**分发物** —— 打包发出去的世界不该带着作者的钥匙。
  **世界里一个 secret 都没有**:Fernet/keyfile 已随 SQLite 整体退役,往世界里写
  非空密文值现在是**当场 `RuntimeError`**(不是加密,引擎手里已没有钥匙),
  `config set llm.api_key` 自动路由进机器配置。降级照旧不许无声:`World.state()`
  的 `runtime.llm.degraded_reason` 常驻。
- **世界里只存作者动过的**(1.4.0;如今在 `:config` / `:prompts` 两个 hash):
  创世**不播**引擎默认值,读的时候按环境变量 → 机器配置 → 世界 → `_DEFAULTS` 解析,
  `config list` / `prompt_list` 每行带 `source`。理由是播下去的那份是**创世那天的快照**:引擎把 `chat.recall_k`
  从 3 改成 99,已有的世界一个都吃不到,而 `config list` 看上去一模一样。
  于是 `:config` 里剩下的就是作者的意见(内置种子的世界 = 8 行,毛坯 = 0 行),
  `:prompts` 是 0 行。两个连带纪律:
  - **判据是"引擎声明过什么",不是"表里有没有行"** —— `has` / `meta` / `list` / `set`
    四个都要回落。`set` 曾经漏了:一个新世界的表是空的,于是
    `set("llm.api_key", …)` 拿不到 `is_secret`,**密钥明文写进世界文件而且一声不吭**。
  - **落库的永远是作者动过的**:`list()` 是合并视图,整份落库等于把刚拆掉的
    快照原样重建(31 条默认模板的教训)。
  `tests/test_config_provenance.py` 是这几条的闸。
- **水位前进的条件是"折过了",不是"我写了一条"**(`_projection_seq`)。它的含义是
  「≤ 它的事件我都折过了」,而 `catch_up_projection` 只往前看 —— 任何一处把它推过
  没折过的事件,那些事件**再也补不回来**。`_apply_memory_trigger` 曾经在自己追加一条
  时直接挪到自己那条的 seq,于是**别的进程写的一整段被签了字**:线上从维护容器写的
  四张角色卡,长驻世界里永远是 `card: null`;`player forget` 更贵 —— **关系就是投影**,
  共享 Redis 清干净了而那个进程内存里的幽灵留着,她继续惦记一个不存在的人。
  一个跑着的世界每 tick 都在追加事件,所以这**不是竞态,是必然**,而且零报错。
  修法是自己追加前先补空档(`_fold_gap_before`),**只在真有空档时才多跑一次 replay**
  (无条件 replay = 每条事件一次 Redis 往返),补的时候按 seq 截断别把自己折两遍。
  写 `_projection_seq` 的地方只该有三处:开机、`reset_projection`、真折过之后。
  连带一条:**只读门自己补课**(`state()` / `roster()` 已加)—— 跑着的世界会在下一次
  追加时自愈,暂停的不会,而只读门不该指望世界正好在动。
  `tests/test_cross_process_projection.py` 守这几条。
- **投影拷一份,别拿事件那个 dict 当自己的状态**(`_apply_agent_join`)。投影是从事件
  折出来的**派生数据**,事件是已经发生过的**事实**;共用一个可变 dict 的话,后来的一条
  `persona_update` 会顺着 `agent.spec.update(...)` **就地改写那条创世 `agent_join` 在内存
  里的样子** —— 于是"那条事件说了什么"有两个答案:`World.events()`(内存窗口)一个,
  `events export` 与任何一次重放另一个,而日志才是对的。没造成过数据损失(goals 自己有
  一条 `persona_update` 事件),但它是地雷:投影往后加的任何一处写都会静默改写历史的
  显示。`tests/test_event_log_fidelity.py` 守这条。
- **scheduler 持有进程内唯一的 RLock**(世界时钟 / 邮箱);别再引入第二把锁。跨进程那把
  是 `RedisLock`,**在它之外不是替代** —— RLock 还被 `threading.Condition` 用着(等规划落地),
  而 Condition 要一把真线程锁。
- **聊天子系统与事件核解耦**:整场会话只在关闭时发一个事件。1.3.0 的 chat-agent 逐轮
  观测量(stance / intent / tool_call)因此**落在 `messages` 行上**,不是每轮一个事件 ——
  它们和消息同生共死,所以转录搬去 MySQL 时**跟着一起走**,不另造一张表(为它单独造表
  等于让"她这一轮赌了气"和"她这一轮说了什么"住在两个后端上,再跨库 join)。
  **写它的 SQL 住在表的主人那儿**(`ChatStore` / `MySQLChatStore`),`ChatStateStore` 只转发 ——
  之前挂错了地方,于是 Redis 版一继承就带着一条指向 SQLite 连接的死路 ——
  issue #18/#16 的原文写的是"每轮发一个事件",那条被有意否决了(见 CHANGELOG 1.3.0
  末尾)。工具造成的**后果**(走开 / 广播 / 静音)照旧是世界事件:世界的历史只记世界里
  发生的事。
- **世界只有一种序列化形式:`.cyberworld`(v3,gzip JSONL)。种子这个概念没有了。**
  v3 之前世界有三种表示 —— 人写的种子 JSON、Redis 键、导出的 dump。第一种和第三种
  是同一件东西的两种写法,留着两种就要维护两套 schema、两套校验、两个跨仓库镜像。
  合成一个之后:**一个文件,两层记录**(`author` 装载时编译,`redis`/`event`/`mysql`
  直接落键),于是**创世和还原是同一个动作** —— 往一个前缀里装一个世界文件
  (`World.open(world_file=)` / `--world-file`;`seed_path` 与 `--seed` 已移除)。
  手写的世界只有 `author` 记录;跑过的世界导出来只有状态记录;把只含 `author` 的文件
  装进一个**已有**的世界 = 一次编辑(创作台要的"增删改查创世态"由此免费得到)。
  ⚠️ **这一条曾经只写在这儿、没在代码里**:作者层原先一律只进空世界,想给一个跑着的
  世界补一层 `kinds` 会被"'agents' must be a list (missing)"挡回来 —— 而那个世界的
  名册明明就在它自己的库里。现在它真的能改了,而**分界不是"世界空不空",是"这份文件
  是谁给的"**:没给 `--world-file` 时用的是**内置兜底那份**(它每次开机都在手上,
  拿它去填已有世界的空表 = 把橱窗的橡树塞进别人的世界,世界照跑、日志干净);
  给了 `--world-file` 就是一次**明示的编辑**,生效。语义仍是**只填缺不覆盖** ——
  加得进新东西,不会把这个世界跑出来的现在倒带回创世那一刻。两半各有一条测试
  (`test_startup_diagnostics.py`),**只钉前一半等于把那个 bug 的门重新打开**。
  混装两层的文件(author + 状态)有一条要知道:`world import` **只落键、不编译作者层**,
  而状态一落键这个前缀就不空了 —— 首启不给 `--world-file` 时作者层再也编译不了。
  它现在会**当场说出来**并告诉你把同一份文件指回来;此前是静默丢失。
  ⚠️ 这里曾经写着"随之去掉了**出生证明**(`:meta.world_seed`)",**那句是假的**:
  `_store_genesis_seed` 一直在写它,新世界的 `:meta` 里就有,导出时也跟着包走
  (只剥 `owner_pid`/`owner_host`)。文档说删了而代码还在写,是这个仓库最怕的
  那种不一致的文档版 —— 2026-08-11 一次人工对账查出来的。
  **坏声明一个字都不写**:本体的校验提到了第一次写之前(`_precheck_ontology`,走
  `parse_kinds → parse_entities → resolve` 同一条路)。从前的顺序是"播地图 → 播规律
  → 编译本体",而会失败的是最后那一步,于是一份写错 `kinds` 的文件留下地图和规律、
  `kinds` 空着 —— 作者改好再来一次,那条半截的规律还在,来路不明;更坏的是那次失败
  让这个前缀不再是空的,重试走的已经不是创世那条路。**别另写一份校验**:两份判断
  迟早给出不同答案,而那种不一致会表现成"预检说没问题,开机还是失败"。
  几条硬纪律:**载荷收在一个字段里**(`body`/`value`/`payload`/`row`,不平铺 ——
  `locations` 条目自己带 `kind`,平铺会**静默覆盖**记录类型);**不认识的记录类型与
  不认识的 section 都当场报错,不跳过**(跳过等于安静地少装一半世界,而文件看上去
  完全正常 —— 这条是被一次真的丢段逼出来的);**压缩与否只看头两个字节**(写出去
  永远 gzip 且 `mtime=0` 保证可 diff,读进来允许裸 JSONL —— 手写世界不该被逼着先
  gzip);导出与导入**都是流式的**(v2 的 dump 是全量 `replay()` 再 `SELECT *`,
  没有任何上限)。线格式在 `world_file.py`,落库在 `world_package.py` ——
  **格式模块不认识 Redis,落库模块不认识 gzip**,两边各自能被单独测。
- 世界只收当轮有限历史(`World.chat` 传入、`record_chat_turn` 回传),完整转录留在
  宿主应用里,不落世界。
- **chat-agent 的四个开关默认关**(`chat.stance/tools/intent/loop.enabled`),而且
  **关着时连提示词都不进**:给了菜单却不执行,等于让她照着念一个没人读的标记。
  能力调用走**行内标记**(`directives.py`)而不是 OpenAI 原生 `tools=`,理由是没有 key
  是默认状态、Mock 与本地端点的 function calling 支持参差 —— 只在原生 tools 上可用的
  能力等于在默认状态下缺席。要加原生路径就当降级路径的对偶来加,别替换掉它。
- **她的选择必须在世界里兑现**:`walk_away` 走 BT 那条路真的起程,`delay_reply` 到点真的
  回来敲门(`agent_followups` → `agent_hail`),`narrative_direction` 真的把人挪过来。
  只改提示词的版本("她走了"但下一 tick 还站在原地)就是这几条 issue 要修的病本身。
- **LLM 客户端注入**,**永不在 tick 线程调用**;叙事、规划、关系判定各自跑在线程池上。
- **版本即契约(硬钉版模型的新形态)**:db 格式联锁(`DB_FORMAT_VERSION` /
  `SCHEMA_REVISION` / 表集合钉扎)随 world.db 整体退役 —— 世界不再是一个可挂载的
  文件,键前缀就是格式。留下的两条由 `tests/test_version_contract.py` 守:
  `anima_world/__init__.py` 的 `__version__` 是**唯一版本源**(pyproject 动态读取);
  `contract --json` 自报 `storage` 段与包格式版本(v2),镜像端照它对齐。
  改 Redis 键形状 / MySQL 表形状 = 改跨仓库契约,和改包格式同一级别。
- `demo.cyberworld` 是本包**唯一的 package data**,随 wheel 分发
  (`tests/test_packaging.py` 盯着,漏了会让宿主环境里少文件、世界开不起来)。
  它以**纯文本**进仓库(可 diff、可 review)—— 一个 review 不了的二进制块不该是
  新用户看到的第一眼,而它同时是世界文件格式的说明书。
- **内置种子是橱窗,不是毛坯**(1.3.0):它用 `"config": {...}` 替这个世界点亮了
  needs/economy/social/stance/tools/intent/autonomy,并播了关系、创世记忆、钱、
  物品、货架、目标。理由是**做了却开箱看不见等于没做** —— 新用户装上包看到的第一屏
  就是这个引擎的全部说服力。加了新特性要顺手问一句:**橱窗里展示它了吗?**
  (`tests/test_flagship_seed.py` 盯着。)
  但**引擎默认值仍然全关** —— 分工是"引擎默认值 = 没人说话时的样子,内置种子 =
  这个世界的作者的意见"。所以验"开关默认关"的测试必须用 `conftest.py` 的
  `bare_seed`(把橱窗剥回毛坯),拿橱窗去验默认值等于在验橱窗的布置。
- **画图只画到终端**(`anima-world map`,1.4.0):地图与轨迹的**数据出口**
  (`World.map_data()` / `map --json`)是契约,终端那张字符画是赠品。要 SVG/网页
  归宿主。三件差点画错的事:几何是**相对父级**的(照原始值画,每个东西都在错的
  地方而且不报错)、**中文是双宽字符**(按字符个数排版会把边框推歪)、两个人走同
  一条路时后画的会把先画的抹掉。共同点是**画错了不会报错,只会好看地骗人**。
- **本包无 HTTP、无 HTML、无创作代码**。曾经的 FastAPI web 层(`anima_world/world/`,
  三组 REST API + membership claim 鉴权)已在纯库化改造中整体移除 —— 需要网络暴露的话,
  由宿主应用自己包一层,不归引擎管。`anima_world/author/` 也已删除,
  `import anima_world.author` 必须 ModuleNotFoundError(`tests/test_packaging.py` 守着)——
  留个 shim 就等于给工具开后门。

## 当前状态

**`__version__` = 3.6.0(2026-08-20 定版)。world.db 整体退役,世界只住 Redis(+可选 MySQL)。**
定版历史:3.3.0(08-19)→ **3.4.0**(同日第二轮:地点两格图 `map_image`/`scene_image`、
图的字节闸、`world check`、`agent set-card --portrait-file`、`location set-image`)→
**3.5.0**(08-20:**法务抹除可续可分片**)→ **3.6.0**(同日:`importance` 见证记忆、
玩家进旁白、**邀请门** —— 她点你的名时你得自己答;验收修复走了好几轮,**有几轮现数、
别写死一个会烂的数字**:`git show HEAD:CHANGELOG.md | awk '/^## \[3\.6\.0\]/,/^## \[3\.5/' | grep -c '^### .*轮'`
—— 这儿原先写死着「三轮」,而敲出来是六。⚠️ **这个数每加一轮就动一次,包括写下它的那一轮**:
第八轮 2026-08-20 在这儿填了 `6`,而它自己那一节落进 CHANGELOG 之后,同一条命令当场答 `7` ——
所以这儿有意不再填数,要知道就敲上面那条)。
⚠️ **3.4.0 到 3.6.0 都没打 tag、没发 PyPI,上线只走镜像** —— 下面这段"发布管线"的账
一个字都没销。**这一节曾经停在 3.3.0 整整两个版本**:一份说着旧版本号的不变量文档,
和它自己批评的那把"把上升报成下降的尺子"是同一类东西,所以定版时顺手改它。
⚠️ **PyPI 上最新是 1.4.0(tag `v1.4.0`,2026-08-04 发出并成功)** —— 2.0.0 到 3.2.0
**一版都没上过索引**:v3.0.0 那次 Release(run `31467060331`)死在 `release.yml` 的
冒烟步骤上,那一步还在用 1.x 的 `World.open('rel.db', force_mock_llm=True)`,
而**没人会去改一条从没跑绿过的路**,所以往后每一版都会死在同一行。
那条路 3.3.0 这一轮修了(两个 job 各起一个真 Redis service,形状照 `ci.yml` 的
`package` job —— 那个每次推 main 都跑绿,是这仓里唯一被真 runner 证过的写法)。
**tag 留给用户打**:`v*` 的 tag 就是发版扣动扳机,而 **PyPI 拒绝重复上传同一个版本号,
第一次尝试就是唯一的一次尝试**(那个 workflow 自己的注释写的)—— 所以顺序是
先看一次 CI 绿,再打 tag。
教训记在这儿而不是只记在 CHANGELOG:**一条测不到自己的发布管线,和一把把上升报成
下降的尺子是同一类东西** —— README 的 PyPI 徽章一直亮着、CI 一路绿、许可从 2.0 起
写着 AGPL,而徽章指着的那个索引停在 Apache 时代的 1.4.0,七个月没有一处报错。

这一轮删掉的:`db.py`、`small_stores.py`、`graph.py`、全部 SQLite store 实现、
Fernet/keyfile、db 格式联锁、`--db-path`。补上的:`RedisChatStore`(无 MySQL 时
转录的家)、`RedisConfigBackend`(`:config`)、`RedisRulesStore`(`:world_rules`)、
`meta_rows`(`:meta`:创世出生证明、占用标记)、RedisMemoryStore 的容量淘汰与
锚定不衰减(删的时候测试逮出来两处 Redis 版行为缺口)。`.cyberworld` 升 v2:
`world.db` 成员换成 `world_state.json`(Redis 键的类型化 dump + 可选 MySQL 段),
template 模式删除,导入只进空 world_id。创世 = 种子直写各 Redis store,空 store
才播;测试套件全量跑在 fakeredis 上(**当时** 687 项,无需真 Redis 服务;今天的数见上面「常用命令」)。

同一轮加进来的是**本体层**(`ontology.py` / `RedisOntologyStore` / `anima-world ontology`
/ `World.kinds()` / `World.entities()` / `StockCondition` BT 叶子),纪律见上。
三份文档已跟着 2.0 对过一遍(REFERENCE 的 world.db / Fernet / template / db 格式联锁
描述全清掉了,§8 新增一张**键清单**当名字的权威;FOR-STUDIO 记了 `ontology` 这一笔)。

本体层之上又走了三轮,都在"她的决定从哪儿来"这条线上(REFERENCE §2.9.6.1–4):
Gibson 那一半(`requires`/`costs`/`me_`)、工具与材料(`have_`/`consumes`)、
**时间也是代价**(`duration`/`occupies`/`:engaged`/`World.engagements()`)+ 动词放开。
顺手逮出一个**创世投影折两遍**的老 bug:`__main__` 重折投影却没挪 `_projection_seq`,
于是新世界第一次 `World.act()` 时每个人的钱和随身物品**当场翻倍**,一次,而且只在
创建它的那个进程里 —— 日志一条不错,重开就正常,所以账面上永远看不出来。
修法是把"重折"和"挪水位"焊进 `Scheduler.reset_projection()`:两个字段各写各的就是那个洞
本身。这一条正是"照跑但给错东西"的标本,而**它没被任何测试发现,是因为测试都直接调
`scheduler.perform_affordance`,没走 `World.act()` 那条真路**。

同样是拿真 CLI 演一遍时撞出来的第二个:**播种一直按"这张表恰好还空着"判,而它只在
第一次开机时和创世重合**。之后每次开机,手里那份种子(缺省是包自带的橱窗)都会去填
当初作者**有意留空**的表 —— 一个写了 `kinds` 却没写 `rules` 的世界,重开一次就被塞进
橱窗那条引用 `tree` 的生长规律,而这个世界里没有 `tree`:下场不是算错,是**这个世界
从此打不开**。创作台整套流程都是自定义种子,一撞一个准。`_seed_ontology` 早就用
`fresh_world=` 判了,这一轮把 rules / stock_visibility / stock_places 三个补齐。

原路线图(docs/ROADMAP.md)的 2.0–5.0 四大机制已并入首发,全部带默认关闭的开关:

- **记忆 2.0**(常开):三因子检索、反思、遗忘曲线
- **需求系统** `needs.enabled`:energy/hunger/social 曲线驱动行为树紧急带
- **经济** `economy.enabled`:物品/钱/店铺/价格漂移,账本是事件投影
- **社交** `social.enabled`:三轴关系(常开)+ 八卦传播 + 小团体

1.3.0 加的一层是**她自己的选择**(一次真人 dogfooding 之后,issue #15/#16/#17/#18 一起
兑现),同样四个默认关闭的开关 —— 细节见 docs/REFERENCE.md §2.9.1:

- `chat.stance.enabled` 关系性意图(八枚举,按 (角色,对方) 存)
- `chat.tools.enabled` 聊天里的能力(`anima_world/tools/`:静音/走开/等会儿/拒谈/广播)
- `chat.intent.enabled` 意图分派(对话 / 导演场景 / 改对话规则 + `persona_overrides`)
- `chat.loop.enabled` 连续输出(`World.chat_burst`,预算 f(性格,关系,心情,时间))

**认知层**(perception,1.3.0):世界的量里她感知得到哪些,四档
`self`/`here`/`public`/`hidden`,**没声明 = 感知不到**(反过来的错不可挽回:一个"暗中的
恨意"若默认公开,角色下一句就说出来)。声明本身就是开关,没有 `perception.enabled`。
感知同时进聊天 grounding **与定时轮次的决定上下文** —— 后者是关键,不接上的话模拟层和
角色层就是两套跑在一个进程里的系统。加了新的量要顺手问:**她该不该知道它?**

**世界的规律也是数据了**(world-rules,1.3.0):`stocks` 表存量 + `world_rules` 表规律,
设计者写 `{every, for_each, when, set, emit}`,`set` 里是受限算术表达式。它补的是
"引擎替所有世界写死了物理法则"这个洞 —— needs/economy 的曲线通用,而"树怎么长"不通用。
三条硬纪律:**绝不 `eval`**(AST 白名单 + 自写解释器,`expressions.py`)、
**连续变化不发事件**(只有 `emit` 的门槛跨过去才发,且边沿触发)、
**双缓冲**(同一轮读上一轮的值,规律之间与顺序无关)。跑在 tick 线程上 —— 纯算术。

**本体层**(ontology,2.0):`kinds` 声明"这个世界有哪些**种类**的东西"(量、能力、
提示词预算),`entities` 只装每一个的 id/name/gloss/location —— Type Object,声明写一遍,
提示词引种类而不重复它。**这是有界性的来源**。七条:

- **声明本身就是开关**,和 perception 逐字同构:不写 `kinds` 的世界这一层整个缺席,
  行为与从前逐位相同。一旦作者写下 `kinds`,他就是在说"我已经声明了这个世界有什么" ——
  于是建议变成闸:量名拼错(`树髙` vs `树高`)**当场开不了机**。放行的样子是这个仓库最怕
  的那种:安静地建成第二个量,规律照跑、日志干净,作者要到发现那棵树三个月没长才知道。
- **加载时严格、运行期降级**:坏声明拒绝整个世界并**一次列全**(所以 `--ticks 0` 能当
  校验用);运行期出错只跳过一条规律。
- **能力(affordance)不是规律**:规律有意拒绝跨实体写(双缓冲下扇入没有意义),而一次
  能力调用是"一个人、一个东西、一个瞬间",那条反对不适用。走 `act(agent, "interact",
  {"target","verb"}, surface="body")` 这一条统一路径。⚠️ **一次交互是一下子的事,不是
  一个状态** —— 让 `interact` 赖在 `_current_action` 上,按跃迁发事件的那一处就只会发一次,
  然后永远沉默。(**除非它起了一件占着她的长过程**,见下。)
- **两道闸,别混**:能力调用上的是**同处一地**(`perform_affordance` 的 `absent`,
  两头都说 —— 只说"它在 cafe"会读成一句谎,真正的原因可能是引擎不知道**她**在哪);
  排班按量分支上的是**感知**(`_settle_stock_watches`)。后者的理由是她拿一个自己感知
  不到的量做决定,和她说出矿里还有多少矿一样出戏,而且**连提示词痕迹都不留**。
  这三个理由要分得开:`hidden`/`not_mine` 是作者的静态错误(警告一次,然后永远写
  `None` —— 留旧值等于让她按上一次路过时看到的数字决定),`elsewhere` 是正常且短暂的
  (闭嘴)。而 `perform_affordance` 的 `conditions`(果子还没熟)与其余理由也要分开:
  混成一个,她就不知道该等一会儿还是该换件事做。
- **能力是施动者与对象之间的关系,不是对象单方面的属性**(Gibson;`requires` / `costs` /
  `me_` 前缀)。之前能力只读得到对象身上的量,于是谁都一样能干 —— 一个人可以连着照料
  一百棵树而不累。而**一个总能成功的动作产生不了任何决策**:没有理由挑先做哪件、
  没有理由歇一会儿、也没有理由变强。挡住它需要"她身上也有量",所以 `agent` 是唯一能在
  数据里被扩写的内置种类,而且**只能扩写 `quantities`**(她不是一样可以被 `tend` 的东西;
  她的能力在行为树和聊天工具里)。四条硬纪律:**`requires` 只准读 `me_*`**(不成立永远
  只有一个意思——「你做不了」;能读对象的量它就和 `when` 分不开,而分得开正是它存在的
  全部理由)、**`me_X` 必须声明过**(否则恒为 0,门要么永远开着要么永远关着)、
  **`costs` 与 `set` 读同一份旧值**(双缓冲,和规律那一层同一条)、**拒绝时一个字都不写**。
  拒绝理由因此是**三**类:`conditions`(这会儿不行 → 等)、`incapable`(她做不了 → 去歇着)、
  讲不通的那一摞。合成一个的话,一个累坏了的人会挨棵树轮着试过去,每一棵都回她"再等等"。
  她身上的量住 `stock:agent:{id}`,和树的量同一个后端、同一套可见性 —— 于是**声明成
  `self` 她自己感知得到、声明成 `here` 别人看得见**,零额外接线。两个落点各只有一个:
  量的播种在 `Scheduler.register`(角色不在 `ontology.entities` 里,而她可能在世界跑起来
  之后才出现——开机名册 / 节拍 `agent_join` / 重启中途加入的唯一共同窄口),她此刻在哪
  同步进可见性表在 tick 循环的 `_settle_actor_place`(`loc` 有五处写点,挨个加等于给未来
  的第六处留一个静默的洞)。**工具与材料**是同一条的续:`have_X` 读她随身带着几个 X
  (库存投影,只读不写;没带的读作 **0** 而不是"名字不存在"),`consumes: {id: n}` 花掉
  材料并发 `item_consume`。`consumes` **自带一道"你得有"的门**,不必再写一遍 `requires` ——
  写两遍就给了只写一遍的机会,而只写 `consumes` 的世界里她会用一包不存在的肥料把活干完
  (库存扣不到负数,连账上都看不出来)。东西的 id 在加载时查,拼错**开不了机**。
- **时间是第三种代价,而且是唯一封得住"生成"的那种**(`duration` / `occupies`,tick 为单位,
  0 = 一下子的事 = 默认):量和材料都能靠"睡一觉就回来"绕开,而一段时间过不去就是过不去。
  三条:**代价当场付、效果到点落**(付在收尾的话,起个头再放弃就是免费的);**关口只在
  起头查一次**(付了十个月再被一句"这会儿不行"拒掉,她没有任何办法预防,而预防不了的
  失败教不会她任何东西);**效果读收尾那一刻的值**,不是起头的快照。`occupies` 是**这件事
  的属性,不是她的状态** —— 做椅子占用她、怀胎不占用,两者都花十个月,而"这期间她还能不能
  干别的"才是代价的真实形状;占用的一次只能有一件,期间任何能力调用拒绝成**第四类**
  `busy`(她该等自己手上这件做完,既不是换一棵也不是去补足)。在做的长过程住
  `:engaged`(**真状态,不是缓存**:内存态等于每次重启都流产一次),收尾在 tick 帧里、
  **早于行为树**;收不了尾发 `entity_disengage` 而**代价不退**。顺带修掉一个真 bug:
  排班里 30 分钟窗口的 `interact` 会做 6 遍 —— 给它一个覆盖窗口的 `duration` 就只做一遍。
- **世界自己长得出新东西**(`spawn` / `destroys_target`,2.0):`entities` 从"创世时钉死的
  闭集"变成运行期可生可灭 —— 一个不能长出新东西的世界是个西洋镜,而"按规则铺开实体"
  正是"生成一个世界"这件事本身。**生成必须要代价,而代价由作者写,不是引擎发配额**:
  声明了 `spawn`/`destroys_target` 却没写 `costs`/`consumes`/`duration` 里任何一样,
  **开不了机**。理由是配额是**引擎的天花板**——撞上去时她收到的拒绝在世界里没有意义,
  她也永远学不会;代价是**世界的理由**,她知道自己为什么做不到、要做到得先补什么。
  ⚠️ 而**代价只封得住速率,封不住存量**(体力天天回满 → 一百天一百个孩子),所以
  **生灭同一轮加**:只有生的引擎会让每个世界最后都挤爆,而且漏得很慢、很安静。
  几条落点:生在**收尾那一刻**(否则十月怀胎只是一句话);`spawn.quantities` 只收常数
  (新生的东西身上还没有值可读);没写的量照声明**逐个量填**;id 由引擎发且**只增不减**
  (它进事件、进提示词、进 `.cyberworld`,复用死者的号等于让历史指向另一样东西);
  `destroys_target` 不许和 `set` 一起写(写到一个正要被抹掉的东西身上,引擎挑哪条都是猜),
  抹掉时**四样一起走**(实例 / 量 / 位置 / 挂在它身上的长过程)。
  **出生自检是出生的一部分**(`check_entity` / `World.check_entity` / `ontology --check`):
  运行期生出来的东西走的不是创世那条路,而创世那条路上的闸在这里一条都不在 ——
  不验的话,一个"量一个都没落地"的东西会安静地待在世界里,规律算不动、条件求不出值,
  作者三个月后才发现。判据是**算得出一个叫得出名字的结论**,不是"能成功"(`conditions`/
  `incapable` 都算过关,只有 `error` 不算);没过就整个撤回并发 `entity_stillborn`,
  **代价不退**(退了那个 bug 就从账面上也消失了)。
  **跨进程靠 `:entities_rev` 版本号**(不是行数——一生一灭净变化是 0),变了只重编译
  实例那一半;**种类仍然是冻的**,运行期新增种类会让"这条规律合不合法"随时间变化。
- **动词是作者的,不是引擎的**:闭集那十个词退成默认词表。原来的理由("效果终归由引擎
  实现")在 `set`/`costs`/`consumes` 落地之后就不成立了——`apply_affordance` 没有一处按动词
  分支。闸没松:自造动词照样声明一次、别处写错开不了机,**纯 ASCII 的必须给 `label`**
  (她读到的是那几个字,"端详、brew"里的 brew 是噪音)。**人话也调得动**,反查按种类做
  而不是全世界一张表(两个种类各有一个"照料"时,全局表只留得下一个)。

创世那一刻两条(都踩过):**默认值逐个量填,不是逐个实体填** —— 按实体跳的话,种子里给
某棵树写了一个 `树高` 就会让它声明过的其余量一个都不落地,于是 `tend` 的条件求不出值、
生长规律算不动,两件事都只是安静地不发生。**顺序是规律 → 本体 → 种子显式值 → 声明默认值**:
本体要拿规律去查引用;作者写的 3.2 必须先落,否则被声明的 1.0 盖掉。

外加一个第五个开关,补上"没人跟她说话时她也能主动"这一半:`autonomy.enabled`
(调度器 tick 上每隔 `autonomy.interval_ticks` 问一次她要不要做点什么,决定与执行
在世界自己那条循环上跑——时钟永不等网络;`World.autonomy_stats()` 报这条链通没通)。

HTTP 层于 2026-07 移除:网站/运维台若要对接,走 import(Python)或 CLI + `.cyberworld`
(非 Python);旧的 `/internal/v1` 协议与 membership claim 实现在 git 历史里
(commit `e7e3188` 之前)可考古。

多仓库拆分前的合并历史归档在 `/home/super/src/vibecoding/Anima正式版-history.git`(bare)。
