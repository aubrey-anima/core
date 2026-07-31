# 给创作台的引擎能力说明(anima-world → anima-studio)

| | |
|---|---|
| 发出方 | anima-world(世界引擎),1.3.0 |
| 面向 | anima-studio(创作工作台) |
| 日期 | 2026-07-31 |
| 用途 | **让工作台知道现在够得着什么** —— 然后照着提 issue |

这份文档回答一个问题:**站在工作台那一侧,这个引擎现在能做什么。**

工作台从不 import 引擎,所有操作都是子进程 `cores/<版本>/venv/bin/anima-world …`
(`CoreRegistry.run()` 是唯一通道)。你们那份《引擎接口诉求》里那句话我认:

> **引擎库里已经有的能力,只要没有 CLI 出口,对工作台就等于不存在。**

所以下面每一条都标了**有没有 CLI 出口**。没有的那些,就是该提 issue 的地方 ——
别绕过去自己读 `world.db`,那等于在工具里养一份会过期的第二实现。

---

## 0. 先说三件你们现在最该知道的事

### ① 你们卡着的两条诉求,早就交付了

`docs/引擎接口诉求-试炼与试聊.md` 的状态还写着"待引擎评估",而 **诉求 A 和 B 都已经
在 CLI 上了**。你们的 P1(C3 三日试炼 / C5 一键试玩 / B3 性格试镜)不必再等:

```bash
anima-world report --db-path w.db --json      # 诉求 A:结构化运行摘要
anima-world simulate --db-path w.db --ticks 864 --report r.json   # 或者跑完直接落文件
anima-world chat --db-path w.db --agent 夏 --name 作者             # 诉求 B:本地试聊
```

这是我这边的流程问题:交付了没有回执。这份文档就是那个回执,以后新能力我在
CHANGELOG 之外单独在这里记一笔。

### ② 但 `report` 少答了你们五个问题里的一个

你们列的五个问题,现在答上四个:

| 你们要的 | 现在 | 在哪 |
|---|---|---|
| 每个世界日多少事件、什么类型 | ✅ | `events.by_day[].buckets` / `events.by_type` |
| 每对居民同处一地多少次 / 多久 | ✅ | `encounters[]` |
| 关系起点 → 终点 | ✅ | `relationships[]` |
| **哪些节拍触发了、哪些一直没触发** | ❌ **没有** | —— |
| 每个居民的一天怎么过 | ✅ | `agents[].share_by_activity` |

第四条是你们理由写得最硬的一条("一拍都没响 = 这个脚本白写,作者必须当场知道"),
而它恰好是唯一没做的。**请提 issue**,我按你们的口径加(`beat_fired` 事件是现成的,
缺的是"声明了几拍、响了几拍、哪几拍没响"这个差集)。

### ③ `chat` 只有 REPL,没有一次一问

你们要的是:

```bash
anima-world chat --db-path w.db --agent 昀 --message "十九年前那份报告是谁签的字?"
```

现在的 `chat` **没有 `--message`**,只有交互式 REPL。子进程驱动 REPL 要喂 stdin、
要判断哪一行是回复,比一次一问脆得多。**这条也请提 issue** —— 一个 `--message`
参数 + `--json` 输出就够了。

---

## 1. 现在 CLI 上有什么(13 个子命令)

```
start  config  doctor  chat  prompt  run  simulate  events  report  validate  play  contract  world
```

按你们用得上的顺序:

| 命令 | 干什么 | 对工作台的意义 |
|---|---|---|
| `contract --json` | **报这一版的全部契约数字** | 装完一个 core 先问它,别猜 |
| `validate` | 校验种子 / 节拍脚本 | 权威校验永远问选中的那个 core(你们已经这么做了) |
| `simulate --ticks N --report f.json` | 无头快进 + 落一份摘要 | C3 三日试炼 |
| `report --json` | 读一个跑过的世界出摘要 | 同上,事后再问一次 |
| `chat --agent X` | 本地试聊(免 claim) | B3 性格试镜 |
| `prompt --agent X` | **看她收到的提示词,逐块带来源** | 1.3.0 新增,见 §3 |
| `world export/import` | `.cyberworld` 打包 | 出厂 |
| `events export` | 事件流 JSONL | 连续性通路(只导出,不重放) |
| `doctor` | 体检 | 装完 core 自检 |
| `play` | 交互式跑 | 人手动玩 |
| `run` / `start` | 前台宿主 / 引导 | 工作台一般不用 |

### `contract --json` 是你们最该依赖的东西

