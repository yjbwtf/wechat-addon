"""生成 assets/enka_names.json：角色 ID -> 中文名。

## 为什么需要它

Enka 的接口只返回 `avatarId` 数字，**名字要靠一张映射表**（官方文档自己说
这是「最大的挑战」）。完整 store 有 1.5MB（avatars + locs 六个文件），
塞进仓库不合适；但真正要用的只有「ID -> 名字」这一列，加起来 5KB。

所以这里把完整 store 压成一张紧凑表，跟着仓库走；出新角色时重跑一次本脚本。

## 数据来源

用 **api.enka.network 的 store 镜像**，不是 GitHub raw —— 实测这个镜像
直连就能拉（不需要代理），换台机器也能跑。

## 三个游戏的字段名不一样（实测，不是猜的）

| 游戏 | avatars 里的名字字段 | 本地化表 |
|---|---|---|
| 原神   | `NameTextMapHash`（**数字** hash） | store/gi/locs.json  |
| 星穹铁道 | `AvatarName.Hash`（**字符串** hash） | store/hsr/hsr.json |
| 绝区零 | `Name`（**本地化 key**，不是 hash） | store/zzz/locs.json  |

- 崩铁没有 locs.json，名字在 hsr.json 里；哈希是字符串
- 绝区零的名字字段直接是 key，例如 `Avatar_Female_Size02_Anbi` -> 安比
- 统一 `str()` 之后再查，三个游戏走同一段代码

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
#: 注意绝区零是 store/zzz，不是 store/ef（ef 是另一个东西，见文件头注释）
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
    out: dict[str, dict[str, str]] = {}
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
        out[game] = names
        print("  -> %d 个角色有名字，%d 个查不到" % (len(names), missing))
        print()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True),
                   encoding="utf-8")
    total = sum(len(v) for v in out.values())
    print("已写入 %s：%d 个游戏 / %d 个角色 / %d 字节"
          % (OUT.relative_to(ROOT), len(out), total, OUT.stat().st_size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
