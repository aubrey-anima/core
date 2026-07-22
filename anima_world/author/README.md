# anima-author — 创作工作台

`anima-world` 包里的创作侧:把一部小说变成一个可运行的世界。
产出物是数据(`.cyberworld` 数据包 / world_seed / beats),消费方是运维层(导入)与世界运行时(播种)——无运行态耦合。

创作台与引擎同包发布:一次安装既能创作世界,也能打包和运行它。

## 结构

```
author/
├── app.py       创作台 FastAPI(/author/v1):作业上传/扫描进度/实体卡片/物化;
│                内嵌数据库编辑器(db_editor_port/url 由部署方注入)
├── extract.py   小说分块 + LLM 扫描(章节→实体/关系/素材,断点可续、逐块重试)
├── distill.py   蒸馏(素材→人设/记忆/关系定稿)+ 种子装配
├── generate.py  一句话概念 → 世界种子(CLI: author generate)
├── store.py     作业库(author.db:jobs/chunks/entities/notes/relations)
└── static/index.html  单页工作台前端(上传→进度→卡片→物化,含「数据库」区块)
```

## 流水线(数据链路)

```
小说.txt ─上传→ author.db(job) ─分块扫描(LLM)→ entities/relations/lore
        ─蒸馏(LLM)→ 定稿人设/记忆/关系 ─装配→ world_seed(+beats)
        ─物化→ .cyberworld 数据包 ──→ 运维台导入 → 世界容器首启自播种
```

- 唯一持久化:`author.db` + `<data-dir>/novels/*.txt`(原文)、`seeds/`、`worlds/`(产出)
- LLM 客户端注入(`llm_factory`),永不在请求线程调用;扫描在工作线程池,断点续扫

## 依赖的引擎模块(同包)

- `anima_world.world_seed.is_valid_world_seed` —— 种子 schema 校验
- `anima_world.beats.BeatScript` —— 节拍脚本严格校验(坏脚本必须在创作台报错,不能流到世界启动)
- `anima_world.world_package` —— `.cyberworld` 数据包 export

这三个同时也是对外冻结的格式契约:运维台持有它们的 JS 镜像实现,两边靠
`src/platform/test/contract.test.js` 双向互验。改这里的格式 = 改跨 surface 契约。

## 运行

```bash
anima-author serve --host 127.0.0.1 --port 8402       # 等价 anima-world author serve
anima-author generate --concept "雨季不停的港口小镇" --output seed.json
```
