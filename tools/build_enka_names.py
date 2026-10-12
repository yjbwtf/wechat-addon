"""生成 assets/enka_names.json：角色名 + 圣遗物套装名 + 词条名 + 物品名。

## 为什么需要它

Enka 的接口只返回数字/哈希，**名字全靠映射表**（官方文档自己说这是
「最大的挑战」）。完整 store 有 2MB+，但真正要用的只有四张表，
加起来十几 KB，所以这里压成一张紧凑表跟着仓库走；出新角色/新套装时重跑。

## 数据来源

用 **api.enka.network 的 store 镜像**，不是 GitHub raw —— 实测这个镜像
直连就能拉（不需要代理），换台机器也能跑。

## 四张表分别从哪来（都是实测出来的，不是猜的）

| 表 | 来源 | 规模 |
|---|---|---|
| `avatar` 角色名 | 见下面「三个游戏的字段不一样」 | 312 |
| `set` 圣遗物套装名 | `store/gi/relics.json` 的 `Sets[setId].Name` -> locs | 65 |
| `prop` 词条名（暴击率…） | `store/gi/locs.json` 里的 `FIGHT_PROP_*` **字符串键** | 52 |
| `text` 物品名（武器…） | `store/gi/locs.json` 里的**数字键** | 444 |

注意：`locs.json` 的 zh-cn 只有 501 条，**不含**单件圣遗物的名字
（那有几千件）。所以圣遗物按「套装 + 部位」显示，这也是玩家的习惯叫法。

## 三个游戏的角色名字段不一样

| 游戏 | avatars 里的名字字段 | 本地化表 |
|---|---|---|
| 原神   | `NameTextMapHash`（**数字** hash） | store/gi/locs.json |
| 星穹铁道 | `AvatarName.Hash`（**字符串** hash） | store/hsr/hsr.json |
| 绝区零 | `Name`（**本地化 key**，不是 hash） | store/zzz/locs.json |

**踩过一次**：store 下还有一个 `store/ef/`，键是 4/6/8 这种小整数、
字段叫 `NameHash`。第一版我把它当成了绝区零，生成出来的名字是
「弧光/余烬」这种 —— 全是错的。真正的绝区零是 `store/zzz/`（键是 1011 这种）。

用法：
    venv\\Scripts\\python.exe tools\\build_enka_names.py
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "enka_names.json"
MIRROR = "https://api.enka.network/"
UA = {"User-Agent": "wechat-addon/0.1 (personal wechat bot)"}
LANG = "zh-cn"

#: 游戏 -> (avatars 路径, 本地化表路径, 从 avatars 项里取名字键的路径)
SPEC = {
    "gi": ("store/gi/avatars.json", "store/gi/locs.json", "NameTextMapHash"),
    "hsr": ("store/hsr/avatars.json", "store/hsr/hsr.json", "AvatarName.Hash"),
    "zzz": ("store/zzz/avatars.json", "store/zzz/locs.json", "Name"),
}


def fetch(path: str) -> dict:
    url = MIRROR + path
    print("  拉 %s ..." % url, end="", flush=True)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode("utf-8"))
    print(" %d 项" % len(data))
    return data


def dig(item: dict, dotted: str):
    """按 "AvatarName.Hash" 这种路径取值。取不到返回 None。"""
    cur = item
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def main() -> int:
    out: dict[str, dict] = {}

    # ---- 角色名 -------------------------------------------------------
    for game, (av_path, loc_path, field) in SPEC.items():
        print("%s:" % game)
        avatars = fetch(av_path)
        locs = fetch(loc_path)
        table = locs.get(LANG) or {}
        if not table:
            print("  !! %s 里没有 %s，跳过" % (loc_path, LANG))
            continue
        names: dict[str, str] = {}
        missing = 0
        for aid, item in avatars.items():
            h = dig(item, field)
            if h is None:
                missing += 1
                continue
            name = table.get(str(h))
            if name:
                names[aid] = name
            else:
                missing += 1
        out[game] = {"avatar": names}
        print("  -> 角色 %d 个有名字，%d 个查不到" % (len(names), missing))
        print()

    # ---- 原神的套装 / 词条 / 物品名 ------------------------------------
    # 这三张表只有原神要（崩铁/绝区零的装备结构不同，各有各的表）
    print("gi 的套装/词条/物品名:")
    locs_gi = fetch("store/gi/locs.json")[LANG]
    sets: dict[str, str] = {}
    for sid, info in (fetch("store/gi/relics.json").get("Sets") or {}).items():
        name = locs_gi.get(str((info or {}).get("Name")))
        if name:
            sets[sid] = name
    props = {k: v for k, v in locs_gi.items() if k.startswith("FIGHT_PROP")}
    texts = {k: v for k, v in locs_gi.items() if k.isdigit()}
    out["gi"]["set"] = sets
    out["gi"]["prop"] = props
    out["gi"]["text"] = texts
    print("  -> 套装 %d 个 / 词条 %d 个 / 物品名 %d 个" % (
        len(sets), len(props), len(texts)))
    print()

    # ---- 崩铁：一张大表全包 -------------------------------------------
    # 光锥名、遗器套装名、词条名都是**同一个 zh-cn 表**里的 hash/字符串键，
    # 所以直接整张存下来（479 条），不用拆。
    print("hsr 的文本表:")
    out["hsr"]["text"] = fetch("store/hsr/hsr.json")[LANG]
    print("  -> %d 条（光锥名 + 遗器套装名 + 词条名共用）" % len(out["hsr"]["text"]))
    print()

    # ---- 绝区零：音擎 / 驱动盘套装 / 词条 ------------------------------
    print("zzz 的装备表:")
    locs_zzz = fetch("store/zzz/locs.json")[LANG]

    def loc(key) -> str:
        return locs_zzz.get(str(key)) or ""

    weapon: dict[str, dict] = {}
    for wid, info in fetch("store/zzz/weapons.json").items():
        name = loc((info or {}).get("ItemName"))
        if name:
            weapon[wid] = {"n": name, "r": (info or {}).get("Rarity")}

    # 驱动盘：单个盘 Id -> (中文套装名, 稀有度)。
    # **稀有度必须一起存** —— 主词条的数值要按
    # `值 * (1 + 等级 * 稀有度系数)` 算（见 src/enka.py 里的说明）
    eq = fetch("store/zzz/equipments.json")
    suits = {}
    for sid, info in (eq.get("Suits") or {}).items():
        name = loc((info or {}).get("Name"))
        if name:
            suits[sid] = name
    disc = {}
    for did, info in (eq.get("Items") or {}).items():
        name = suits.get(str((info or {}).get("SuitId")))
        if name:
            disc[did] = {"s": name, "r": (info or {}).get("Rarity")}

    # 词条：Name 是本地化 key，Format 里带 % 就是百分比
    # （比硬编码哪些词条是百分比靠谱 —— 这是数据自己说的）
    prop = {}
    for pid, info in fetch("store/zzz/property.json").items():
        name = loc((info or {}).get("Name"))
        if name:
            prop[pid] = {"n": name,
                         "p": 1 if "%" in str((info or {}).get("Format") or "") else 0}

    out["zzz"]["weapon"] = weapon
    out["zzz"]["disc"] = disc
    out["zzz"]["suit"] = suits
    out["zzz"]["prop"] = prop
    print("  -> 音擎 %d / 驱动盘 %d / 套装 %d / 词条 %d" % (
        len(weapon), len(disc), len(suits), len(prop)))
    print()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True),
                   encoding="utf-8")
    total = sum(len(v) for game in out.values() for v in game.values())
    print("已写入 %s：%d 个游戏 / 一共 %d 条 / %d 字节"
          % (OUT.relative_to(ROOT), len(out), total, OUT.stat().st_size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
