# anima-world 十个大版本路线图(v2.0 – v11.0)—— **设计存档,不是路线图**

> 🔴 **2026-08-25 复核:这份文档的坐标系已经不存在了,所以它的每一个版本号都是死的。**
> 它写在 1.0.0 发布之前,整份建立在一条规则上 —— **「主版本号 = db 格式」**。
> 而 **2.0 那一轮 world.db 整体退役了**(`db.py` / db 格式联锁 / `--db-path` 全部删除),
> 世界只住 Redis 的一个键前缀,主版本改成 **= 世界的可挂载性**。于是下面那张表里
> "v6.0 / db 6"这种写法**不是过期,是指着一个被删掉的东西**:今天的引擎是 3.7.0,
> 而它已经落了这张表排到 v9.0 的一部分内容。判据:
> `python -c "import anima_world; print(anima_world.__version__)"`;
> `anima-world contract --json | python -c "import json,sys; print(json.load(sys.stdin)['storage'])"`
> —— 那里面没有 `db` 这个段。
>
> **⚠️ 别按这份文档判"什么做了、什么没做",一条都别** —— 它答不出,而它看上去答得出。
> v2.0–v5.0(记忆 / 需求 / 经济 / 社交)整个并入了首发,这一条上面那版旗子已经说了。
> 而**后面几版的落法比"落了/没落"复杂**,举两个 2026-08-25 现查的:
> **v7.0 那句"现状的痛"已经不成立** —— 它写着「玩家是内存里的一个 dict,重启即蒸发」,
> 而 3.2.0 把在场搬进了 Redis(带 TTL,`contract --json` 的 `storage.presence`),
> 3.6.0 又加了邀请门;同一节里的**声誉**与**送礼**照旧没有。
> **v6.0 的 `beats v2` 换了个完全不同的形状落地** —— 3.7.0 把节拍收成了作者层第十二个段
> (它进得了 `.cyberworld`),而这一节写的 `repeat`/`arc`/`weight`/LLM 导演一个都没做。
> **逐版对账这件事这份文档不做**:当前实现一律以 [REFERENCE.md](REFERENCE.md) 与
> [ARCHITECTURE.md](ARCHITECTURE.md) 为准,已知的缺口在 [FOR-STUDIO.md](FOR-STUDIO.md) §5,
> 一版一版的实况在 [CHANGELOG.md](../CHANGELOG.md)。
>
> **那它为什么还留在 `docs/` 里**(2026-08-25 仓内自判,没有移进 `docs/archive/`):
> 它是导演系统 / 大地图与环境 / 时间线分支 / 世界联邦这几件事**唯一写下过设计的地方**,
> 真要开那几件活时要回来读。而**把一份说错话的文档挪进另一个目录并不会让它少说一句错话** ——
> 那是拿"它住在哪儿"冒充"它说得对不对",和这个仓库反复吃亏的那类判据是同一种。
> 所以留在原地,把旗子改准:**标题里写死它是存档,开头写死它的坐标系已经没了。**

> 规则回顾(⚠️ **这一段是当时的规则,今天已经不成立,留着是为了读懂下面的版本号**):
> 当时**主版本号 = db 格式**,每个大版本都动 db schema 所以都是主版本;第二/三位是程序优化。
> 硬钉版模型**这一半仍然成立**:老世界留在老版本上跑,新世界用新版本生成,
> **没有任何一版做跨版本迁移**。变的是"主版本钉的是什么" —— 从 db 格式变成了可挂载性。
>
> 每一版的代码块是**设计级实现**(可直接作为该版开发的起点),不是当前仓库的代码。
> 全部遵守既有铁律:事件日志只放历史、配置进表(data-plane)、唯一 RLock、
> LLM 永不进 tick 线程、加载严格 / 运行降级、派生值永不存储。
>
> 研究基础:Stanford Generative Agents 的三因子检索与反思机制、MemoryBank/SAGE 的
> Ebbinghaus 遗忘曲线、GATSim 的短期/长期分层记忆、sqlite-vec(SQLite 向量检索扩展)、
> 2026 年 LLM 生活模拟游戏的需求系统与活经济实践。文末附文献。

---

## 总览

