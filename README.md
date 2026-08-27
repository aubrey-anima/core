# anima-world

**多 agent 共同生活的世界引擎。** 以形式本体论为地基——声明什么存在、能对它做什么、
要付什么代价——让角色的每个决定从世界里长出来,而不是从提示词里编出来。角色会醒来、
会饿、会去上班、会背后议论别人、会抱团、会花钱、会记得上周发生的事、会改变对你的
看法;世界里的东西会生长,也会生灭。

[![License](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](https://github.com/aubrey-anima/core/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://github.com/aubrey-anima/core/blob/main/pyproject.toml)
[![PyPI](https://img.shields.io/pypi/v/anima-world.svg)](https://pypi.org/project/anima-world/)

---

## 这是什么

大多数"AI agent"框架给你的是一个带工具的聊天机器人。这个引擎给你的是一个**地方** ——
没人看着它也在跑,而且记得没人看着的时候发生过什么。

它不是多 agent **协作**框架(消息编排、任务分解是 AutoGen / CrewAI 那条赛道)。
这里的协作不是编排出来的,是从共享的世界里长出来的:同一棵树、同一个经济、
同一张关系图。

世界是 tick 驱动的模拟:每个 tick,每个角色拿自己的需求和世界此刻的状态跑一遍
行为树,然后行动。行动变成事件,进一条只追加的日志 —— 日志是世界唯一的真相,
余额、关系、位置全是它的**投影**,不存在第二份可能失配的账。

LLM 在模拟**旁边**,不在里面:它负责叙事、给角色安排空闲时间、判定一场对话如何
改变了关系,全跑在后台线程上。**时钟永远不等网络。** 把 LLM 整个拿掉,世界照样跑,
只是文本变成模板。

引擎是**一个库,不是一个服务**。没有 HTTP、没有端口:你的应用 import 它,
世界住在 Redis 上:

```
你的应用 ──import anima_world.api──▶ Redis 上的世界(anima:{world_id}:*)
```

## 30 秒看它跑起来

```console
$ pip install anima-world
$ anima-world start

  ANIMA 世界引擎
  ────────────────────────────────────────────

  ① LLM
     ! LLM 未配置 —— 叙事、空闲计划、关系判定都会降级成模板文本
       修复:anima-world config set llm.api_key sk-…

  ② 世界
     ✓ 新建 world(住在 redis://127.0.0.1:6379/0)
     时钟:1 tick/秒 —— 约 5 分钟走完一个世界日(现实时间的 300 倍速)
     ✓ 3 个角色就位: 苏晚夏、陆知遥、沈亦柔

  ③ 运行
     世界在本进程里运行,叙事会打印在下面;停止:Ctrl-C

  [第0天 00:10] 遥:遥四处走了走
  [第0天 00:10] 夏:夏睡下了
  ^C
  世界已停下。下次接着跑:anima-world start
```

`start` 依次做三件事 —— 引导配 LLM、创世、前台运行 —— 不用先读任何文档。
没有 API key 一切照常,只是叙事变模板、角色没有计划。这个降级是有意的,
而且**绝不无声**:`anima-world doctor` 会点名,`World.state()` 里常驻降级理由。

## 安装

```bash
pip install anima-world          # Python 3.11+;世界住 Redis,测试/试玩可用 fakeredis
```

运行时依赖三个,都是嵌入方无法回避的:`redis`(世界的家)、`openai`
(任何 OpenAI 兼容端点)、`httpx`。要 MySQL 的装 `anima-world[mysql]`。

从源码:

```bash
git clone https://github.com/aubrey-anima/core.git anima-world
cd anima-world && pip install -e ".[dev]" && python -m pytest
```

## 嵌进你的应用

这是主接口。`World` 是一个有生命周期的普通对象:打开、驱动、关闭。

```python
from anima_world.api import World

import redis

client = redis.Redis.from_url("redis://127.0.0.1:6379/0", decode_responses=True)
with World.open("world", redis=client) as world:
    world.start_clock()                        # 后台时钟;或者自己驱动
    print(world.state()["world_time"])         # {'day': 0, 'hour': 6, 'minute': 25, ...}

    # 替玩家和角色说话。流式;完整转录归你的应用管。
    for chunk in world.chat("夏", [{"role": "user", "content": "你好"}],
                            player_id="p1", display_name="阿宇"):
        print(chunk, end="")

    # 提交一轮结束的对话:摘要 + 一个世界事件 + 一次关系判定
    world.record_chat_turn("夏", "p1", [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好呀"},
    ])

    world.player_move("p1", "cafe")
    world.config_set("scheduler.tick_rate", 1.0)
```

批处理就自己驱动时钟 —— `world.tick(n)` 是同步且确定的,快进和测试都靠这一点:

```python
with World.open("world", redis=client) as world:
    world.tick(288)                     # 一个世界日
    print(world.memories("夏"))         # 她记得什么,按分排好
    print(world.needs("夏"))            # {'energy': 0.59, 'hunger': 0.16, ...}
    print(world.cliques())              # 谁和谁抱了团
```

## 一个世界里有什么

四个子系统,除记忆外全是默认关闭的开关。默认关是因为一个只会走动和说话的世界
也是正当的世界,而每开一个机制都是更多 LLM 开销、更多要想清楚的面。

| 子系统 | 开关 | 加了什么 |
|---|---|---|
| **记忆** | 常开 | 相关性 × 新近度 × 重要性的三因子检索、定期反思写出更高阶的记忆、遗忘曲线 |
| **需求** | `needs.enabled` | `energy` / `hunger` / `social` 逐 tick 衰减,驱动行为树的紧急带 —— 累了的角色会放下手里的事去睡觉 |
| **经济** | `economy.enabled` | 物品、钱、店铺、工资、价格漂移。账本是 `payment` 事件的投影,余额造不了假 |
| **社交** | `social.enabled` | 三轴关系(常开)+ 二手传播、置信度衰减的八卦 + 涌现的小团体 |

```bash
anima-world config set needs.enabled true --world-id world
```

### 她自己的选择(1.3.0,四个开关,默认关)

1.2 的聊天轮次里,角色只有一件事可做:**把句子接下去**。说"我走了"之后还站在原地,
话背后没有关系性的意图,也分不清你是在跟她说话还是在导演场景。这四个开关是缺的那一半:

| 开关 | 她多了什么 |
|---|---|
| `chat.stance.enabled` | 回话前先选一个**姿态** —— 讨好、讨坏、试探、回避、宣泄、挑逗、顺从、中性。按(角色,对方)存:她可以对你刺、对别人甜 |
| `chat.tools.enabled` | 聊天中途**调能力**:静音你、结束对话、"等会儿再说"(到点真的回来)、走开(一段真的路程,不是一句散文)、拒谈话题、广播 |
| `chat.intent.enabled` | 你的每条消息先分类:对话 / 导演场景(改的是世界,不是提示词)/ 改对话规则("叫我霜霜" —— 写进她对这个玩家的 persona,永久) |
| `chat.loop.enabled` | `world.chat_burst()` 一直生成到**她**自己想停:明确让位、一个反问、预算耗尽、或一个结束轮次的能力 |

能力调用走回复流里的行内标记(`〔tool:mute {"minutes": 5}〕`),不走 OpenAI 原生
`tools=`,因为默认状态必须能用:没 key 的世界跑在 Mock 上,而本地端点的 function
calling 支持参差。正文照样逐字流出,一个标记都不会漏给玩家。

她不理某个玩家时 `world.chat()` 抛 `AgentUnavailable` —— 空回复和 LLM 挂了
在界面上分不出来,而这两件事该有不同的 UI。

### 不是人的东西(2.0:本体层)

2.0 之前,世界里有角色、地点、能带走的物件,东西身上也有量 —— 但没有任何地方
说得清"一棵树"**是**什么,于是也说不清能对它**做**什么。声明一个**种类**,
两件事一次说清,所有实例共享:

世界写在一个 `.cyberworld` 文件里(gzip + JSONL,一行一条记录;不压缩也读得进来,
所以手写完全可行)。声明种类就是往里加几条 `author` 记录 —— 下面为了好读换了行,
真实文件里每条记录占一行:

```jsonc
{"kind":"author","type":"kind","body":{
    "id": "agent",
    "quantities": {
      "体力": {"default": 100, "visibility": "self", "unit": "点"},
      "手艺": {"default": 1.0, "visibility": "here"}
    }
}}
{"kind":"author","type":"kind","body":{
    "id": "tree",
    "gloss": "一棵树",
    "quantities": {
      "树高":   {"default": 1.0, "visibility": "here", "unit": "米"},
      "最大树高": 12.0
    },
    "affordances": {
      "look": {},
      "tend": {"when":     ["树高 < 最大树高"],
               "set":      {"树高": "min(树高 + 0.05 * me_手艺, 最大树高)"},
               "requires": ["me_体力 >= 15"],
               "costs":    {"体力": "me_体力 - 15"}}
    },
    "prompt": {"budget": 3}
}}
{"kind":"author","type":"entity","body":{
    "id": "tree:oak", "name": "门口那棵老橡树", "location": "yard"}}
```

声明住在种类上,实例只带 id、名字、在哪。这一刀买到的是**有界性**:提示词里
种类只出现一遍,`prompt.budget` 封顶她一次看得见几个 —— 3000 棵树的世界和
20 棵树的世界发给她一样多的字。截断了会明说("这里还有 2997 样东西你没细看");
静默截断会让她在一个"她以为只有三棵树"的世界里做决定,而且永远不会发现。

**声明本身就是开关。** 不写 `kinds` 的世界行为逐位如旧 —— 这一层整个缺席。
写下 `kinds` 就等于说"我已经声明了这里有什么",于是建议变成闸:量名拼错
(`树髙`)当场开不了机,报错里带着你声明过的那些名字。放行的话,它会安静地
建成第二个量,而真的那个停在默认值上,日志干净,直到几个月后你发现那棵树没长过。

**能力是施动者与对象之间的关系,不是对象单方面的属性。** 同一把斧头对有力气的人
是"能砍",对没力气的人不是。所以 `when` / `set`(关于树)有另一半:`requires` /
`costs`(关于她),通过 `me_` 前缀读她自己的量。没有这一半,每个动作都总能成功 ——
而**一个总能成功的动作产生不了任何决策**:没有理由挑先做哪件、没有理由休息、
没有理由变强。有了它,照料六次她力气见底,第七次回她 `incapable`,她得去做点别的。

**工具、材料、时间也是代价。** `have_剪刀 >= 1` 读她随身带着几把剪刀(库存投影;
没带的读作 0,不是报错 —— 从没拿过剪刀的人和刚放下剪刀的人,在"现在能不能修枝"
上没有区别)。`consumes` 花掉材料,自带一道"你得有"的门。`duration` 是唯一
睡不回来的代价:量会恢复、材料能再买,而一段时间过不去就是过不去 —— 十月怀胎
拦得住,不是因为它贵,是因为它长。

```jsonc
"打椅子": {"duration": 8640,          // tick;0(默认)= 一下子的事
          "occupies": false,          // 这期间她占不占用;默认 true
          "consumes": {"木料": 3}, "costs": {"体力": "me_体力 - 40"},
          "set": {"成色": "成色 + 1"}}
```

代价**当场付**,效果**到点落** —— 付在收尾的话,起个头再放弃就是免费的,而一个
可以随时反悔且不留痕的承诺不是承诺。关口**只在起头查**:付了十个月再被一句
"这会儿不行"拒掉,是她没有任何办法预防的失败,而预防不了的失败教不会她任何东西。
`occupies` 是这件事的属性,不是她的状态 —— 做椅子占用她,怀胎不占用,两者都要
十个月。被占用时任何能力调用拒绝成 `busy`:第四类理由,她该等手上这件做完,
既不是换一棵树,也不是去歇着。`world.engagements()` 报谁正在做什么。

**动词是作者的。** 声明 `酿` 或 `brew` 它就存在,别处拼错照样开不了机;
纯 ASCII 的动词必须给一行 `label`,因为她提示词里读到的就是那几个字,
"你可以对它:端详、brew"里的 brew 是噪音,而她还得照着它行动。

**世界自己长得出新东西,也抹得掉。** 2.0 之前 `entities` 是创世时钉死的闭集:
树不能被种下,杯子不能被打碎。一个不能长出新东西的世界是个西洋镜 ——
而"按规则铺开实体"正是"生成一个世界"这件事本身。

```jsonc
"育苗": {"duration": 24, "when": ["树高 >= 3"],
        "requires": ["me_体力 >= 20"], "consumes": {"肥料": 1},
        "costs": {"体力": "me_体力 - 20"},
        "spawn": {"kind": "sapling", "name": "新育的树苗"}},
"拔掉": {"costs": {"体力": "me_体力 - 5"}, "destroys_target": true}
```

**生成必须要代价,而代价由作者写。** 声明了 `spawn` / `destroys_target` 却没写
`costs` / `consumes` / `duration` 里任何一样,开不了机。引擎不发配额:配额是
**引擎的**天花板,撞上去时她收到的拒绝在世界里没有意义 —— "这个世界最多一百棵树"
不是她能理解、能应对的东西,她也永远学不会。代价是**世界的**理由:她知道自己
为什么做不到,也知道要做到得先补什么。但代价只封得住速率,封不住存量 ——
体力天天回满,一百天就是一百个孩子。真实世界靠生灭成对,所以这两样是一起发的。

**每一次出生都要过自检,没过的不算生。** 运行期生出来的东西不走创世那条路,
创世的闸对它一条都不生效。不验的话,一个新生的东西可以在 `entities` 里看着
好好的,量却一个都没落地 —— 条件对着 0 求值、规律算不动,两件事都安静地不发生,
直到几个月后有人发现那棵树没长过。所以引擎空跑一遍:量在不在、每条能力算不算
得出一个**叫得出名字的**结论、按声明的可见性在不在场。是"叫得出名字",不是
"成功" —— "还没熟"和"她做不了"都是世界在正常说话。没过的整个撤回,报
`entity_stillborn`,而**代价不退**:她确实付过了,退钱会把作者的 bug 从账面上
也抹掉。

问 CLI 这个世界声明了什么、里面的东西活不活得了:

```bash
anima-world ontology --world-id world --json    # 种类、量、动词、此刻的值
anima-world ontology --world-id world --check   # 逐个实体跑出生自检
```

**能力表只有这里问得到。** `stocks` 给得出数字,而数字不告诉你 `tend` 这个词
存不存在。

## 它是怎么搭的

**一个键前缀就是一个世界。** 世界住在 Redis 的 `anima:{world_id}:*` 下(事件、
聊天、记忆、配置、地图、她的黑板)。传 `mysql=` 则无限增长的四样(events /
memories / conversations / messages)搬去 MySQL。LLM 的钥匙住在这台机器上
(`~/.anima-world/config.json`,0600)—— 永不进世界,所以打包发出去的世界
在构造上就不带 secret。

**事件日志是唯一的真相。** 没有 `balances` 表,因为两份真相迟早不一致,而你分不出
哪份是对的。世界不存"夏有 50 块",存的是**她为什么**有 50 块。对账 = 重放。

**多进程,一个世界。** 时钟、黑板、她要带进提示词的一切都在 Redis 上,所以第二个
只有 Redis 连接的进程看得见、也改得动同一个世界,在一把跨进程锁下。中途加入永不
倒带:进门路上的每一笔写都是只填缺。

**版本即契约。** 一次发版把引擎代码、存储形状、包格式一起冻结;**世界钉死在生成
它的引擎版本上,不做跨版本迁移** —— 这是显著特性,不是脚注。`anima-world contract
--json` 自报存储契约与包格式版本,持有镜像实现的仓库拿它对账,而不是安静地掉队。

## 把世界发给别人

世界打成单个 `.cyberworld` 文件 —— **一个 gzip 的 JSONL,一行一条记录**:
清单、每个 Redis 键、每条事件各一行。它同时是**手写格式**、**流转格式**和**归档格式** ——
手写的世界只有 `author` 记录(装载时编译),跑过的世界导出来只有状态记录,
两者同一道门,因为创世和还原本来就是同一个动作。

```bash
anima-world world export --world-id world --output my.cyberworld \
    --package-id my-world --name "我的世界"

anima-world world import my.cyberworld --world-id restored   # 目标必须是空世界
```

包自己说清它需要什么,不需要你能跑它 —— 管着多个引擎版本的启动器正是那个
还跑不了它的调用方:

```bash
anima-world world inspect my.cyberworld --json
# {"world_id": "my-world", "engine_min": "2.0.0", …, "current_engine_version": "1.1.0",
#  "runnable": false}          # 照样回答,退出码 0 —— 在这里拒绝就违背了这个格式的意义
```

⚠️ **`inspect` 读的是封皮,不是判决**:它答的是"作者**声称**要哪个引擎"。
"这一版引擎**真的**收不收它"是另一条命令:

```bash
anima-world world check my.cyberworld --json
# {"loadable": true, "errors": [], …}     # 退出码 = 这句话我答没答上来,loadable 才是答案
```

⚠️ 而 `check` 自己也有一段够不着的:它查的是**作者层**。一个跑过的世界导出来
**只有状态记录**,它会照实说一句"没有作者层,所以这里没有可查的东西"——
**然后 `loadable` 仍然是 `true`**。所以**拿一份包换到别的引擎上去之前,真开一次机**;
判据是"该发生的发生了没有",不是"有没有报错"(老引擎不认识的状态层键是**装得进、
开得起来、安静地不生效**的)。

因为它是文本,排障不需要任何工具:

```bash
zcat my.cyberworld | head -3                           # 它是什么
zcat my.cyberworld | grep '"type": "entity_spawn"'     # 这个世界里生出过什么
diff <(zcat a.cyberworld) <(zcat b.cyberworld)         # 两次导出差在哪
```

⚠️ **冒号后那个空格是承重的**。记录用 `json.dumps` 的默认分隔符写出去,所以
`grep '"type":"entity_spawn"'`(这份文档在 3.6.0 之前一直是这么写的)**一条都匹配不到**;
而 grep 找不到时只是安静地退 1,和"这个世界确实没生出过东西"在屏幕上长得一模一样。

## 命令

分两拨:**人打的**和**部署/脚本打的**。分界是承重的 —— `start` 会引导、会替新世界
换成演示速度,`run` 一概不做。

```bash
# 人打的
anima-world start          # 引导配 LLM → 创世 → 前台运行 —— 从这里开始
anima-world play           # 在活着的世界里说话:时钟一边走,她可能自己走过来找你
anima-world chat           # 说一轮话就退(时钟不走);不带 --agent 列出谁住在这里
#                            -m/--message 可重复 = 一次一问,不进 REPL;--json 给脚本读
anima-world prompt         # 她收到的提示词,逐块带来源
anima-world map            # 地图、谁在哪、谁去了哪儿(--json)
anima-world ontology       # 有哪些种类的东西、量与动词(--json / --check)
anima-world roster         # 这个世界里有谁:名字、一句话、立绘、主次、此刻在哪(--json)
anima-world doctor         # 体检:Redis 持久化、密钥、真调一次 LLM、时钟
anima-world config         # 读写配置;api key 自动进机器配置,不进世界

# 部署 / 脚本打的
anima-world run            # 只把时钟跑在前台:不引导、不问、不改时钟
anima-world simulate       # 无头快进(--report 落一份运行摘要;--ticks 0 = 只创世)
anima-world world          # export / import / inspect / check / setting / migrate / drop
#                            check = 这一版引擎装不装得进这份包(退出码 = 答没答上来)
#                            setting = 读/改一个**跑着的世界**的世界观(不给开关就是只读)
anima-world validate       # 不建世界就查一份 .cyberworld 或一份节拍脚本
anima-world contract       # 引擎自报存储契约与包格式版本 —— 持镜像的仓库拿它对账
anima-world report         # 只读地出一份运行摘要,不跑世界
anima-world events         # 事件流导出成 JSONL
anima-world contact        # 谁想起过玩家、由头是什么(--why 连没触发的;--inbox 看敲门)
anima-world relationship   # 一段关系此刻的人话:当什么、哪一档、上一次是什么改变了它
anima-world player         # 玩家那一侧:options 点得动什么;forget 告别;erase 法务抹除
anima-world drift          # 她还是不是她:人设漂移与迎合度(纯计数;漂了退 1,可进 CI)
anima-world engagement     # 他跟这个世界处得有多深(依赖预警要的那笔账)
anima-world presence       # 谁在谁跟前(开同处一地那道闸之前的体检)
anima-world agent          # 改一个跑着的世界里某个人的卡(set-card);老 goals 的修补
anima-world location       # 改一个跑着的世界里某个地点的两格图(set-image)
anima-world memory         # 老世界的数据修补(盖了墙钟的记忆 tick)
```

## 换一个模型

LLM 客户端是注入的,所以换模型不用改引擎 —— `config set llm.api_key` / `llm.base_url`
指到哪儿就是哪儿(OpenAI 兼容端点即可)。

**自托管想要更稳的角色扮演**,可以看一眼 [CoSER](https://github.com/Neph0s/CoSER)
(MIT,LLaMA-3.1 底,8B / 70B 两档):它拿 771 本书里 17,966 个角色训过,评测上
追平 GPT-4o 那一档。许可和本项目(AGPL-3.0-or-later)兼容。

要判断换了到底有没有变好,别靠感觉 —— 世界里就有尺子:

```bash
anima-world drift --world-id w --agent 夏 --json    # 七个文风特征 + 迎合度,漂了退 1
```

它是纯计数、不调模型的,所以同一段转录跑一百遍给同一个答案。**它测的是文风不是人格**,
当报警器用,别当结论(见 docs/REFERENCE.md §2.9.12)。

## 文档

| | |
|---|---|
| [docs/REFERENCE.md](https://github.com/aubrey-anima/core/blob/main/docs/REFERENCE.md) | 逐命令、逐 `World` 方法、逐配置键、节拍脚本格式 |
| [docs/ARCHITECTURE.md](https://github.com/aubrey-anima/core/blob/main/docs/ARCHITECTURE.md) | 为什么长这样:真相模型、tick 帧、线程与锁、不变量、已知债务 |
| [CONTRIBUTING.md](https://github.com/aubrey-anima/core/blob/main/CONTRIBUTING.md) | 开发环境、补丁不许破坏的不变量、怎么提变更 |
| [CHANGELOG.md](https://github.com/aubrey-anima/core/blob/main/CHANGELOG.md) | 发版历史 |

## 参与

欢迎 issue 和 PR。先读 [CONTRIBUTING.md](https://github.com/aubrey-anima/core/blob/main/CONTRIBUTING.md) ——
里面列了改动不许破坏的那几条不变量(系统里只有一把锁、LLM 永不在 tick 线程上被
调用、几个文件格式被别的仓库镜像着)。

## 许可

[GNU AGPL-3.0](https://github.com/aubrey-anima/core/blob/main/LICENSE)。**用了它,
你的东西也得开源** —— 包括把它跑成网络服务:AGPL 与 GPL 的差别正在这一条,
世界引擎最常见的用法是被服务包着,GPL 管不到那种用法,AGPL 管得到。
修改与衍生同样必须以 AGPL 开源。

⚠️ **一句要说清的:直到 1.4.0,PyPI 上发出的每一个版本都是 Apache-2.0** ——
换许可是 2.0 那一版做的,而 **2.0.0 到 3.6.0 一版都没上过索引**(理由见
[CHANGELOG](https://github.com/aubrey-anima/core/blob/main/CHANGELOG.md) 的 3.3.0 那一节)。
已发出的那些维持原许可;**本仓库自 2.0 起是 AGPL-3.0-or-later**,而 **3.7.0 是照它发的
第一版**(2026-08-26)。

⚠️ **所以从 1.4.0 升到 3.7.0,变的是两件事,而只有一件会报错。**

- **会报错的那件**:跨了两个主版本,而主版本在这个项目里就是**可挂载性** ——
  1.x 的世界是一个 SQLite 文件(`World.open("saves/world.db")`),3.x 的世界是 Redis 上的
  一个键前缀(`World.open("world", redis=…)`),CLI 的 `--db-path` 也换成了
  `--redis` + `--world-id`。照 1.x 写的代码升上来**当场 TypeError**,老世界文件**读不了**,
  而且没有跨版本迁移 —— 这是显著特性,不是脚注。
- **不会报错的那件**:**许可从 Apache-2.0 变成 AGPL-3.0-or-later**。`pip install -U` 不会
  为此说一个字。如果你把这个引擎包在一个网络服务里对外提供,AGPL 第 13 条管得到那种用法,
  而 Apache-2.0 管不到 —— **这一格的变化只有你自己看得见,所以写在这里。**
  想留在 Apache-2.0 上,就钉住 `anima-world==1.4.0`。