装完一个 core、切换 core、出包之前,问它一次。它报的每个数都是**这一版**的答案:

```jsonc
{
  "engine_version": "1.3.0",
  "db": { "format_version": 1, "min_supported": 1, "schema_revision": 3 },
  "package": { "format_version": 1 },
  "report":  { "format_version": 2, "buckets": [...] },
  "seed":    { "agent_keys": [...], "location_keys": [...] },
  "beats":   { "ops": [...], "op_required_fields": {...},
               "predicates": [...], "predicate_required_fields": {...} },
  "chat_tools": [ { "id": "walk_away", "kind": "walk_away",
                    "params": ["to_location"], "surfaces": ["chat"] }, ... ]
}
```

两个提醒:

- **`db.schema_revision` 是 1.3.0 新加的**(见 §2)。它和 `format_version` 不是一回事:
  format 变了 = 老引擎**打不开**;revision 变了 = 老引擎**打得开但少东西**。
  你们按版本归档世界目录的做法很对,再把 revision 也记进 `core.json` 会更有用。
- **`chat_tools` 名字是历史包袱,它不只有 chat 面**。`reach_out` 只在定时轮次里出现,
  聊天里永远调不到。**按每条的 `surfaces` 过滤**,别照着字段名做一个"聊天能力"列表 ——
  那会给作者一个永远等不到的按钮。(`surfaces` 是 1.3.0 加的,纯加法。)

---

## 2. 版本规则变了:主版本 = 可挂载性

这条直接影响你们的核心设计(多版本并存 + 按版本归档),所以单独说。

**旧规则**:schema 一变就升 db format,db format 一升就升主版本。
**新规则**(1.3.0 起):

- **主版本 = db format = 可挂载性。** 只有让老引擎**读不了**世界文件的改动才升第一位。
- **纯加法的 schema 变化**(新表、新的可空列)**跟着次版本走**,戳在
  `db_meta.schema_revision`,只增不减。

为什么改:1.3.0 加了四张表,按字面旧规则该叫 2.0.0,而那会把所有 1.x 的世界为一批
加法全作废。旧规则真正保护的是"版本号能告诉你两个世界文件互不互通",而加法不影响互通。

**对工作台的实际影响**:

- 一个 1.3.0 的世界跑在 1.2 引擎上**照样能开** —— 但 stance、静音、拒谈话题、
  世界规律、认知层整套缺席。**这就是 revision 存在的唯一理由:让这种降级看得见。**
- 所以"能不能开"和"开了之后完不完整"是两个问题。你们的 `core.json` 现在记了
  db format 和 min_supported,建议**再记一个 schema_revision**,并在"用 A 版本生成、
  想用 B 版本打开"时拿它提示作者。

---

## 3. 1.3.0 新增了什么(五层,全部默认关闭)

每层都标了 CLI 可达性。**"❌ 只有 Python API"= 对你们等于不存在,是提 issue 的地方。**

### 3.1 她自己的选择(chat-agent,四个开关)

| 开关 | 是什么 |
|---|---|
| `chat.stance.enabled` | 关系性意图:八枚举(讨好/讨坏/试探/回避/宣泄/挑逗/顺从/中性),按 (角色,对方) 存 |
| `chat.tools.enabled` | 聊天里的能力:静音 / 结束对话 / 等会儿再说 / 走开 / 拒谈话题 / 广播 / 让位 |
| `chat.intent.enabled` | 意图分派:对话 / 导演场景 / 改对话规则 |
| `chat.loop.enabled` | 连续输出:一次触发说到她自己想停 |

**她的选择在世界里真的兑现**:`walk_away` 真的起程,`delay_reply` 到点真的回来敲门,
`narrative_direction` 真的把人挪过来。不是只改提示词。

**CLI 可达性**:✅ 开关走 `config set`,效果在 `chat` 里能试出来,能力清单在
`contract --json` 里。⚠️ 但**逐轮的观测量(她这一轮选了什么 stance、调了什么能力)
落在 `messages` 表行上,CLI 上读不到**。B3 性格试镜要看"她刚才是不是在赌气",
现在只能靠读回复文本猜。**这条可以提 issue。**

### 3.2 世界的规律成为数据(world-rules)

设计者写规律,引擎执行:

```jsonc
{ "id": "tree_growth",
  "every": {"days": 1},
  "for_each": {"kind": "tree"},
  "when": ["world_季节 != 4"],
  "set": {"树高": "min(树高 + 生长速度 * dt, 最大树高)"},
  "emit": [{"when": "树高 >= 最大树高", "type": "tree_matured"}] }
```