| 版本 | db | 主题 | 一句话 |
|---|---|---|---|
| 2.0 | 2 | 记忆 2.0 | 三因子检索、反思、遗忘曲线 —— 角色开始"消化"经历 |
| 3.0 | 3 | 需求系统 | 饿、困、想社交 —— 行为从"按表办事"变成"活着" |
| 4.0 | 4 | 物品与经济 | 物品、钱、店铺、价格漂移 —— 世界有了物质层 |
| 5.0 | 5 | 关系 2.0 | 多维关系、八卦传播、小团体 —— 世界有了社会层 |
| 6.0 | 6 | 导演系统 | 剧情弧、LLM 导演、玩家任务 —— 节拍从脚本变成活导演 |
| 7.0 | 7 | 玩家一等公民 | 角色跨会话记得玩家,玩家有身份、有名声、能送礼 |
| 8.0 | 8 | 大地图与环境 | 多街区、昼夜、天气、路网 —— 世界有了空间纵深 |
| 9.0 | 9 | 人口与生态 | 程序化角色、搬入搬出、百人小镇 —— LOD 分级模拟 |
| 10.0 | 10 | 时间线分支 | 在任意 seq 分叉世界、what-if、回放 —— 事件溯源的收获期 |
| 11.0 | 11 | 世界联邦 | 角色跨世界旅行、书信往来 —— .cyberworld 从包变成信道 |

排序逻辑:2–3 让单个角色更可信;4–5 让角色之间的世界更可信;6–7 让玩家进得来;
8–9 把规模撑开;10–11 收获事件溯源架构的独特红利。前面的每一版都是后面的地基
(经济依赖需求,八卦依赖记忆,导演依赖关系,LOD 依赖需求+经济的可压缩性)。

---

## v2.0 —— 记忆 2.0:检索、反思、遗忘(db 2)

**现状的痛**:记忆是平面的 —— 定容 50 条、按最旧淘汰、按 importance 排序取前 k。
角色不会"由小事悟出大事",旧记忆不会褪色只会消失,聊天召回和当下话题无关。

### 需求

- **三因子检索**(Stanford 配方):`score = w_r·recency + w_i·importance + w_v·relevance`。
  recency 指数衰减;importance 已有;relevance 用嵌入余弦(可选依赖 sqlite-vec,
  没装则退化为关键词匹配 —— 降级不崩,老规矩)。
- **反思(reflection)**:累计 importance 过阈值时,后台线程让 LLM 从近期记忆归纳
  2–3 条"洞察"写回记忆流(kind='reflection',记录 source_ids)。角色开始有观点。
- **遗忘曲线**(Ebbinghaus / MemoryBank 配方):记忆有强度,被检索会加固,
  长期不用衰减;淘汰按"强度最低"而非"最旧",anchor 仍然永不删。
- **每日固化**:世界日切换时,把当天流水归纳成一条日记式记忆(短期→长期分层,GATSim 模式)。

### db 格式 2

```sql
-- memories 表新增(重建表,CHECK 迁移同款单事务舞蹈):
ALTER 语义:
  strength      REAL NOT NULL DEFAULT 1.0,   -- 遗忘曲线的当前强度
  last_access   INTEGER,                     -- 上次被检索的 tick
  access_count  INTEGER NOT NULL DEFAULT 0,
  source_ids    TEXT,                        -- reflection 的证据链(JSON id 数组)
  embedding     BLOB;                        -- 可选;sqlite-vec 不在时恒 NULL

CREATE TABLE IF NOT EXISTS reflection_state (   -- 反思触发器的水位
  agent_id TEXT PRIMARY KEY,
  accumulated_importance REAL NOT NULL DEFAULT 0,
  last_reflection_tick INTEGER NOT NULL DEFAULT 0
);
```

### 核心代码

