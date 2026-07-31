# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 这个仓库是什么

**anima-world —— ANIMA 世界引擎**,可发布的 pip 包。**只做引擎**:跑世界、快进、打包成
`.cyberworld`。创作已拆成独立的桌面程序 `anima-studio`(`../tool`),原因见下。

**引擎是纯库,没有 HTTP。** 任何要用世界的模块 import 本包,通过 `anima_world.api.World`
的函数操作 `world.db`;世界活在调用方进程里。一个 `world.db` 就是一个世界。

`README.md` 是本仓库最准的文档,`docs/REFERENCE.md` 是逐函数/逐命令的参考,改架构前先读。

## 在 ANIMA 系统里的位置

ANIMA 是多个**互相独立**的仓库。协作方式:**Python 宿主 import 本包**(pip 钉版),
非 Python 端与跨版本场景走 **CLI 子进程 + `.cyberworld` 文件**:

```
任何 Python 宿主(如网站后端)──import anima_world.api──▶ world.db(世界在宿主进程里活)
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
| `anima_world/db.py` 的 `DB_FORMAT_VERSION` / `SCHEMA_REVISION` | 世界文件的可挂载性与加法修订 | 运维台读 `contract --json` 的 `db.*` |

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

python3.13 -m pytest -q               # 464 项;pyproject 的 addopts 已屏蔽 ROS 的 pytest 插件
python -m build                       # → dist/*.whl + dist/*.tar.gz
python -m twine upload dist/*         # 发布

# 给人用的三个命令(onboarding.py + __main__.py 的 run_start/run_config/run_doctor)
anima-world start                     # 引导配 LLM → 建世界 → 前台运行;新世界用演示速度
anima-world doctor                    # 体检:密钥文件、db 格式、真调一次 LLM、时钟翻译成人话
anima-world config set llm.api_key sk-…   # 改配置不用写代码(按声明类型强转后再写)

# 给改提示词的人用的(run_prompt / World.debug_prompt)
anima-world prompt --db-path w.db --agent 夏   # 她收到的提示词,逐块带来源 + 少了哪块为什么
# 提示词是这套东西最不可见又最容易出错的一层(1.3 四个 bug 有三个在这儿)。
# 它和真聊天共用 `ChatService.prompt_blocks` —— **调试视图另写一遍拼装就会撒谎**。

# 给部署/脚本用的
anima-world run --db-path saves/world.db               # 前台宿主,Ctrl-C 停
anima-world simulate --db-path w.db --ticks 288        # 无头快进
anima-world world export --seed seed.json --output my.cyberworld \
    --world-id my-world --name "我的世界" --mode template
anima-world world import my.cyberworld --destination ./instances
```

嵌入到应用里(主要用法):`from anima_world.api import World` → `World.open(db_path)`。

## 关键不变量

- **`start` 是人的门,`run`/`World.open` 是程序的门**:`start` 会引导配置 LLM、给新世界
  换成演示速度(1 tick/秒);`run` 和 `World.open` 一概不做。改 onboarding 时别把这
  两条路径搅在一起。
- **一个运行中的世界独占它的 world.db**:世界的真相一半在内存(时钟/投影/锁/线程池),
  第二个进程绕过 `World` 直接写同一个 db 会立刻分叉。离线处置(打包/快进)在世界关闭
  后进行。**一个进程一个引擎版本;信任边界是进程边界**(`api.py` docstring 的三条纪律)。
- **`world.db.key`(Fernet 密钥)必须随 db 搬迁** —— 丢了 `llm.api_key` 就解不开,全线降级 Mock。
  降级本身是设计(世界照跑),但**不许无声**:`ConfigStore.undecryptable_secrets()` 区分"没配过"
  与"读不出来",`build_serve_scheduler` 开机点名,`World.state()` 的
  `runtime.llm.degraded_reason` 常驻(见 `tests/test_startup_diagnostics.py`)。