- 量 = `(owner, key, value)`,owner 前缀即种类(`tree:oak_01` / `agent:夏` / `world`)
- **绝不 `eval`**:AST 白名单 + 自写解释器,属性访问/下标/lambda 在解析时就拒
- **连续变化不发事件**,只有 `emit` 的门槛跨过去才发,且边沿触发
- **双缓冲**:同一轮读上一轮的值,规律之间与顺序无关
- **加载时严格**:一条写错整体拒绝(规律是物理法则,少一条不是少一点内容,是从此算错)

⚠️ **一条规律只写它自己那个 owner 的量。** 写 `world_x` 或 `mine:north.储量` 会在
**加载时被拒**(此前是静默写错地方)。跨实体相互作用(挖矿让矿脉减少)v1 表达不了。

**种子里怎么写**:

```jsonc
"stocks": [{"owner": "tree:harbor_oak", "values": {"树高": 3.2, "生长速度": 0.004, "最大树高": 12}}],
"rules":  [ … 上面那条 … ]
```

**CLI 可达性**:✅ 种子写进去、`validate` 校验、`simulate` 跑。
❌ **没有任何 CLI 能读一个世界当前的量或规律**(`World.stock/stocks/rules/rule_stats`
只有 Python)。你们做"世界工坊"要给作者看"树现在多高了",现在够不着。**请提 issue。**
❌ **也没有 CLI 能改一条规律** —— 规律只能在创世时从种子进,改一条要重建世界。
(这一条我自己也认为是洞,已经排进 1.4.0。)

### 3.3 认知层(perception):客观存在 ≠ 她知道

世界的量里她感知得到哪些,四档,**没声明 = 感知不到**:

| 档 | 意思 |
|---|---|
| `self` | 只有主人自己知道(她的功力) |
| `here` | 得在同一个地方(这棵树多高) |
| `public` | 人人皆知(季节、粮价) |
| `hidden` | 谁也不知道(**默认**) |

默认定死成"感知不到",因为反过来的错不可挽回:一个"暗中的恨意"若默认公开,
角色下一句就说出来了。**声明本身就是开关**,没有 `perception.enabled`。

种子里:

```jsonc
"stock_visibility": [{"kind": "tree", "key": "树高", "visible": "here"}],
"stock_places":     [{"owner": "tree:x", "location": "cafe", "label": "门口那棵老橡树"}]
```

`label` 是给角色看的名字 —— 提示词里"这里的老橡树"比"这里的 tree:x"像人话。

真模型实测:她把 `树高 9.4` 说成"目测得有九米多快十米了吧",问她无关的事时一个数字
都没提。

⚠️ `hidden` 目前是**绝对不可知** —— "有人告诉她"那条路还没接。

**CLI 可达性**:✅ 种子写、`prompt --agent X` 能**看见**感知块进没进提示词。
❌ `World.perception()` / `visibility_rules()` 没有 CLI 出口。

### 3.4 定时轮次(autonomy):没人跟她说话时她也能主动

`autonomy.enabled`。调度器每隔 `autonomy.interval_ticks`(默认 72 tick = 6 世界小时)
问一次在场的角色"此刻想做点什么吗",她可以什么都不做。`autonomy.max_per_day` 是硬上限,
**被问不算用掉额度,只有真的做了才算**。

⚠️ **`simulate` 不跑定时轮次** —— 它挂在 `World` 上,而快进直接建 scheduler。这是
有意的(快进一年 = 上千次网络往返),开关开着时 `simulate` 会打一行说明。
**所以 C3 三日试炼里不会有她主动做的事**;要看那个得用 `run`。

真模型实测行动率约 1/15 轮,默认节奏下**大约三四天主动一次** —— 偏稀。
`autonomy.decide` 是热改模板,世界作者可以自己调。

**CLI 可达性**:✅ 开关。❌ `World.autonomy_stats()`(报这条链通没通)没有 CLI 出口。

### 3.5 提示词第一次能被看见(`anima-world prompt`)

**这一条你们可能最用得上,而且我猜你们还不知道它存在。**

```bash
anima-world prompt --db-path w.db --agent 夏 --name 作者     # 摘要
anima-world prompt --db-path w.db --agent 夏 --full          # 连正文
anima-world prompt --db-path w.db --agent 夏 --json          # 给程序
```