```python
# memory_retrieval.py(新模块;MemoryStore 增加 retrieve())
import math

def score(memory, *, now_tick, query_vec, half_life_ticks=288*3,
          w_recency=1.0, w_importance=1.0, w_relevance=1.5):
    """三因子检索分。所有因子归一到 0~1;无嵌入时 relevance 缺席而非报错。"""
    age = max(0, now_tick - int(memory["tick"]))
    recency = math.exp(-math.log(2) * age / half_life_ticks)
    importance = min(1.0, float(memory["importance"]))
    relevance = 0.0
    if query_vec is not None and memory.get("embedding") is not None:
        relevance = _cosine(query_vec, memory["embedding"])
    return w_recency * recency + w_importance * importance + w_relevance * relevance

def reinforce(conn, memory_id, now_tick):
    """被检索的记忆加固(检索即复习)。"""
    conn.execute(
        "UPDATE memories SET strength = MIN(strength + 0.3, 3.0),"
        " last_access = ?, access_count = access_count + 1 WHERE id = ?",
        (now_tick, memory_id))

def decay_pass(conn, agent_id, now_tick, ticks_per_day=288):
    """每世界日一次:强度 = 强度 × 0.5^(闲置天数/强度)。强度越高忘得越慢
    (Ebbinghaus:复习过的曲线更平)。淘汰改为 strength ASC, anchor=0 优先。"""
    conn.execute(
        "UPDATE memories SET strength = strength * pow(0.5,"
        " CAST(? - COALESCE(last_access, tick) AS REAL) / ? / MAX(strength, 0.1))"
        " WHERE agent_id = ? AND anchor = 0", (now_tick, ticks_per_day, agent_id))
```

```python
# 反思:挂在 judge 池上(第四类 LLM 任务,复用池,不新增线程)
REFLECTION_THRESHOLD = 3.0   # config: memory.reflection_threshold

def maybe_reflect(scheduler, agent_id):
    # 锁内检查水位并快照近期记忆 → 池线程 LLM → 回锁写 memory_seed 事件
    # (kind='reflection', importance=0.8, source_ids=证据)。
    # 反思也是事件,重放可重建 —— 记忆仍然是派生真相。
    ...
```

### World API 新增

```python
world.retrieve_memories(agent_id, query: str, k=5)   # 三因子检索(聊天 grounding 换用它)
world.reflections(agent_id)                          # 只看洞察
# config 新键:memory.half_life_days / memory.reflection_threshold / memory.embedding(bool)
```

**风险**:嵌入调用进 tick 线程 —— 绝不;嵌入在记忆写入时由池线程补(缺了照样检索)。
**不做**:向量库外置(sqlite-vec 或纯 Python 余弦够 50~200 条/角色的规模)。

---

## v3.0 —— 需求系统:角色开始"活着"(db 3)

**现状的痛**:行为树三层是"值班表 + LLM 排班 + 闲逛",角色没有内在状态。
不吃不睡不孤独,duty 之外的行为全靠 planner 编,可信度封顶。

### 需求

- 每角色四条基础需求曲线:**energy / hunger / social / mood**,随 tick 衰减,
  由动作恢复(sleep→energy,eat→hunger,chat/idle_social→social,mood 是前三者的函数)。
- 行为树插入**效用层(utility band)**:duty 之上加"紧急需求"(饿晕了先吃饭再上班),
  duty 之下 follow_plan 之上加"普通需求"。Selector 顺序即优先级,不引入新树类型,
  只加一种叶子:`need_action`。
- planner 的 prompt 注入当前需求水平 —— LLM 排班开始"顺着身体状态写"。
- 需求水平进 `state()`、进聊天 grounding("她看起来很累")。

### db 格式 3

```sql
CREATE TABLE IF NOT EXISTS agent_needs (      -- 当前值:data-plane(不是历史)
  agent_id TEXT NOT NULL, need TEXT NOT NULL CHECK (need IN ('energy','hunger','social','mood')),
  value REAL NOT NULL DEFAULT 1.0,            -- 0~1
  updated_tick INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (agent_id, need)
);
-- bt_nodes.type 的 CHECK 增补 'need_action'(重建表,老舞蹈)
-- 需求"跨越阈值"才发 state_change 事件(kind='need_band')——
-- 连续曲线不进事件日志,只有台阶进(和关系档位同一哲学)。
```

### 核心代码

