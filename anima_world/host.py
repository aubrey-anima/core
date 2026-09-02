"""主持人:世界永远先开口(3.9.0,2026-09-02 裁决 §2.1)。

玩家点进一个世界,从前看到的是名册、地图,和**一个空白的聊天框** —— 他不知道自己
是谁、该找谁、说什么、说了会怎样。跑团桌上从来没有这个问题,因为 GM 永远先开口:
「你们站在英灵殿门口,门房抬头看了你一眼。你要做什么?」**场景 → 选项 → 后果。**

这个模块只做前两拍里**纯算术**的那一半:从候选池里挑 3–5 项,并把要交给 LLM 的那几句
话拼出来。它不认识 Redis、不认识 `World`,所以挑法可以被单独测。

## 三条这一层的纪律

1. 🔴 **挑哪几项是纯算术,LLM 只写字。** 这不是新发明,是 `contact.py` 已经写下的
   那句:「LLM 在这一层没有否决权,它只写那句线索……给它否决权还有第二个坏处:
   有 key 的世界和没 key 的世界会有两套行为,而差别只在一个环境变量上。」
   可验的说法:**同一个世界同一时刻挑两次,逐项相同**。
2. 🔴 **每一项都指向今天已经存在的那扇门**(`door`)。主持人是**荐者不是执行者**,
   它一条新的"写世界"的路都不开。这一条买到三样:壳只加一扇门、网站不必在前端拼
   第二套「能做什么」、引擎侧零新增写路径。
3. 🔴 **藏起来的人一个字都不许漏。** `card.billing == "hidden"` 的角色不进候选,
   **而且根本不进给 LLM 的那份提示** —— 不是"给了再叮嘱它别说"。理由是结构性的:
   那三扇结构化的门(roster / perception / player-options)壳能按行筛掉藏起来的人,
   而主持人交出去的是**散文**,名字是模型写进去的,壳筛不了;**筛一半比不筛更坏**。
   落法是让这份提示**没有别的名字来源** —— 它只由已经筛过的那几项拼出来。
"""

from __future__ import annotations

from typing import Any, Iterable

# 四个时刻。**这不是一份说明,是引擎里那道闸的取值** —— `World.host_turn` 只在
# 时刻钥匙变了的时候开口,别处没有第二条生成场景的路。
HOST_MOMENTS = ("arrive", "new_day", "beat", "ask")
OPTION_KINDS = ("invitation", "beat", "talk", "verb", "travel", "free")
# 点下去走哪条**今天已经有**的门。闭集 —— 多一种就是多一条写世界的路。
DOOR_METHODS = ("answer_invitation", "chat", "player_walk", "player_tool", "free")
# 心流那三挑的标记(一个安全的、一个有风险的、一个社交的)。**不是难度数值** ——
# 引擎里没有难度这个东西,给它一个数字等于凭空发明一份世界不认识的真相。
TONES = ("safe", "risky", "social")

FREE_OPTION_ID = "opt:free"
FREE_LABEL = "自己说点什么……"

# 候选排序里各类的先后。**故事线在前,杂事在后**:一个玩家点进来最该看到的是
# 「有人在等你答话」和「你这条线走到哪儿了」,不是「你可以端详布告栏」。
_KIND_RANK = {kind: i for i, kind in enumerate(OPTION_KINDS)}


def free_option() -> dict[str, Any]:
    """自由输入那一项。**永远在,永远最后,而且不占 `host.max_options` 的名额。**

    跑团的规矩正是这样:GM 给选项,玩家可以不选,但 GM 先说话。把它算进名额里,
    一个选项多的时刻就会把"我想说点别的"挤掉 —— 而那恰恰是这一层要保住的自由。
    """
    return {
        # 键序照 `contract.host.option_keys` —— **JSON 键序不是契约,而"我自己声明了
        # 一张有序表、自己的产出却不照它"是另一回事**:一个照表写解析器的人会先
        # 怀疑自己。别的项都是同序的,就这一项从前把 `who` 排在 `hook` 后面。
        "id": FREE_OPTION_ID, "kind": "free", "label": FREE_LABEL, "who": "", "hook": "",
        "tone": "social", "available": True, "reason": "", "refusal": "", "cost": "",
        "door": {"method": "free", "params": {}},
    }


