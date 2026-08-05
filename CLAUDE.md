# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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
| `anima_world/world_package.py` | `.cyberworld` 数据包格式 | 运维台 `lib/worldPackage.js` |
| `anima_world/world_seed.py` | 种子 schema 校验 | 运维台 `lib/worldSeed.js` |
| `anima_world/beats.py` | 节拍脚本严格校验 | (无镜像;创作台经 CLI 委托校验) |
| `contract --json` 的 `storage` 段 | 存储契约:Redis 键前缀 / MySQL 表与表前缀(db.* 段随 world.db 退役) | 运维台镜像要改读 `storage.*` |

`docs/FOR-STUDIO.md` 是给创作台的能力说明 —— **它那侧的判据是"有没有 CLI 出口"**
(库里有而 CLI 上没有,对它等于不存在)。教训是流程的:创作台的
`docs/引擎接口诉求-试炼与试聊.md` 提了两条,我早就交付了却没回执,他们的 P1
白等了几天。**加了 CLI 出口就去那份文档里记一笔。**

数据包与种子两项由运维台的 `test/contract.test.js` 与本包**双向互验**(引擎不可用时整体 skip)。
节拍脚本的严格校验有个硬要求:**坏脚本必须在加载时当场报错,不能流到世界启动**。
**显式指定的种子同规矩**(`WorldSeedError`):种子只读进空库一次,静默降级成内置演示世界
不可挽回;只有内置种子才降级(装坏了也得能开机)。

Python 侧的对外接口是 `anima_world/api.py` 的 `World` 门面(加上 CLI)。它是宿主应用
依赖的 API 面:**只加不改**,破坏性变更等于跨仓库破坏。
`tests/test_reference_docs.py` 是这条纪律的闸门:**加公开方法就必须写进
`docs/REFERENCE.md`**(或进 `UNDOCUMENTED_ON_PURPOSE` 并说明理由),文档写的形参名
必须和真实签名对得上。REFERENCE 是宿主照着写代码的那份东西,而它和代码之间原本没有
任何机械联系 —— 一次人工对账就查出四处不实。

## 常用命令

```bash
pip install -e ".[dev]"

python3.13 -m pytest -q               # 687 项(fakeredis,无需真 Redis);addopts 已屏蔽 ROS 插件
python -m build                       # → dist/*.whl + dist/*.tar.gz
python -m twine upload dist/*         # 发布

# 世界住在 Redis:每个命令都吃 --redis URL(默认 $ANIMA_REDIS_URL 或
# redis://127.0.0.1:6379/0)与 --world-id(默认 $ANIMA_WORLD_ID 或 world);
# 可选 --mysql DSN 让无限增长的四样进 MySQL。

# 给人用的三个命令
anima-world start                     # 引导配 LLM → 创世 → 前台运行;新世界用演示速度
anima-world doctor                    # 体检:Redis 持久化、密钥、真调一次 LLM、时钟翻译成人话
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

# 给部署/脚本用的
anima-world run --world-id w                          # 前台宿主,Ctrl-C 停
anima-world simulate --world-id w --ticks 288         # 无头快进;--ticks 0 = 只创世
anima-world world export --world-id w --output my.cyberworld \
    --package-id my-world --name "我的世界"
anima-world world import my.cyberworld --world-id w2  # 目标必须是空世界
```

嵌入到应用里(主要用法):`from anima_world.api import World` →
`World.open(world_id, redis=redis.Redis.from_url(url, decode_responses=True), mysql=None)`。
**`decode_responses=True` 是契约的一部分**:裸 bytes 客户端下量名变成 `b'树高'`,
规律静默失配。

## 关键不变量

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
- `world_seed.json` 是本包**唯一的 package data**,随 wheel 分发
  (`tests/test_packaging.py` 盯着,漏了会让宿主环境里少文件)。
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

**2.0 改造完成(未发版):world.db 整体退役,世界只住 Redis(+可选 MySQL)。**
PyPI 上已发布到 1.3.0(tag `v1.3.0`);`__version__` 还停在 1.4.0,发版前要定版本号
(按硬钉版纪律这是主版本级变更 —— World.open 签名、包格式、CLI 参数全破)。

这一轮删掉的:`db.py`、`small_stores.py`、`graph.py`、全部 SQLite store 实现、
Fernet/keyfile、db 格式联锁、`--db-path`。补上的:`RedisChatStore`(无 MySQL 时
转录的家)、`RedisConfigBackend`(`:config`)、`RedisRulesStore`(`:world_rules`)、
`meta_rows`(`:meta`:创世出生证明、占用标记)、RedisMemoryStore 的容量淘汰与
锚定不衰减(删的时候测试逮出来两处 Redis 版行为缺口)。`.cyberworld` 升 v2:
`world.db` 成员换成 `world_state.json`(Redis 键的类型化 dump + 可选 MySQL 段),
template 模式删除,导入只进空 world_id。创世 = 种子直写各 Redis store,空 store
才播;测试套件全量跑在 fakeredis 上(687 项,无需真 Redis 服务)。

同一轮加进来的是**本体层**(`ontology.py` / `RedisOntologyStore` / `anima-world ontology`
/ `World.kinds()` / `World.entities()` / `StockCondition` BT 叶子),纪律见上。
三份文档已跟着 2.0 对过一遍(REFERENCE 的 world.db / Fernet / template / db 格式联锁
描述全清掉了,§8 新增一张**键清单**当名字的权威;FOR-STUDIO 记了 `ontology` 这一笔)。

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
提示词引种类而不重复它。**这是有界性的来源**。五条:

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
  然后永远沉默。
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
  的第六处留一个静默的洞)。

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