```python
# needs.py
DECAY_PER_TICK = {"energy": 1/288, "hunger": 1/192, "social": 1/432}
BANDS = (0.15, 0.4)   # 紧急 / 偏低 / 正常

def tick_needs(conn, agent_id, now_tick):
    """惰性结算:读时按 (now - updated_tick)×衰减率补账,不逐 tick 写库。
    100 角色 × 288 tick/日也只有真正被读到的行需要更新。"""
    rows = conn.execute("SELECT need, value, updated_tick FROM agent_needs WHERE agent_id=?",
                        (agent_id,)).fetchall()
    out = {}
    for need, value, updated in rows:
        if need == "mood":
            continue
        value = max(0.0, value - DECAY_PER_TICK[need] * (now_tick - updated))
        out[need] = value
    out["mood"] = 0.2 + 0.8 * min(out.values())   # 木桶效应
    return out

class NeedAction:                                  # bt_nodes 新叶子
    """value < threshold 时 SUCCESS 并选中恢复动作;衰减是数学,不是 LLM。"""
    def __init__(self, need, threshold, action_id):
        self.need, self.threshold, self.action_id = need, threshold, action_id
    def tick(self, bb):
        if bb.read(f"need.{self.need}", 1.0) < self.threshold:
            bb.write("_selected_action_id", self.action_id)
            return Status.SUCCESS
        return Status.FAILURE
```

**World API**:`world.needs(agent_id)`;config 新键 `needs.enabled`(false = 全部行为同 v2,
老世界的行为语义一丝不变 —— 这类"机制开关"从本版起是惯例)。

---

## v4.0 —— 物品与经济:世界有了物质层(db 4)

**需求**

- **物品定义**(配置)与**库存**(当前值):咖啡、画、图纸…可持有、可转移、可消耗。
- **钱**:每角色余额;工资(duty 结算)、消费(eat 要花钱)、买卖。
- **店铺**:地点挂商品与库存;价格按供需慢漂移(2026 年 AI NPC 游戏的标配:
  你把咖啡买断,明天真的涨价)。
- 转移/消耗全部走事件(`item_transfer` / `item_consume` / `payment`),
  余额与库存是投影 —— **对账 = 重放**,天生防复制品 bug。

### db 格式 4

```sql
CREATE TABLE IF NOT EXISTS item_defs (        -- 配置:studio 作者数据
  id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,   -- consumable/durable/artwork
  base_price REAL NOT NULL DEFAULT 0, restores TEXT              -- JSON: {"hunger": 0.4}
);
CREATE TABLE IF NOT EXISTS shop_stock (       -- 当前值(data-plane)
  location_id TEXT NOT NULL, item_id TEXT NOT NULL,
  quantity INTEGER NOT NULL, price REAL NOT NULL,
  PRIMARY KEY (location_id, item_id)
);
-- 库存与余额不建表:它们是 item_transfer/payment 事件的投影,存快照里。
```

### 核心代码

```python
# economy.py — 价格漂移:每世界日结算一次,纯函数
def drift_price(base, price, sold, restocked, *, k=0.08, floor=0.25, cap=4.0):
    pressure = (sold - restocked) / max(sold + restocked, 1)
    target = price * (1 + k * pressure)
    return max(base * floor, min(base * cap, 0.7 * price + 0.3 * target))

# projection.py 新增事件:
#  item_transfer {from, to, item_id, qty}   → 投影里挪库存
#  payment       {from, to, amount, reason} → 投影里挪余额(负债允许,行为树会去打工)
#  item_consume  {who, item_id}             → 减库存 + 按 item_defs.restores 恢复需求
```

**World API**:`world.inventory(agent_id)` / `world.balance(agent_id)` /
`world.shop(location_id)` / `world.player_buy(player_id, location_id, item_id)`(玩家钱包
= players dict 的字段,宿主充值)。**依赖 v3**:eat/consumable 需要需求系统接收恢复效果。

---

## v5.0 —— 关系 2.0 与社会结构(db 5)

**需求**

- **关系多维化**:单一 sentiment → `trust / affection / respect` 三轴 + 保留档位机制。
  判官(relationship judge)升级为三轴裁定,单轴仍限 ±0.2、同日阻尼照旧。