def select_options(candidates: Iterable[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    """从候选池里挑不超过 `limit` 项,**确定且可复现**,末尾补上自由输入。

    挑法(照设计稿 §3.2 的起点:一个安全的、一个有风险的、一个社交的):
    先按 (类别, id) 排成一个**确定**的池子,再各取一个三种口味的,剩下的名额按池子
    顺序补。**排序按 id 而不是"最有意思的那几个"** —— 和 perception 的截断同一条
    理由:要的是确定,而不确定的挑法会让同一个世界每次给他不同的现实。
    """
    pool = sorted(candidates, key=lambda o: (_KIND_RANK.get(o["kind"], 99), o["id"]))
    chosen: list[dict[str, Any]] = []
    seen: set[str] = set()
    cap = max(0, int(limit))
    for tone in TONES:
        if len(chosen) >= cap:
            break
        for option in pool:
            if option["id"] in seen or option.get("tone") != tone:
                continue
            chosen.append(option)
            seen.add(option["id"])
            break
    for option in pool:
        if len(chosen) >= cap:
            break
        if option["id"] not in seen:
            chosen.append(option)
            seen.add(option["id"])
    chosen.sort(key=lambda o: (_KIND_RANK.get(o["kind"], 99), o["id"]))
    chosen.append(free_option())
    return chosen


def mock_scene(*, place_name: str, day: int, hour: int,
               options: list[dict[str, Any]], going_to: str = "") -> str:
    """没有 key / LLM 挂了 / 超时时那一段话。

    **没配 key 是这个引擎的默认状态**,所以这不是降级路径上的边角料,而是很多人看到
    的第一屏(#15 那一课)。它只用**已经筛过**的那几项拼,和真提示词同一个来源 ——
    两边各拼一份的话,mock 迟早会说出一个藏起来的人的名字。
    """
    when = "清晨" if hour < 9 else "上午" if hour < 12 else "下午" if hour < 18 else "夜里"
    # ⚠️ **他可能还没落脚**(刚进来、还没走过一步)。拿一个空的地名去拼,出来的是
    # 「你在。」—— 一句念不通的话,而这个仓库的口径是**一句念不通的拒绝语,和一句
    # 错的一样贵**。这一格是拿真 CLI 敲出来的,不是想出来的。
    if going_to:
        # 在路上的人不该被告知"你在出发地" —— 那正是两扇门说两句话的那一格。
        return f"第 {day} 天{when},你在去{going_to}的路上。到了地方再说。"
    if not place_name:
        return f"第 {day} 天{when}。你还没落个脚 —— 先挑个地方站过去。"
    head = f"第 {day} 天{when},你在{place_name}。"
    # ⚠️ **拿 `who` 那一格,不是 `label`**:label 是一句祈使("和苏晚夏说说话"),
    # 拼进"这儿有人:…"就成了「这儿有人:和苏晚夏说说话。」—— **一句念不通的话**。
    # 候选自己带着人名,这里就不该再从按钮上的字里去抠。
    people = [o["who"] for o in options
              if o["kind"] in ("talk", "invitation") and o.get("who")]
    if people:
        head += "这儿有人:" + "、".join(people[:3]) + "。"
    elif len(options) > 1:
        head += "四下没什么人。"
    return head + "你要做什么?"


def scene_messages(*, place_name: str, place_desc: str, day: int, hour: int,
                   minute: int, world_setting: str,
                   options: list[dict[str, Any]], going_to: str = "") -> list[dict[str, str]]:
    """交给背景槽的那一次调用。**一次调用同时写场景那段话和每一项的钩子。**

    🔴 **这份提示里没有第二个名字来源。** 它只由 `options` 拼出来,而 `options` 已经
    按 `billing` 筛过 —— 藏起来的人不在里面,所以模型手上根本没有他的名字。
    这比"给了再让它别说"强的地方在于:后者失手一次就漏了,而且漏在一段散文里,
    宿主筛不掉;这里失手需要模型凭空编出一个它没见过的名字。
    """
    listed = "\n".join(
        f"{i}. {o['label']}" for i, o in enumerate(options, 1) if o["kind"] != "free"
    )
    system = (
        "你是一个文字冒险游戏的主持人。用中文写,口吻克制、具体、有画面,"
        "**不要**替玩家做决定,**不要**替任何角色说出成段的台词。"
    )
    user = (
        (f"世界:{world_setting}\n" if world_setting else "")
        + (f"地点:他在去{going_to}的路上,还没到。\n" if going_to
           else f"地点:{place_name}。{place_desc}\n" if place_name
           else "地点:他还没落脚,不在任何地方。\n")
        + f"时间:第 {day} 天 {hour:02d}:{minute:02d}\n"
        + (f"此刻他可以做的事:\n{listed}\n" if listed else "此刻他没有什么特别能做的。\n")
        + "\n请按下面的格式输出,不要有别的字:\n"
        + "第一行:一段 30–80 字的开场,说清他在哪、看得见什么、气氛怎样。\n"
        + "之后每行一句不超过 20 字的钩子,顺序对应上面那几件事,"
        + "写它此刻**看上去**是什么样,不要写结果。\n"
        + "⚠️ 只许提到上面出现过的人和地方,不许写出别的名字。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_scene_reply(text: str, *, options: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """把模型那几行拆成 `(场景, 每一项的钩子)`。**读不懂就少给,绝不猜。**

    对不齐的时候按顺序尽量填,填不满的那几项钩子是空串 —— 而空钩子是合法的
    (`hook` 本来就可空)。硬要求模型给出严格 JSON 是这一层最容易碎的地方:
    一次格式失手就是整屏没有场景,而这一屏正是玩家点进来看到的第一样东西。
    """
    lines = [line.strip() for line in str(text or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return "", []
    scene = lines[0]
    hooks: list[str] = []
    wanted = len([o for o in options if o["kind"] != "free"])
    for line in lines[1:]:
        # 模型爱写「1. …」「钩子 2:…」——去掉序号,留那句话。
        for sep in (". ", "、", ":", ":"):
            head, found, tail = line.partition(sep)
            if found and len(head) <= 6 and any(ch.isdigit() for ch in head):
                line = tail.strip()
                break
        if line:
            hooks.append(line)
    return scene, hooks[:wanted]
