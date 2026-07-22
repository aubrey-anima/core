# anima-world 功能与接口参考

> 本文档面向两类读者:想了解引擎能做什么的人,和要对接它的程序(网站、运维台、创作台 anima-studio)。
> 契约级别的权威定义永远以代码为准(见 [README](../README.md) 的契约表);本文是可查阅的展开说明。
> 对应引擎版本:0.1.0(db 格式 1,包格式 1,内部协议 v1)。

---

## 目录

1. [引擎是什么、不是什么](#1-引擎是什么不是什么)
2. [功能详解](#2-功能详解)
3. [CLI 参考](#3-cli-参考)
4. [HTTP API 参考](#4-http-api-参考)
5. [环境变量](#5-环境变量)
6. [配置键参考](#6-配置键参考)
7. [提示词模板](#7-提示词模板)
8. [数据文件](#8-数据文件)
9. [节拍脚本格式](#9-节拍脚本格式)

---

## 1. 引擎是什么、不是什么

**是**:一个可 pip 安装的世界运行时。一个 `world.db` 文件就是一个世界;引擎负责跑世界(时钟、
角色决策、LLM 叙事/规划/关系)、快进世界、把世界打包成可分发的 `.cyberworld`。

**不是**:
- **没有任何网页界面**。三组 HTTP API 全部只返回 JSON;`/` 返回一段自我说明。玩家界面归网站
  (走 `/internal/v1`),管理台归运维台(走 `/api/admin/v1`)。
- **没有创作功能**。把小说变成世界种子、编排剧情,是独立桌面程序 anima-studio 的事。
  studio 以子进程方式驱动本引擎(每个引擎版本一个隔离 venv),永不 import 本包 ——
  `tests/test_packaging.py` 机器强制这条边界。
- **不做跨版本迁移**。一个世界钉死在生成它的引擎版本上(版本即契约,详见 §2.9)。

---

## 2. 功能详解

### 2.1 事件核(event sourcing)

世界的全部历史是一条 **append-only 事件日志**(`events` 表,`seq` 自增主键即全局逻辑时钟)。
只有 INSERT,没有 UPDATE/DELETE。当前状态(角色、关系、地点、叙事日志)是把事件流 fold
出来的**投影**,随时可以从 seq=0 重放重建;快照(`snapshots` 表)只是投影在某个 seq 的缓存,
恢复 = 最新快照 + 尾部增量重放。

事件类型(投影器处理的全集):`agent_join` / `agent_move` / `agent_action` / `agent_idle` /
`agent_leave` / `agent_return` / `location_join` / `narrative` / `user_message` /
`capability_registered` / `state_change`(按 `payload.kind` 二级分发:`sentiment`、
`sentiment_delta`、`r_type`、`agent_state`、`persona_update` 等)。未知类型静默忽略 ——
废弃旧事件天然向后兼容。

关系值有两条写入路径:`sentiment`(绝对赋值,只用于创世注入)和 `sentiment_delta`
(累加并 clamp 到 [-1,1],运行期一律用它)—— 保证一次闲聊不会覆盖种子设定的宿怨。

**data-plane 原则**:事件日志只放"发生了什么";地图、行为树、种子、节拍脚本是"配置",
存在表里或 JSON 文件里,不进日志。

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
  live 读取,admin 改完下次调用即生效。**没有 key 时全线降级 Mock**:世界照跑、事件照发,
  只是文本是模板 —— 降级不许无声(见 §2.8)。
- **三个独立线程池**(各 2 worker):叙事(把动作写成人读文本)、规划(自由时间计划)、
  关系判定。**LLM 永不在 tick 线程或请求线程调用**;世界事件在提交前已记录,LLM 结果
  回来时才补落地,LLM 挂了世界不停。
- **关系判定(relationship judge)**:一次聊天结束后,LLM 裁定双向**不对称**的好感度变化,
  单次上限 ±0.2;同一对角色同日多次判定按 0.5^(N-1) 阻尼;跨越关系档位
  (宿敌/交恶/淡漠/熟识/亲近/挚交)时由 LLM 用一句 ≤20 字中文短语重写关系描述。

### 2.5 记忆与图谱

- **记忆**(每角色私有):规则式触发器决定什么值得记 —— 会话摘要、关系跨档位。容量
  `memory.capacity`(默认 50)条,超出按最旧淘汰,`anchor` 记忆永不删。记忆是派生真相,
  `memories` 表清空后可从事件日志重建。
- **图谱**(跨角色共享):`edges` 表存 (subject, predicate, object) 三元组,只编码关系
  **结构**(friendship / rivalry / conversation),永不含消息内容。

### 2.6 聊天子系统

与事件核解耦:聊天回合本身不发事件,**整场会话只在关闭时发一个 summary 事件**
(零消息会话静默关闭)。会话按 (agent, player) 键控;空闲超过 `chat.idle_timeout`
(默认 600 秒)由收割线程自动关闭。

`/internal/v1/chat` 路径**不落平台历史**:网站每次把最近 ≤20 条对话随请求带来,完整
转录留在网站库里;世界侧的 prompt 由自持状态构成(persona + 世界观 + grounding 块 +
认证身份块)。回复流式返回,内置一个 token 级状态机给全角括号内的动作描写补角色名前缀。

玩家会话关闭后触发关系判定(读真实转录),让玩家进入和 NPC 相同的关系机制。

### 2.7 剧情节拍(beat director)

节拍脚本是编排好的剧情,打进运行中的世界。**加载严格**(坏脚本当场列出全部错误、拒绝
启动),**触发降级**(运行时谓词失败读作"未满足",坏 op 跳过并警告,绝不让世界崩溃)。
哪些 beat 已触发是历史(`beat_fired` 事件),重启后不重放。格式详见 §9。

### 2.8 配置与密钥

配置存 `config` 表,带类型(str/int/float/bool)、分类、是否 secret。secret 用 **Fernet
加密**入库,密钥在 db 旁边的 `world.db.key` 文件(0600 权限)—— **搬迁 db 必须带上它**。
丢了 keyfile,`llm.api_key` 读不出来,世界静默降级 Mock,但三处会点名真实原因
("没配过" 与 "读不出来" 严格区分):serve 启动警告、`anima-world doctor`、
`/api/state` 的 `runtime.llm.degraded_reason`。

提示词模板(约 12 个)存 `prompt_templates` 表,拼 prompt 现场 live 读取,改完即生效;
保存前用代表性变量试渲染一次,占位符错误直接 400。

### 2.9 版本即契约

一个 core 版本 = (引擎代码, 内部协议版本, db 格式版本, claim 签名格式) 一起冻结:

- `anima_world.__version__` 是唯一版本源(pyproject 动态读取)
- **主版本号 = db 格式**:db 格式变才升第一位;第二、三位都是程序优化(第二位加能力,
  第三位纯修 bug)
- `DB_FORMAT_VERSION` 联锁:挂上更新格式的 db 当场拒绝打开,**不写入任何表**
- membership claim 线格式**永久冻结**(黄金向量测试钉死到字节,见 `tests/test_claim_freeze.py`)
- `/internal/v1` 冻结,只加字段;破坏性变更走 `/internal/v2` 并行

---

## 3. CLI 参考

命令分两拨:**给人打的**(`start` / `config` / `doctor`)和**给部署打的**
(`serve` / `simulate` / `world`)。裸 `anima-world` 打印欢迎页指路 `start`。

### 3.1 anima-world start —— 人的门

引导配 LLM(真调一次验证连通;直接回车 = 先用 Mock)→ 建世界(新世界用演示速度
1 tick/秒)→ 启动并打开浏览器。端口被占自动往后找(最多 10 个)。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--db-path` | `saves/world.db` | 世界文件位置,不存在就新建 |
| `--host` / `--port` | `127.0.0.1` / `8000` | 绑定地址与起始端口 |
| `--seed` | 内置种子 | 世界种子 JSON,**只对新建世界生效** |
| `--beats` | 无 | 节拍脚本 JSON |
| `--no-input` | - | 不交互提问(CI / 脚本) |
| `--real-time` | - | 新世界也用真实时间,不用演示速度 |

### 3.2 anima-world config —— 改配置不用 curl

```bash
anima-world config list [--category llm]     # 密钥自动打码,未设置显示"(未设置)"
anima-world config get llm.model
anima-world config set llm.api_key sk-…      # 按声明类型强转后写入,立即生效
```

`--db-path` 同上。`set` 未知键返回退出码 2。

### 3.3 anima-world doctor —— 体检

检查:世界文件是否存在、`world.db.key` 是否在(不在则警告旧密钥永久读不出)、db 格式
版本、事件/角色计数、LLM 状态(四态:没建库 / 读不出来 / 没配 / 正常)+ **真调一次 LLM**
(`--skip-probe` 跳过)、时钟快慢翻译成人话。有问题退出码 1。

### 3.4 anima-world serve —— 部署的门

生产入口:不引导、不改时钟。世界镜像跑的就是它。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` / `--port` | `127.0.0.1` / `8000` | **绑非 loopback 时 `/api/*` 自动要求 admin token** |
| `--db-path` | `saves/world.db` | 世界文件 |
| `--seed` | 内置种子 | 只在空库首启读一次;对已有库会明说被忽略 |
| `--beats` | 无 | 坏脚本直接拒绝启动 |
| `--tick-rate` | 无 | **仅当 db 无配置行时生效**;db 里的 `scheduler.tick_rate` 永远赢 |
| `--instance-id` / `--world-id` / `--world-name` | `legacy` | 运行时身份(claim 受众绑定用) |
| `--agents` | 种子全员 | 注册角色数上限 |
| `--world-admin-token-env` | `ANIMA_WORLD_ADMIN_TOKEN` | admin token 从哪个环境变量读 |
| `--platform-service-token-env` | `ANIMA_WORLD_SERVICE_TOKEN` | 平台服务凭证(逗号分隔多个) |
| `--membership-claim-secret-env` | `ANIMA_MEMBERSHIP_CLAIM_SECRET` | claim 签名密钥 |
| `--cors-origin` | 无 | 可重复;不允许 `*` |

服务凭证与 claim 密钥**必须成对配置**,否则退出码 2。loopback 上两者都缺省时自动注入
本地开发默认值(`anima-loopback-*`),非 loopback 不注入(internal 组 fail-closed)。
收 SIGINT/SIGTERM 优雅退出。

### 3.5 anima-world simulate —— 无头快进

不睡眠、不起 web,把世界快进 N 天/N tick。

| 参数 | 说明 |
|---|---|
| `--db-path`(必填) | 世界文件 |
| `--days N` / `--ticks N` | 二选一必填 |
| `--llm full\|planner\|mock` | 三档:全真 / 真规划+Mock 叙事(长跑推荐)/ 全 Mock |
| `--no-llm` | `--llm mock` 的别名,同时给时它赢 |
| `--plan-wait-cap` | 每世界日等待在途计划的秒数上限(默认 2×planner.timeout) |
| `--seed` / `--beats` / `--agents` | 同 serve |

非 mock 档会**先预检 LLM 再建世界**(坏 key 不会把降级能力目录种进新库)。内置"计划
等待预算":连续两个世界日等待耗尽则判定 planner 死亡、不再等待,绝不挂起。

### 3.6 anima-world world export / import —— 打包

```bash
# 模板包(只有种子,世界首启自建 db)
anima-world world export --seed seed.json --output my.cyberworld \
    --world-id my-world --name "我的世界" --mode template

# 快照包(带跑过的 world.db;secret 配置行会被剥除)
anima-world world export --seed seed.json --db-path saves/world.db \
    [--beats beats.json] --output my.cyberworld \
    --world-id my-world --name "我的世界" --mode snapshot \
    [--summary … --genre … --setting … --theme …]

anima-world world import my.cyberworld --destination ./instances
```

成功时 stdout 输出一行 JSON(export: `operation/world_id/revision_id/mode`;
import: `operation/world_id/instance_id/path`);失败退出码 2。
`--world-id` 必须匹配 `^[a-z0-9][a-z0-9._-]{0,63}$`。

---

## 4. HTTP API 参考

三组受众、三种鉴权。所有响应都是 JSON;`/` 返回 404 + 自我说明。

### 4.1 `/api/*` —— 本机查看与配置

**鉴权**:loopback 绑定上无鉴权(面向本机运维);**非 loopback 绑定时整组要求
`Authorization: Bearer <admin_token>`**,未配置 admin token 则整组关闭。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state`、`/api/v1/state` | 世界快照(角色/关系/地点/叙事,`runtime.llm.degraded_reason` 常驻);v1 前缀多带协议版本与身份 |
| GET | `/api/v1/world` | 身份卡:protocol_version / instance_id / world_id / capabilities |
| POST | `/api/simulation/toggle` | 暂停/继续世界时钟 |
| GET | `/api/memories/{agent_id}` | 某角色的记忆 |
| GET | `/api/graph` | 关系图谱三元组 |
| GET | `/api/config` | 全部配置(secret 打码为 `前3***后4`) |
| PUT | `/api/config/{key}` | 改配置,body `{"value": …}`;按声明类型强转,立即生效;secret 空值拒绝 |
| GET | `/api/prompts` | 全部提示词模板 |
| PUT | `/api/prompts/{name}` | 改模板;保存前试渲染,占位符错误 400 |
| GET | `/api/conversations/{agent_id}` | 某角色的会话列表 |
| GET | `/api/conversations/{id}/messages` | 会话消息 |
| POST | `/api/conversations/{id}/close` | 手动关闭会话(触发摘要+事件) |
| GET | `/api/stream`、`/api/v1/stream` | SSE:首帧 `catchup`(可带 `?since_seq=`),之后 `world` 事件 + `heartbeat` |

### 4.2 `/internal/v1/*` —— 网站 → 世界容器

**协议已冻结**:端点、claim 键集、header 回显只增不改;破坏性变更走 `/internal/v2`。

**鉴权(双因子,缺一不可)**:

```
Authorization: Bearer <平台服务凭证>          # 长期,机器对机器,常量时间比对
X-Cyberworld-Membership: <membership claim>  # 短期(默认 60s),每请求签发
```

claim 格式(**永久冻结**):`base64url(payload).base64url(HMAC-SHA256签名)`,payload
恰好 6 个字段 `membership_id / world_id / role / instance_id / iat / exp`,按 key 排序、
紧凑分隔符序列化,签名覆盖编码后文本。验签规则:多一个字段拒收;`world_id`+`instance_id`
必须与本运行时一致(受众绑定);寿命必须 ≤300 秒;允许 30 秒时钟偏移。任何自由格式身份
字段(`player_name` 等)出现在请求里直接 400 —— 身份只来自 claim。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/internal/v1/meta` | **唯一免鉴权**:protocol / engine_version / db_format(健康探针) |
| GET | `/internal/v1/state` | 世界快照 + 本 membership 视图(recent_events/narrative 截尾 100) |
| GET | `/internal/v1/context/{agent_id}` | 有界 grounding:记忆截 8 条×500 字 + 在场 + 关系 |
| POST | `/internal/v1/chat` | 代玩家聊天。body:`agent_id`、`messages`(1~20 条,role ∈ user/assistant,末条必须 user,每条 ≤4000 字);流式返回;不落平台历史 |
| POST | `/internal/v1/chat-evolution` | 回传一个已完成回合(恰好 user→assistant 2 条)。带 `delivery_id` 幂等:重复投递返回原状态 + `duplicate:true`,指纹冲突 409;失败的投递可安全重试 |
| POST | `/internal/v1/commands` | 命令白名单 `noop / player_move / player_action`,带 `command_id` 幂等回执;`player_move` 目标必须是 point 地点 |
| GET | `/internal/v1/stream` | SSE:首帧 `event: ready` 确立身份,后续世界事件 |

### 4.3 `/api/admin/v1/*` —— 运维台

**鉴权**:`Authorization: Bearer <admin_token>`(环境变量 `ANIMA_WORLD_ADMIN_TOKEN`),
常量时间比对;未配置时一律 401(fail-closed)。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/v1/runtime` | 世界身份 + endpoint / db 路径 / tick / 暂停位 / tick_rate |
| POST | `/api/admin/v1/evolution/pause` / `resume` | 暂停/恢复世界演化 |
| POST | `/api/admin/v1/server/stop` | 202 + 后台优雅停机(未配置回调则 503) |
| GET | `/api/admin/v1/database/tables` | 库内全部表 + 行数 |
| GET | `/api/admin/v1/database/tables/{name}` | 只读分页浏览(`offset` / `limit`≤500);表名白名单;blob 转 base64 |

---

## 5. 环境变量

| 变量 | 用途 |
|---|---|
| `ANIMA_WORLD_ADMIN_TOKEN` | admin API token(serve 读取,变量名可用 `--world-admin-token-env` 改) |
| `ANIMA_WORLD_SERVICE_TOKEN` | 平台服务凭证,逗号分隔多个 |
| `ANIMA_MEMBERSHIP_CLAIM_SECRET` | claim 签名密钥(与服务凭证必须成对) |
| `ANIMA_SETTINGS_KEY` | Fernet 密钥(优先于 `world.db.key` 文件) |
| `ANIMA_LLM_API_KEY` / `OPENAI_API_KEY` / `LONGCAT_API_KEY` | 仅首启播种 `llm.api_key` 时读取 |
| `ANIMA_LLM_BASE_URL` / `OPENAI_BASE_URL` | 仅首启播种 `llm.base_url` |
| `ANIMA_LLM_MODEL` / `OPENAI_MODEL` | 仅首启播种 `llm.model` |
| `NO_COLOR` | 关闭 CLI 彩色输出 |

只设 `LONGCAT_API_KEY` 时自动播种 LongCat 端点与模型。**首启之后 `llm.*` 一律以 db
配置为准**,环境变量不再被读。

## 6. 配置键参考

`anima-world config list` / `GET /api/config` 可见,全部支持热更新。

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
| `memory.capacity` | int | 50 | 每角色记忆容量(anchor 不占淘汰) |
| `memory.sentiment_threshold` | float | 0.3 | 关系变动触发记忆的阈值 |

## 7. 提示词模板

`GET /api/prompts` 可见、`PUT` 可改、live 生效。清单:

`chat.system_persona`(角色人设)· `chat.memory_block` / `chat.world_memory_block` /
`chat.presence_block` / `chat.relation_block`(四个 grounding 块)· `chat.session_summary`
(会话摘要)· `narrative.describe`(叙事)· `planner.freetime`(自由时间规划)·
`judge.relationship` / `judge.user_relationship` / `judge.relabel`(关系判定三件套)·
`world.setting`(世界观,**原样使用不做 format**,可放字面量 `{}`)。

## 8. 数据文件

**一个世界 = 一个卷**,包含:

| 文件 | 说明 |
|---|---|
| `world.db` | SQLite(WAL):事件、快照、聊天、记忆、图谱、配置、提示词、地图、行为树、格式戳 |
| `world.db.key` | Fernet 密钥,**搬迁必须随行**;丢失 = secret 永久读不出(降级 Mock,但会点名) |
| `world_seed.json` | 种子:`agents`(id/name/location/personality,可选 duties/goals)、`locations`(嵌套邻接树,region 带 x/y/w/h、point 带 x/y,相对父区域 0~1)、可选 `relations`/`memories`。畸形条目降级跳过,永不阻断启动 |
| `beats.json` | 可选节拍脚本(见 §9) |

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