- **八卦传播**:A 的高重要度记忆,在 A、B 同地闲聊时以概率衰减复制给 B
  (`kind='hearsay'`,带 `heard_from` 与失真等级)—— 信息第一次可以**间接**到达,
  声誉由此涌现:C 没见过玩家也可能"听说过你"。
- **小团体**:从关系图谱周期性抽社群(纯图算法,不用 LLM),团体进聊天 grounding
  ("你们都是老港口画室那伙的")。

### db 格式 5

```sql
-- state_change 事件的 payload 扩展三轴(事件格式加字段 = 向后兼容,老事件读作单轴);
-- 投影 Relation 增加 trust/affection/respect;快照格式随之 +3 字段。
CREATE TABLE IF NOT EXISTS cliques (          -- 派生缓存:可随时重算,存表只为省重算
  id INTEGER PRIMARY KEY, member_ids TEXT NOT NULL, label TEXT, computed_tick INTEGER NOT NULL
);
```

### 核心代码

```python
# gossip.py — 挂在 idle_social 的动作后果上,tick 线程只掷骰子,不调 LLM
def maybe_gossip(rng, speaker_memories, listener_id, *, p=0.25, distortion=0.15):
    """同地闲聊时:说者最重要的近期非私密记忆,以 p 概率变成听者的 hearsay。
    每转一手 importance × (1-distortion),三手以后自然消亡 —— 谣言有半衰期。"""
    candidates = [m for m in speaker_memories
                  if m["importance"] >= 0.5 and m["kind"] not in ("hearsay3",)]
    if not candidates or rng.random() > p:
        return None
    src = max(candidates, key=lambda m: m["importance"])
    hop = int(src["kind"][7:]) + 1 if src["kind"].startswith("hearsay") else 1
    return {"agent_id": listener_id, "kind": f"hearsay{hop}",
            "summary": src["summary"], "importance": src["importance"] * (1 - distortion),
            "heard_from": src["agent_id"]}

# cliques.py — label propagation,50 角色毫秒级,LLM 只给团体起名(池线程)
```

**依赖 v2**(八卦复制的是记忆流)。**风险**:八卦风暴 —— 每对每日限一条 + 失真链上限 3 手。

---

## v6.0 —— 导演系统:剧情从脚本变成活导演(db 6)

**需求**

- **beats v2**:`repeat + cooldown`(v1 只有 once)、`arc`(节拍归属剧情弧,弧有
  阶段推进条件)、`weight`(同 tick 多个 due 时按权重择一,避免剧情堆叠)。
- **LLM 导演**(可关):每世界日一次,后台读世界状态(关系张力、闲置角色、未推进的弧),
  **生成候选 beat 提案** —— 提案必须通过 v1 同款严格校验才入库,校验不过就丢弃并记日志。
  LLM 只能提案,**永远不能直接改世界**:提案 → 严格校验 → 成为普通 beat → 正常触发。
  这是"加载严格/运行降级"哲学对生成内容的自然延伸。
- **玩家任务**:beat 的 op 新增 `offer_quest`(给玩家的目标 + 奖励),完成判定走谓词。

### db 格式 6

```sql
CREATE TABLE IF NOT EXISTS story_arcs (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, stage INTEGER NOT NULL DEFAULT 0,
  stages TEXT NOT NULL,          -- JSON:每阶段的推进谓词(与 beat when 同一谓词语言)
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','resolved','abandoned'))
);
CREATE TABLE IF NOT EXISTS proposed_beats (   -- 导演提案的隔离区:校验通过才转正
  id INTEGER PRIMARY KEY, beat_json TEXT NOT NULL, arc_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','rejected')),
  reason TEXT, created_tick INTEGER NOT NULL
);
-- beat_fired 事件 payload 增加 arc_id/stage —— 剧情推进本身是历史,可重放。
```

### 核心代码

```python
# director.py — 跑在 planner 池,一日一次
def director_pass(world_summary, arcs, llm) -> list[dict]:
    """输入:关系张力 top5、闲置角色、卡住的弧。输出:0~2 个 beat 提案(JSON)。
    出口只有一个:BeatScript._validate_script 对提案逐个过闸,
    错一个字段就 rejected + reason 存档 —— 导演的失败是数据,不是事故。"""
    raw = llm.complete(DIRECTOR_PROMPT.format(**world_summary))
    proposals = _extract_json_list(raw) or []
    accepted = []
    for p in proposals:
        errors = validate_single_beat(p)          # 复用 beats.py 的严格校验
        if errors:
            log_rejection(p, errors)
            continue
        accepted.append(p)
    return accepted[:2]
```