```
苏晚夏 此刻收到的提示词:8 块 / 2256 字
  world.setting    329 字  14%  【世界】
  persona          305 字  13%  你是苏晚夏。开朗热情…
  memories          89 字   3%  你最近记得的事：
  presence          38 字   1%  现在是第 1 天 00:00。你在家…
  perception        77 字   3%  【你此刻感觉到的】
  identity         233 字  10%  【认证对话身份｜最高优先级事实】…
  stance           552 字  24%  【关系性意图(stance)…】
  tools            619 字  27%  【你可以做的事,不只是说话】

没出现的块,以及为什么:
  relation       她和这个玩家之间还没有关系行(说过话之后才有)
  overrides      这个玩家还没教过她对话规则
```

三条保证:

1. **它不撒谎** —— 和真聊天共用同一个拼装函数,有测试拿真提示词逐字比对。
2. **它解释缺席** —— "少了哪块、为什么",照着那句话就能让它出现。
3. **看,但不碰** —— 不推时钟、不进 LLM、不写玩家状态。

为什么对你们有用:你们做的是"把小说变成世界",而世界好不好玩最后落在**她收到了什么**
上。人物卡写得再好,如果那段人设在提示词里只占 13%、后面压着两块更响的规则,她就
不是你们设计的那个人。`--json` 的 `blocks[].chars` 直接给你占比。

**这可能是"好玩分"的第五维:提示词体检。**

---

## 4. 旧特性速查(1.0 ~ 1.2,你们大概已经在用)

| 层 | 一句话 | 开关 |
|---|---|---|
| 事件溯源 | `events` 是唯一真相,没有 `balances` 表,审计 = 重放 | 常开 |
| 记忆 2.0 | 三因子检索、反思、遗忘曲线 | 常开 |
| 行为树 + 日课 | `bt_nodes` / duties,角色按作息过日子 | 常开 |
| 需求 | energy / hunger / social 曲线驱动紧急带 | `needs.enabled` |
| 经济 | 物品 / 钱 / 店铺 / 价格漂移,账本是事件投影 | `economy.enabled` |
| 社交 | 三轴关系(常开)+ 八卦传播 + 小团体 | `social.enabled` |
| 剧情节拍 | `beats.json`,加载严格 / 触发降级 | 给了就有 |
| 提示词模板 | 12 个模板存 `prompt_templates`,**热改即生效** | 常开 |

**内置种子是橱窗,不是毛坯**(1.3.0 改的):它替那个世界点亮了 needs / economy /
social / stance / tools / intent / autonomy,并播了关系、创世记忆、钱、物品、货架、
目标、量与规律。你们生成种子时可以照抄这个形状 —— `anima_world/world_seed.json`
是它,随 wheel 分发,在 core 的 venv 里找得到。

但**引擎默认值仍然全关**。分工是:引擎默认值 = 没人说话时的样子,种子 = 这个世界的
作者的意见。你们生成的种子就是"作者的意见"。

---

## 5. 已知的洞(这些是我认的,不用你们说服我)

| 洞 | 状态 |
|---|---|
| `report` 缺"节拍触发/未触发" | 等你们提 issue,按你们的口径做 |
| `chat` 没有 `--message` 一次一问 | 同上 |
| 量 / 规律 / 感知 / autonomy 统计**全都没有 CLI 出口** | 已认,排 1.4.0 |
| 规律只能创世时进,不能改 | 已认,排 1.4.0(`World.set_rules` + CLI) |
| 跨实体相互作用(挖矿让矿脉减少)表达不了 | 要先设计扇入语义,1.4.0 之后 |
| `hidden` 的量绝对不可知(八卦链没接) | 1.4.0 |
| 逐轮 stance / tool_call 观测量没有 CLI 出口 | 等你们提 issue |
| 定时轮次行动率偏稀(约三四天一次) | 提示词是热改模板,可自己调;结构性改法(情境触发)排 1.4.0 |

---

## 6. 怎么提 issue

提到 anima-world 仓库。**照着你们那份《引擎接口诉求》的写法就很好** —— 它是我见过
最好用的跨仓库诉求文档,因为它写清了三件事:

1. **现状为什么不够**(而不是直接给方案)
2. **要什么问题的答案**,而不是要什么字段名 —— "字段名怎么起、怎么嵌套,引擎说了算"
3. **边界**:你们不会缓存、不会跨版本比较、不会写回

第 2 条尤其重要。你们那份文档里"我们需要的**是这些问题的答案**,不是某种具体格式"
那一句,是这次 `report` 能一次做对的原因。

唯一的建议:**加一句"这个功能在工作台哪一步被卡住"**。这次 `report` / `chat` 交付了
却没人告诉你们,你们的 P1 白等了几天 —— 如果诉求里写着"C3 卡在这条上",我在
CHANGELOG 里就会记得回一句。