- **scheduler 持有系统唯一的 RLock**(世界时钟 / 邮箱);别再引入第二把锁。
- **聊天子系统与事件核解耦**:整场会话只在关闭时发一个事件。1.3.0 的 chat-agent 逐轮
  观测量(stance / intent / tool_call)因此**落在 `messages` 行上**,不是每轮一个事件 ——
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
- **版本即契约(硬钉版模型)**:一个 core 版本 = (引擎代码, db format 版本, 包格式版本)
  一起冻结。宿主装上某个版本后就只依赖该版本,不做跨版本迁移。
  - `anima_world/__init__.py` 的 `__version__` 是**唯一版本源**(pyproject 动态读取)
  - **主版本号 = db 格式 = 可挂载性**:让老引擎**读不了**世界文件的改动(改列义/拆表/
    换单位)才升第一位;第二位加能力,第三位纯修 bug
  - **加法修订 `SCHEMA_REVISION`(1.3.0 = 3)**:纯加法的 schema 变化(新表、新的可空列)
    不改可挂载性,跟着**次版本号**走,戳在 `db_meta.schema_revision`,**只增不减**。
    1.3.0 破的是"schema 一变就升主版本"那条 —— 那会把已有的世界为一批加法全作废;守的
    是"版本号能告诉你两个文件互不互通"这条,后者才是联锁真正在保护的东西。
    加表时问自己一句:**老引擎打开这个文件还能跑吗?** 能 → 加法修订 + 次版本;
    不能 → 那是 db 格式变更,升主版本。`tests/test_version_contract.py` 里
    "更高修订的世界仍然能挂"那条测试就是这道闸。
    1.3.0 内部走过 2 → 3(第一批 chat-agent,第二批 world-rules 与认知层)——
    一个版本号对应两个修订只因为这一版从没发布过,别当成先例。**闸在
    `SCHEMA_TABLES_AT_REVISION_3`**:表集合被钉住,加一张表就必须动那一行,
    于是"顺手加表却忘了升戳"不可能再悄悄发生(它就这么发生过一次,全量测试没红)。
  - 这个戳存在的唯一理由是**让降级看得见**(1.3 的世界跑在 1.2 引擎上照跑,但 stance /
    静音 / 拒谈话题整套缺席);`contract --json` 与 `doctor` 都报它,**运维台镜像要同步读**
  - `db.py` 的 `DB_FORMAT_VERSION` / `MIN_SUPPORTED_DB_FORMAT` 是运行期安全联锁:两者相等即
    "硬不兼容",挂错卷会当场拒绝而不是静默写坏(见 `tests/test_db_format.py`)
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
- **本包无 HTTP、无 HTML、无创作代码**。曾经的 FastAPI web 层(`anima_world/world/`,
  三组 REST API + membership claim 鉴权)已在纯库化改造中整体移除 —— 需要网络暴露的话,
  由宿主应用自己包一层,不归引擎管。`anima_world/author/` 也已删除,
  `import anima_world.author` 必须 ModuleNotFoundError(`tests/test_packaging.py` 守着)——
  留个 shim 就等于给工具开后门。

## 当前状态

**1.3.0(db 格式 1,schema 加法修订 3)。** PyPI 上已发布到 1.1.1;1.2.0 与 1.3.0 尚未
推 tag。版本规则由 `tests/test_version_contract.py` 机器强制。原路线图(docs/ROADMAP.md)的
2.0–5.0 四大机制已并入首发,全部带默认关闭的开关:

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

外加一个第五个开关,补上"没人跟她说话时她也能主动"这一半:`autonomy.enabled`
(调度器 tick 上每隔 `autonomy.interval_ticks` 问一次她要不要做点什么,决定与执行
在世界自己那条循环上跑——时钟永不等网络;`World.autonomy_stats()` 报这条链通没通)。

HTTP 层于 2026-07 移除:网站/运维台若要对接,走 import(Python)或 CLI + `.cyberworld`
(非 Python);旧的 `/internal/v1` 协议与 membership claim 实现在 git 历史里
(commit `e7e3188` 之前)可考古。

多仓库拆分前的合并历史归档在 `/home/super/src/vibecoding/Anima正式版-history.git`(bare)。