**config**:`director.enabled`(⚠️ **3.11.2 起默认 true** —— 见下面第 2 条那个
例外;这份路线图写下时是 false)/ `director.max_beats_per_day`。
**World API**:`world.arcs()` / `world.quests(player_id)` / `world.complete_quest(...)`。

---

## v7.0 —— 玩家一等公民(db 7)

**现状的痛**:玩家是内存里的一个 dict,重启即蒸发;角色对玩家的记忆有,但玩家自己
没有世界内的持久身份。

### 需求

- **玩家档案入库**:display_name、外观描述、首次到访、累计天数 —— 重启后角色真的
  "记得你上次来过"(players 表是当前值;到访/离开是事件)。
- **玩家声誉**:v5 的八卦机制自动覆盖玩家(hearsay 的主语可以是玩家),加一个
  聚合读数:`reputation(player_id)` = 全体角色对玩家三轴的加权均值 + 档位词。
- **送礼/帮忙**:玩家动作接上 v4 经济(送出物品 → 判官按物品价值与关系语境裁定
  三轴变化)与 v6 任务(完成任务 → 声誉与奖励)。
- **多玩家在场**:players 不再互斥;角色同时应对两个玩家时,grounding 里有彼此。

### db 格式 7

```sql
CREATE TABLE IF NOT EXISTS players (
  player_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
  persona TEXT, first_seen_tick INTEGER NOT NULL, last_seen_tick INTEGER NOT NULL,
  wallet REAL NOT NULL DEFAULT 0
);
-- player_arrive / player_depart 进事件日志(在场历史可重放);
-- 现 players dict 变成这张表的内存缓存。
```

**World API**:`world.register_player(player_id, display_name, persona=None)` /
`world.reputation(player_id)` / `world.player_gift(player_id, agent_id, item_id)`。
纪律 3 不变:player_id 的真实性仍是宿主的责任,引擎只负责"世界内的这个人是谁"。

---

## v8.0 —— 大地图与环境(db 8)

**需求**

- **多街区**:嵌套地图从"一个旧港区"到"若干区域 × 每区若干点";`region` 可标
  `travel_kind`(步行/轮渡…),跨区旅行时间显著大于区内。
- **路网**:region 间显式 `paths` 表(v1 刻意无墙无门,到城市尺度直线距离失真),
  区内仍然直线 —— 分层寻路:区间 Dijkstra + 区内直线,50 地点毫秒级。
- **昼夜与天气**:世界日历派生昼夜(不存,老规矩);天气是**事件**(`weather_change`,
  马尔可夫链日更),投影持有当前天气;BT 谓词与 planner/narrative prompt 都能读
  ("下雨 → 不去海边写生 → 咖啡店人多 → 相遇率上升"——环境变成剧情泵)。

### db 格式 8

```sql
CREATE TABLE IF NOT EXISTS paths (
  from_region TEXT NOT NULL, to_region TEXT NOT NULL,
  travel_kind TEXT NOT NULL DEFAULT 'walk', minutes INTEGER NOT NULL,
  PRIMARY KEY (from_region, to_region)
);
-- locations 表加列 travel_kind;weather_change 是新事件类型(payload: {weather, until_tick})
```

```python
# routing.py — 分层寻路
def route_minutes(loc_store, paths, a, b, minutes_per_unit):
    ra, rb = loc_store.region_of(a), loc_store.region_of(b)
    if ra == rb:
        return loc_store.distance(a, b) * minutes_per_unit          # 区内:v1 语义原封不动
    inter = dijkstra(paths, ra, rb)                                  # 区间:路网
    return (loc_store.distance_to_gate(a, ra) + inter
            + loc_store.distance_from_gate(rb, b)) * minutes_per_unit
```

---

## v9.0 —— 人口与生态:百人小镇(db 9)

**需求**

- **程序化角色**:studio/导演给"角色模板 + 生成参数",引擎批量铸造
  (LLM 生成 persona,严格 schema 校验后走标准 `agent_join` —— 又是提案/闸门模式)。
- **搬入搬出**:`agent_leave/return` 之上加 `agent_emigrate/immigrate`(永久离开/新到),
  人口随经济与社交密度自调节(空置职位吸引移民,长期孤独触发搬走)。
- **LOD 分级模拟**(规模的关键):角色分三档 ——
  **焦点**(玩家同区/剧情弧成员):全速,LLM 全开;
  **背景**(同世界不同区):每 N tick 粗结算(需求按公式走,不跑行为树,不调 LLM);
  **冬眠**(离场/极远):只结算需求地板,聚合成"区域氛围"统计。
  档位每日重算,纯规则。LLM 成本从 O(人口) 变成 O(焦点数)。

### db 格式 9

```sql
CREATE TABLE IF NOT EXISTS agent_templates (
  id TEXT PRIMARY KEY, archetype TEXT NOT NULL, params TEXT NOT NULL   -- JSON 生成参数
);
-- agents 无表(仍是事件投影);LOD 档位是派生值,每日算,不存 —— 又一个不许有的第二真相源。
```

```python
# lod.py
def assign_bands(agents, focus_regions, arc_members):
    for aid, brain in agents.items():
        if brain.region in focus_regions or aid in arc_members:
            yield aid, "focus"
        elif brain.away:
            yield aid, "dormant"
        else:
            yield aid, "background"

# scheduler.tick():background 档每 12 tick 走一次 coarse_tick(需求公式+计划步进,无 BT 无事件风暴)
```

**风险**:档位切换的"苏醒不一致"(背景角色被玩家撞见时状态粗糙)——
苏醒时用一次 LLM 补写"这段时间他大概在干嘛"的记忆,成本一次性。

---

## v10.0 —— 时间线分支与回放(db 10)

**这是事件溯源架构十年一遇的收获**:世界 = 事件日志,那么"平行世界"= 同一日志
在某 seq 之后的两条尾巴。

### 需求

- **分叉**:`fork_world(db, at_seq, out_path)` —— 复制 events ≤ seq、重放建投影、
  盖新 world_id 与 `forked_from {world_id, seq}`。玩家的"如果那天我没说那句话"
  变成一个真的可以进去住的世界。
- **回放**:`replay(db, from_seq, to_seq)` 产出逐事件的叙事流(studio 的时间轴 UI 数据源);
  快照链让任意 seq 的"当时世界"秒级可得。
- **世界 diff**:两个同源世界对齐 `forked_from.seq` 之后逐事件对比,输出
  "分歧点之后,夏在 A 线爱上了遥,在 B 线离开了小镇" —— LLM 只负责把 diff 写成人话。

### db 格式 10

```sql
CREATE TABLE IF NOT EXISTS lineage (           -- 世系:我从哪条时间线的哪个点来
  key TEXT PRIMARY KEY CHECK (key = 'origin'),
  forked_from_world TEXT, forked_at_seq INTEGER, forked_at_wall TEXT
);
```

```python
# timeline.py
def fork_world(src_conn, at_seq, dst_path, new_world_id):
    dst = open_db(dst_path)                                   # 新库,格式 10
    with dst:
        for row in src_conn.execute(
                "SELECT ts,type,who,loc,payload FROM events WHERE seq<=? ORDER BY seq", (at_seq,)):
            dst.execute("INSERT INTO events (ts,type,who,loc,payload) VALUES (?,?,?,?,?)", row)
        dst.execute("INSERT INTO lineage VALUES ('origin', ?, ?, ?)",
                    (src_world_id(src_conn), at_seq, _now_iso()))
        _copy_data_plane(src_conn, dst)        # 地图/行为树/配置(secret 除外)/物品定义
    # 快照不复制:fork 后首开全量重放一次,顺手验证日志自洽
```

**World API**:`world.fork(at_seq, out_path)` / `world.replay(from_seq, to_seq)`。
`.cyberworld` manifest 增加 `lineage` 段(包格式版本 +1,运维台镜像同步)。

---

## v11.0 —— 世界联邦:角色跨世界旅行(db 11)

**需求**

- **书信**:世界 A 的角色给世界 B 的角色写信(LLM 生成,严格校验后打成
  `.cyberletter` —— 一个微型签名包);B 世界导入后成为收信人的记忆 + 可能的剧情触发。
- **旅行**:角色打包(persona + 记忆精选 + 关系摘要)成 `agent bundle`,
  从 A `agent_emigrate`,在 B `agent_immigrate` —— 两边都是既有事件,天然可重放;
  B 世界的导演收到"外来者"可自动起弧。
- **联邦规约**:包格式扩展 `federation` 段(来源世界、引擎版本、内容签名)。
  **同主版本才能互通**(主版本 = db 格式,天然就是联邦兼容线 —— 你的版本规则
  在这一版收全款)。运维平台负责传输与请求头凭证,引擎只做打包/校验/导入,
  信任边界仍在进程/平台边界上。

### db 格式 11

```sql
CREATE TABLE IF NOT EXISTS federation_log (    -- 收发记录:防重放(delivery_id 唯一)
  delivery_id TEXT PRIMARY KEY, direction TEXT NOT NULL CHECK (direction IN ('in','out')),
  kind TEXT NOT NULL CHECK (kind IN ('letter','agent')),
  peer_world TEXT NOT NULL, payload_hash TEXT NOT NULL, applied_tick INTEGER
);
```

```python
# federation.py — 全部复用 world_package 的安全底座(zip 约束/校验和/确定性打包)
def export_agent_bundle(world, agent_id, out_path):
    bundle = {
        "persona": world._persona(agent_id),
        "memories": world.retrieve_memories(agent_id, query="一生中最重要的事", k=20),
        "relations": world.graph(agent_id),
        "needs": world.needs(agent_id),
    }
    _write_signed_package(bundle, out_path, kind="agent")     # v2 记忆检索在这里收全款

def import_letter(world, package_path):
    letter = _read_verified_package(package_path, kind="letter")   # 严格校验,坏包当场拒
    if _seen(world, letter["delivery_id"]):
        return "duplicate"
    world.scheduler._record_event({"type": "memory_seed", ...})    # 信 = 收信人的一条记忆
```

---

## 贯穿十版的四条工程纪律

1. **每一版的新 LLM 用途都走同一个模式**:LLM 只能提案(反思/导演/铸造角色/写信),
   出口永远是严格校验的闸门,校验不过就丢弃留痕。世界状态只被"通过校验的普通事件"改变。
2. **每一版的新机制都带开关**(`needs.enabled` / `director.enabled` / …,默认关):
   ⚠️ **`director.enabled` 3.11.2 起是这条规矩唯一的例外,而那是有意的**:
   编剧不是一个可选特性,它就是那一版的产品命题,且没配 key 也照跑
   (整条 mock 路是活的)。实测过默认关的下场 —— 三个世界升上去之后**一拍都没写**,
   而每一块屏幕都显示正常。**这条例外要留在这儿,否则下一个人会照这一段把它关回去。**
   大版本升级 ≠ 行为突变,studio 可以按世界逐个点亮。
3. **连续量不进事件日志**:需求曲线、价格、强度衰减都是公式派生或台阶事件 ——
   日志只记跨档,防止 288 tick/日 × 百角色的事件洪水。
4. **每版发布清单**:`DB_FORMAT_VERSION` +1、主版本对齐、`world_package` 兼容区间
   自动指向新主版本、运维台镜像(包格式若动)同步、CHANGELOG 标注四契约状态。

## 参考文献

- Park et al., *Generative Agents: Interactive Simulacra of Human Behavior*(三因子检索、反思阈值)
- MemoryBank / SAGE:Ebbinghaus 遗忘曲线在 LLM 长期记忆中的应用
- GATSim(2025):短期/长期分层记忆的城市模拟实践
- ACAN(2025):交叉注意力记忆检索(v2 的可选升级路径)
- sqlite-vec:SQLite 向量检索扩展(v2 的可选依赖)
- 2026 LLM 生活模拟游戏实践(AI Cultivation World Simulator 等):无脚本涌现叙事、
  声誉定价、活经济
