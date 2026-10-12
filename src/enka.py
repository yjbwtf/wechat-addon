"""Enka.Network 账号查询：米哈游游戏的**公开角色展柜**。

## 为什么单独一条指令、不走 LLM

「按 UID 查一个账号」是确定性的：一次 HTTP GET + 一段固定格式的文字。
走 agent 要多花一次模型调用、几十秒，而且 agent 没法遵守这个 API 的规矩
（见下）。这和 `\\开` / `\\刷课` 是同一个思路：确定的事就不要过模型。

## 这个 API 的规矩（作者写在文档里的，必须遵守）

1. **不许枚举 UID、不许批量查**。原文是「不要为了填数据库去大量查询」。
   所以这里没有「遍历 UID」之类的东西，只有单次查询 + 缓存。
2. **必须带自定义 User-Agent**，方便作者定位问题。
3. UID 端点有动态限流，打太快会 429（还会被 MHY 那边限）。
4. 响应里的 `ttl` = 「下次真的去问 MHY」的秒数。**这段时间内要用本地缓存** ——
   反复打同一个 UID 照样吃限流。作者建议「用 Redis 缓存 ttl」，
   这里用 data/enka_cache.json 代替。

据此本模块自带三道理：**ttl 缓存**、**调用间隔下限**、**每小时上限**。

## 名字映射

接口只返回 `avatarId` 数字，名字靠 assets/enka_names.json（312 个角色，8KB）。
那张表由 tools/build_enka_names.py 从官方 store 生成 ——
三个游戏的字段名各不相同，详见那个脚本的注释。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import mona

ROOT = Path(__file__).resolve().parent.parent
NAMES_PATH = ROOT / "assets" / "enka_names.json"

#: 作者要求带自定义 UA —— 别用 python-urllib 的默认值
USER_AGENT = "wechat-addon/0.1 (personal wechat bot)"

GAME_LABEL = {"gi": "原神", "hsr": "星穹铁道", "zzz": "绝区零"}
ENDPOINT = {
    "gi": "https://enka.network/api/uid/{uid}/",
    "hsr": "https://enka.network/api/hsr/uid/{uid}/",
    "zzz": "https://enka.network/api/zzz/uid/{uid}/",
}
#: 用户可能怎么叫这几个游戏。
#:
#: 按「大家实际会打的字」收，不是按官方全名收：口语简写（崩铁/星铁/绝区）、
#: 拼音缩写（hsr/zzz/ys）、官方名**带冒号**的写法（崩坏：星穹铁道）、
#: 以及常见错字（元神）。反正这几个词当角色名是不可能的，多认几个不亏。
ALIASES = {
    # ---- 原神 ----
    "gi": "gi", "genshin": "gi", "genshinimpact": "gi", "ys": "gi",
    "原神": "gi", "原": "gi", "元神": "gi", "提瓦特": "gi",
    # ---- 崩坏：星穹铁道 ----
    "hsr": "hsr", "starrail": "hsr", "honkai": "hsr",
    "honkaistarrail": "hsr", "sr": "hsr", "bht": "hsr",
    "崩铁": "hsr", "星铁": "hsr", "星穹": "hsr", "铁道": "hsr",
    "星轨": "hsr", "星穹铁道": "hsr", "崩坏星穹铁道": "hsr",
    # 官方名里是有冒号的（崩坏：星穹铁道），全角半角都收
    "崩坏：星穹铁道": "hsr", "崩坏:星穹铁道": "hsr",
    # ---- 绝区零 ----
    "zzz": "zzz", "zenless": "zzz", "zenlesszonezero": "zzz",
    "jql": "zzz", "绝区零": "zzz", "绝区": "zzz", "绝": "zzz",
}

_DATA: dict | None = None


class EnkaError(RuntimeError):
    """查不到 / 被限流 / 服务端出错 —— message 是可以直接发到群里的中文。"""


# ------------------------------------------------------------------ 名字表
def _load() -> dict:
    global _DATA
    if _DATA is None:
        try:
            _DATA = json.loads(NAMES_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logging.warning("读不到 %s（%s），名字会显示成 ID", NAMES_PATH, exc)
            _DATA = {}
    return _DATA


def names(game: str) -> dict[str, str]:
    """角色 ID -> 中文名。文件缺失时返回空表（照常显示 ID，不崩）。"""
    return (_load().get(game) or {}).get("avatar") or {}


def avatar_name(game: str, avatar_id) -> str:
    return names(game).get(str(avatar_id)) or "ID:%s" % avatar_id


def _gi_table(kind: str) -> dict:
    return (_load().get("gi") or {}).get(kind) or {}


def gi_set_name(set_id) -> str:
    """圣遗物套装 ID -> 套装名（角斗士的终幕礼）。

    接口只给 `setId`（15001 这种），名字要靠 store/gi/relics.json 的
    `Sets[setId].Name` 再查一次本地化表 —— 两个 hash 都不是响应里那个
    `setNameTextMapHash`（那个在这个 locs 表里查不到，实测的）。
    """
    return _gi_table("set").get(str(set_id)) or "套装%s" % set_id


def gi_prop(prop_id) -> str:
    """FIGHT_PROP_CRITICAL -> 暴击率。查不到就原样显示，不编。"""
    return _gi_table("prop").get(str(prop_id)) or str(prop_id)


def gi_text(text_hash) -> str:
    """物品名（武器等）。查不到返回空串，让调用方自己兜。"""
    return _gi_table("text").get(str(text_hash)) or ""


#: 圣遗物部位。接口给的是 EQUIP_XXX，这五个是固定的，不用查表
_SLOTS = {
    "EQUIP_BRACER": "生之花", "EQUIP_NECKLACE": "死之羽",
    "EQUIP_SHOES": "时之沙", "EQUIP_RING": "空之杯",
    "EQUIP_DRESS": "理之冠",
}

#: 这些词条的值是百分比（11.7 表示 11.7%），其余是绝对值（4780）
_PERCENT_PROPS = {
    "FIGHT_PROP_HP_PERCENT", "FIGHT_PROP_ATTACK_PERCENT",
    "FIGHT_PROP_DEFENSE_PERCENT", "FIGHT_PROP_CRITICAL",
    "FIGHT_PROP_CRITICAL_HURT", "FIGHT_PROP_CHARGE_EFFICIENCY",
    "FIGHT_PROP_HEAL_ADD", "FIGHT_PROP_FIRE_ADD_HURT",
    "FIGHT_PROP_ELEC_ADD_HURT", "FIGHT_PROP_WATER_ADD_HURT",
    "FIGHT_PROP_GRASS_ADD_HURT", "FIGHT_PROP_WIND_ADD_HURT",
    "FIGHT_PROP_ROCK_ADD_HURT", "FIGHT_PROP_ICE_ADD_HURT",
    "FIGHT_PROP_PHYSICAL_ADD_HURT", "FIGHT_PROP_SKILL_CD_MINUS_RATIO",
}

#: 详细视图里把长词条名缩短，否则一行放不下四个副词条
_SHORT_PROP = {
    "攻击力": "攻击", "攻击力%": "攻击", "生命值": "生命", "生命值%": "生命",
    "防御力": "防御", "防御力%": "防御", "暴击率": "暴击", "暴击伤害": "爆伤",
    "元素充能效率": "充能", "元素精通": "精通", "治疗加成": "治疗",
    "物理伤害加成": "物伤",
}


def _stat_value(prop: str, value) -> str:
    """词条数值的写法：百分比加 %，绝对值直接写。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if prop in _PERCENT_PROPS:
        return "%.1f%%" % num
    return "%g" % num


def _short(prop: str) -> str:
    name = gi_prop(prop)
    return _SHORT_PROP.get(name, name)


def _set_summary(arts: list[dict]) -> str:
    """把 5 件圣遗物压成「角斗士的终幕礼4 幸运儿1」。

    按件数从多到少排；同样件数按名字排，保证同一个号每次输出一样
    （不然测试没法钉）。
    """
    count: dict[str, int] = {}
    for a in arts:
        count[a["set"]] = count.get(a["set"], 0) + 1
    items = sorted(count.items(), key=lambda kv: (-kv[1], kv[0]))
    return " ".join("%s%d" % (n, c) if c > 1 else n for n, c in items)


def _parse_build(avatar: dict) -> tuple[dict | None, list[dict]]:
    """从一个 avatarInfoList 元素里取出武器和 5 件圣遗物。

    **只对原神做过**（崩铁/绝区零的装备 store 结构不同，字段名也不同，
    没有真实样本之前不猜 —— 宁可少显示，也不要显示错的）。
    """
    weapon = None
    arts: list[dict] = []
    for e in avatar.get("equipList") or []:
        flat = e.get("flat") or {}
        kind = flat.get("itemType")
        if kind == "ITEM_WEAPON":
            w = e.get("weapon") or {}
            # affixMap 的值是「精炼等级 - 1」：0 -> 精1，2 -> 精3
            affix = (w.get("affixMap") or {}).values()
            refine = (max(affix) + 1) if affix else None
            weapon = {"name": gi_text(flat.get("nameTextMapHash")) or "?",
                      "level": w.get("level"), "refine": refine,
                      "rank": flat.get("rankLevel")}
        elif kind == "ITEM_RELIQUARY":
            rel = e.get("reliquary") or {}
            main = flat.get("reliquaryMainstat") or {}
            arts.append({
                "slot": _SLOTS.get(flat.get("equipType"), flat.get("equipType")),
                "set": gi_set_name(flat.get("setId")),
                "level": rel.get("level"),
                "lv": _equip_level("gi", rel.get("level")),
                "rank": flat.get("rankLevel"),
                "main": _short(main.get("mainPropId")),
                "main_val": _stat_value(main.get("mainPropId"),
                                        main.get("statValue")),
                "subs": [(_short(s.get("appendPropId")),
                          _stat_value(s.get("appendPropId"), s.get("statValue")))
                         for s in (flat.get("reliquarySubstats") or [])],
                # 算「有效词条」分要用**原始** prop + 数值，不能用上面那串
                # 格式化过的文本（名字被缩短过、百分号也混进去了）。
                # 单位在这里就对齐好（百分比词条 /100），否则分数会大 100 倍。
                "subs_raw": [(s.get("appendPropId"),
                              _norm_value(s.get("appendPropId"),
                                          s.get("statValue")))
                             for s in (flat.get("reliquarySubstats") or [])],
            })
    # 按部位固定顺序排（花/羽/沙/杯/冠），接口给的顺序不保证
    order = {v: i for i, v in enumerate(_SLOTS.values())}
    arts.sort(key=lambda a: order.get(a["slot"], 99))
    return weapon, arts


def hsr_text(key) -> str:
    """崩铁的文本表：光锥名、遗器套装名、词条名**共用同一张**。

    光锥和遗器套装的名字都是 hash **字符串**（8516006325888673169 这种），
    词条名是 "CriticalChance" 这种语义字符串 —— 都在 store/hsr/hsr.json 的
    zh-cn 里，所以整张存下来就够，不用拆成三张。
    """
    return ((_load().get("hsr") or {}).get("text") or {}).get(str(key)) or ""


def _hsr_stat_value(prop: str, value) -> str:
    """崩铁的词条值：`*Delta` 是**绝对值**，其余是比例（0.1512 = 15.12%）。

    实测过：HPDelta/AttackDelta/DefenceDelta/SpeedDelta 是绝对值，
    HPAddedRatio/CriticalChance/SpeedDelta 之外的全是比例。规则就按后缀来，
    比列一堆词条名可靠。
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if str(prop).endswith("Delta"):
        return "%g" % num
    return "%.1f%%" % (num * 100)


#: 崩铁遗器部位。接口给的是 type 1..6
_HSR_SLOTS = {1: "头", 2: "手", 3: "身", 4: "脚", 5: "位面球", 6: "连结绳"}

#: 绝区零驱动盘部位
_ZZZ_SLOTS = {1: "1号位", 2: "2号位", 3: "3号位",
              4: "4号位", 5: "5号位", 6: "6号位"}


def _zzz_table(kind: str) -> dict:
    return (_load().get("zzz") or {}).get(kind) or {}


def _zzz_stat(prop_id) -> tuple[str, bool]:
    """绝区零词条 -> (中文名, 是不是百分比)。

    **是不是百分比由数据自己说了算**：store 里每条词条带一个 `Format`，
    含 `%` 的就是百分比。比硬编码一张「哪些词条是百分比」的表靠谱得多。
    """
    p = _zzz_table("prop").get(str(prop_id)) or {}
    return p.get("n") or str(prop_id), bool(p.get("p"))


def _zzz_stat_name_value(prop_id, value) -> str:
    """绝区零**副词条**的数值。

    **10000 = 100%**（实测：啄木鸟电音 2 件套的暴击率加成存的是 800，
    正好是 8%），所以百分比要除以 100。

    副词条的公式文档没给，是从数据判出来的：同一个 `PropertyValue` 会配上
    `PropertyLevel` 1/2/3/4（例如 20103 永远是 240），说明它是**每次的量**，
    总量要乘 level（240×4 = 960 -> 9.6%，正是暴击率副词条的满值）。
    """
    name, pct = _zzz_stat(prop_id)
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if pct:
        return "%.1f%%" % (num / 100.0)
    return "%g" % num


def _zzz_sub_value(prop_id, value, count) -> str:
    """绝区零**副词条**的数值 = 每次的量 × 次数。

    实测判据：同一个 `PropertyValue` 会配上 `PropertyLevel` 1/2/3/4
    （例如 20103 永远是 240），所以它是每次的量，不是总量。
    240 × 4 = 960 -> 9.6%，正是暴击率副词条的满值。
    """
    base = _zzz_stat_name_value(prop_id, value)
    try:
        n = float(count or 1)
    except (TypeError, ValueError):
        n = 1.0
    if n <= 1:
        return base
    try:
        total = float(value) * n
    except (TypeError, ValueError):
        return base
    return _zzz_stat_name_value(prop_id, total)


#: 驱动盘主词条的稀有度系数。文档只给了 4/3/2（绝区零的 4 = S 级）
_ZZZ_MAIN_SCALE = {4: 0.2, 3: 0.25, 2: 0.3}


def _zzz_main_value(prop_id, value, level, rarity) -> str:
    """绝区零**主词条**的数值。

    文档给的公式：`结果 = 基础值 * (1 + 等级 * 稀有度系数)`。
    验算过：550 * (1 + 15*0.2) = 2200（S 级 1 号位生命值满值）、
    600 * 4 = 2400 -> 24%（暴击率满值）、750 * 4 = 3000 -> 30%（属性伤害满值）。

    **不做这一步所有主词条都会小 4 倍**（第一版就是这样，
    生命值 550 而实际是 2200）。
    """
    try:
        num = float(value)
        lv = float(level or 0)
    except (TypeError, ValueError):
        return _zzz_stat_name_value(prop_id, value)
    scale = _ZZZ_MAIN_SCALE.get(rarity)
    if scale is None:
        # 表里没有这个稀有度就按 S 级算，并留一条日志 ——
        # 静默按错的比例算比报错还难查
        scale = 0.2
        logging.warning("绝区零驱动盘稀有度 %r 不在系数表里，按 S 级算", rarity)
    name, pct = _zzz_stat(prop_id)
    total = num * (1 + lv * scale)
    if pct:
        return "%.1f%%" % (total / 100.0)
    return "%g" % round(total)


def _region(data: dict) -> str:
    raw = str(data.get("region") or "")
    return _REGION.get(raw.lower(), raw)


#: 每个游戏对「装备」的叫法不同，文案得跟着变，不能写死「圣遗物」
_TERM = {
    "gi": {"weapon": "武器", "equip": "圣遗物"},
    "hsr": {"weapon": "光锥", "equip": "遗器"},
    "zzz": {"weapon": "音擎", "equip": "驱动盘"},
}


def _equip_level(game: str, level) -> str:
    """装备等级的写法。**三个游戏的基准不一样，实测过**：

    | 游戏 | 接口给的值 | 显示 | 为什么 |
    |---|---|---|---|
    | 原神 | 21 | `+20` | 1-based，21 才是满级的 +20 |
    | 崩铁 | 15 | `+15` | 就是 +值本身（满级 15），**不能减 1** |
    | 绝区零 | 15 | `Lv.15` | 同样是直接值；社区写法不统一，用不会歧义的 |

    第一版把原神那套（减 1）套到了崩铁上，所有人的遗器都少显示一级。
    """
    if level is None:
        return ""
    if game == "gi":
        return "+%d" % max(0, int(level) - 1)
    if game == "hsr":
        return "+%d" % int(level)
    return "Lv.%d" % int(level)


def _build_line(name: str, level: str, cons: str, build,
                weights: dict | None = None) -> str:
    """展柜列表里的一行：名字 等级 命座 · 武器 · 套装 · 词条分。"""
    bits = ["%s %s%s" % (name, level, (" " + cons) if cons else "")]
    weapon, arts = build
    if weapon:
        w = weapon["name"]
        if weapon.get("level"):
            w += " %s" % weapon["level"]
        if weapon.get("refine"):
            w += " 精%d" % weapon["refine"]
        bits.append(w)
    if arts:
        bits.append(_set_summary(arts))
        total = [sc for sc in (_art_score(a, weights) for a in arts)
                 if sc is not None]
        if total:
            bits.append("词条分 %.1f" % sum(total))
    return " · ".join(bits)


# ------------------------------------------------------------------ 参数解析
#: 游戏名按长度降序 —— 「崩坏星穹铁道」要在「崩铁」「星铁」之前试
_ALIAS_KEYS = sorted(ALIASES, key=len, reverse=True)

#: UID 的长度范围（Enka 那边也是这个范围，见 parse_args 的校验）
_UID_LEN = (6, 10)


def _alias_of(tok: str) -> str | None:
    """这个 token 是不是游戏名。返回游戏代码或 None。

    允许尾部带标点（「崩坏：」这种写法会把冒号留在词尾）。
    """
    t = str(tok or "").strip()
    for cand in (t, t.rstrip("：:、·，,。.")):
        hit = ALIASES.get(cand.lower())
        if hit:
            return hit
    return None


def _match_game(text: str) -> tuple[str | None, str]:
    """一段文字开头是不是游戏名。返回 (游戏, 剩下的部分)。"""
    low = (text or "").lower()
    for key in _ALIAS_KEYS:
        if low.startswith(key):
            return ALIASES[key], text[len(key):]
    return None, text


def _is_noise(tok: str) -> bool:
    """是不是该丢掉的杂词：**一个中文都没有**的片段。

    角色名都是中文（各游戏的 loc 表里就是中文），所以纯 ASCII 的片段基本是
    命令行习惯的残留：`UID`、`\\uid`（两个反斜杠是老写法，前缀只吃一个，
    会剩一个下来）、`-`、`.` 这些。

    实测踩过两次：
      · `UID137602053` -> 那个 `UID` 混进角色名，回「没找到「UID 可莉」」
      · `\\\\uid 137602053` -> 剩下一个 `\\`，焦点变成 `\\uid`

    带中文的都要留着：`11号`、`银狼LV.999`、`「席德」` 都是真角色名。
    """
    t = str(tok or "").strip()
    if not t:
        return True
    if t.lower() in ALIASES:
        return False
    return not any(ord(ch) > 127 for ch in t)


def parse_args(text: str) -> tuple[str | None, str | None, str, str]:
    """解析 `[游戏] <uid> [角色名]`。返回 (游戏, uid, 角色名, 错误说明)。

    **空格可有可无** —— 这是用户明确要求的：

        原神 137602053 可莉
        原神137602053可莉
        137602053可莉
        gi137602053
        137602053

    找 UID 时**优先挑「长度像 UID」的数字段**（6~10 位），不是第一个数字段 ——
    有些角色名自带数字（绝区零的「11号」、崩铁的「银狼LV.999」），
    先到先得会把 11 当成 UID。实测踩过：`11号 137602053` 报「你给的是 2 位」。

    游戏省略时自动试三个；角色名允许不完整（子串匹配）。
    """
    parts = [p for p in (text or "").replace("，", " ").split() if p]
    if not parts:
        return None, None, "", "要查谁的？给我一个 UID，例如：uid 618285856"

    # 「崩坏： 星穹铁道」这种在冒号后面断开了 —— 先粘回去。
    # 不粘的话那个「崩坏：」会被当成角色名（实测：焦点变成 '崩坏：'）。
    # 只在**下一段确实以游戏名开头**时才粘（「星穹铁道108931973」也算），
    # 免得把正常文字也拼到一起。
    glued: list[str] = []
    i = 0
    while i < len(parts):
        cur = parts[i]
        nxt = parts[i + 1] if i + 1 < len(parts) else ""
        if cur.endswith(("：", ":")) and nxt and (
                _alias_of(nxt) or _match_game(nxt)[0]):
            glued.append(cur + nxt)
            i += 2
            continue
        glued.append(cur)
        i += 1
    parts = glued

    # ---- 1) 先找 UID：优先长度像 UID 的那一段 ---------------------------
    uid: str | None = None
    at: tuple[int, int, int] | None = None      # (第几个词, 起, 止)
    for i, p in enumerate(parts):
        for m in re.finditer(r"\d+", p):
            if _UID_LEN[0] <= len(m.group()) <= _UID_LEN[1]:
                uid, at = m.group(), (i, m.start(), m.end())
                break
        if uid:
            break
    if uid is None:
        # 没有像 UID 的，随便挑一个数字段 —— 为了能说「位数不对」而不是
        # 「我看不懂」，后者对用户没有帮助
        for i, p in enumerate(parts):
            m = re.search(r"\d+", p)
            if m:
                uid, at = m.group(), (i, m.start(), m.end())
                break
    if uid is None:
        bad = next((p for p in parts if any(c.isalnum() for c in p)), "")
        if bad:
            return None, None, "", ("「%s」我看不懂。格式是："
                                    "查询 [游戏] <UID> [角色名]，空格可有可无"
                                    % bad)
        return None, None, "", "没看到 UID（要纯数字）。"

    # ---- 2) 游戏名和角色名 ---------------------------------------------
    game: str | None = None
    names: list[str] = []

    def take(part: str) -> None:
        """收下一个角色名片段，杂词（UID 这种）直接丢。"""
        if part and not _is_noise(part):
            names.append(part)

    for i, p in enumerate(parts):
        if at and i == at[0]:
            head, tail = p[:at[1]], p[at[2]:]
            if head:
                g, rest = _match_game(head)
                if g and game is None:
                    game = g
                    take(rest)
                else:
                    take(head)
            take(tail)
            continue
        g = _alias_of(p)
        if g:
            # **游戏名永远不当角色名** —— 即使游戏已经定下来了。
            # 否则「崩坏： 星穹铁道 108931973」里第二个词会变成焦点。
            if game is None:
                game = g
            continue
        take(p)

    if not _UID_LEN[0] <= len(uid) <= _UID_LEN[1]:
        # Enka 对格式不对的 UID 直接 400；本地先拦一道，省一次请求
        return None, None, "", "UID 一般是 9 位数字，你给的 %d 位，检查一下？" % len(uid)
    return game, uid, " ".join(names).strip(), ""


# ------------------------------------------------------------------ 缓存与限流
class Cache:
    """ttl 缓存 + 调用配额。存在 data/enka_cache.json。

    为什么配额也要落盘：机器人重启后如果计数清零，反复重启就能绕开上限，
    那这个限制就没意义了。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.entries: dict[str, dict] = {}
        self.recent: list[float] = []
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries = raw.get("entries") or {}
            self.recent = [float(x) for x in (raw.get("recent") or [])]
        except (OSError, ValueError, TypeError):
            self.entries, self.recent = {}, []

    def save(self) -> None:
        # 先写临时文件再替换：写一半被打断也不会留下坏 JSON
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"entries": self.entries, "recent": self.recent[-200:]},
                ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            logging.warning("写 enka 缓存失败（不影响这次查询）：%s", exc)

    def get_payload(self, key: str) -> dict | None:
        """没过期的缓存（**解析后的精简结构**，不是渲染好的文本）。"""
        e = self.entries.get(key)
        if not e:
            return None
        if float(e.get("expire", 0)) < time.time():
            return None
        p = e.get("payload")
        return p if isinstance(p, dict) else None

    def put_payload(self, key: str, payload: dict, ttl: float) -> None:
        # ttl 是「下次真的去问 MHY」的秒数；最小留 60 秒，
        # 免得接口返回 0 导致我们每次都真的去打
        self.entries[key] = {"expire": time.time() + max(60.0, ttl),
                            "payload": payload}
        if len(self.entries) > 100:
            # 只留最新的一批。存的是精简结构（一个 UID 几 KB），
            # 100 条大概几百 KB —— 再多这个文件就不适合每次读写了。
            keep = sorted(self.entries.items(),
                          key=lambda kv: kv[1].get("expire", 0))[-60:]
            self.entries = dict(keep)
        self.save()        # 顺手落盘：调用方忘了 save 就会静默丢缓存

    def allow(self, min_interval: float, per_hour: int) -> str:
        """能不能再发一次请求。返回空字符串 = 可以，否则是不能的原因。

        时间基准取自**落盘的 recent**，不是进程内变量 —— 重启后间隔和
        每小时上限都还在，不然反复重启就能绕开限制。
        """
        now = time.time()
        self.recent = [t for t in self.recent if now - t < 3600]
        if per_hour > 0 and len(self.recent) >= per_hour:
            wait = int(3600 - (now - self.recent[0])) or 1
            return "这个小时查得太多了（上限 %d 次），%d 分钟后再来。" % (
                per_hour, max(1, wait // 60))
        if min_interval > 0 and self.recent:
            gap = now - self.recent[-1]
            if gap < min_interval:
                return "别点这么快，%.0f 秒后再试。" % (min_interval - gap)
        return ""

    def note_call(self) -> None:
        self.recent.append(time.time())
        self.save()        # 配额也要落盘，不然重启就能绕过上限


# ------------------------------------------------------------------ HTTP
def fetch(game: str, uid: str, timeout: float = 20.0) -> dict:
    """真的去打一次接口。失败抛 EnkaError，message 是给人看的中文。"""
    url = ENDPOINT[game].format(uid=uid)
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        # 文档里写的响应码，逐个给一句人话
        detail = ""
        try:
            body = json.loads(e.read().decode("utf-8", "replace"))
            detail = str(body.get("message") or "")
        except Exception:  # noqa: BLE001
            pass
        if e.code == 400:
            raise EnkaError("UID 格式不对，Enka 不认。") from e
        if e.code == 404:
            raise EnkaError(
                "查不到这个玩家。可能是：UID 写错了 / 服务器不对 / 没开过角色展柜。"
                + (("（Enka 说：%s）" % detail) if detail else "")) from e
        if e.code == 424:
            raise EnkaError("游戏在维护，或者刚更新完、Enka 的数据还没跟上。") from e
        if e.code == 429:
            raise EnkaError("被限流了（查太频）。等一会儿再试。") from e
        if e.code in (500, 503):
            raise EnkaError("Enka 服务端出问题了（HTTP %d），不怪你。" % e.code) from e
        raise EnkaError("Enka 返回了 HTTP %d。" % e.code) from e
    except urllib.error.URLError as e:
        raise EnkaError("连不上 enka.network（%s）。" % (e.reason,)) from e
    except (TimeoutError, ValueError) as e:
        raise EnkaError("Enka 的响应读不出来（%s）。" % e) from e


# ------------------------------------------------------------------ 解析
def _gi_avatars(data: dict) -> tuple[list[dict], bool]:
    """原神：优先用 avatarInfoList（有命座 + 装备），没有就退回展柜概要。

    返回 (角色列表, 是不是只有概要)。

    **实测这两种情况都会遇到**：某个 UID 的 `avatarInfoList` 是空的，
    而 `showAvatarInfoList` 有 12 个角色 —— 展柜开着、但详细数据拿不到
    （玩家没开放角色详细数据）。这时候只能列出「名字 + 等级」，
    没有命座也没有圣遗物，得跟用户说清楚为什么。
    """
    out: list[dict] = []
    for a in data.get("avatarInfoList") or []:
        lvl = ((a.get("propMap") or {}).get("4001") or {}).get("ival")
        cons = len(a.get("talentIdList") or [])
        out.append({"name": avatar_name("gi", a.get("avatarId")),
                    "level": str(lvl or "?"),
                    "cons": ("C%d" % cons) if cons else "",
                    "build": _parse_build(a)})
    if out:
        return out, False
    for a in ((data.get("playerInfo") or {}).get("showAvatarInfoList") or []):
        out.append({"name": avatar_name("gi", a.get("avatarId")),
                    "level": str(a.get("level") or "?"), "cons": "",
                    "build": (None, [])})
    return out, bool(out)


def _zzz_avatars(data: dict) -> tuple[list[dict], bool]:
    """绝区零：字段是 PascalCase，和原神完全两套（文档里写的）。

    装备（音擎/驱动盘）还没做 —— 没有真实样本，不猜字段名。
    """
    pi = data.get("PlayerInfo") or {}
    detail = (pi.get("ShowcaseDetail") or {}).get("AvatarList") or []
    out = []
    for a in detail:
        mind = a.get("TalentLevel")
        out.append({"name": avatar_name("zzz", a.get("Id")),
                    "level": str(a.get("Level") or "?"),
                    "cons": ("命%d" % mind) if mind else "",
                    "build": (None, [])})
    return out, False


def _hsr_avatars(data: dict) -> tuple[list[dict], bool]:
    """星穹铁道：Enka 没给公开文档，按原神那套防御性地取。

    取不到就算了 —— 宁可少显示一行，也不要因为字段名猜错而整条报错。
    遗器/光锥还没做（同上）。
    """
    out: list[dict] = []
    for a in data.get("avatarInfoList") or []:
        lvl = ((a.get("propMap") or {}).get("4001") or {}).get("ival") or a.get("level")
        cons = len(a.get("talentIdList") or a.get("rankList") or [])
        out.append({"name": avatar_name("hsr", a.get("avatarId") or a.get("avatarID")),
                    "level": str(lvl or "?"),
                    "cons": ("E%d" % cons) if cons else "",
                    "build": (None, [])})
    if out:
        return out, False
    for a in ((data.get("playerInfo") or {}).get("showAvatarInfoList") or []):
        out.append({"name": avatar_name("hsr", a.get("avatarId")),
                    "level": str(a.get("level") or "?"), "cons": "",
                    "build": (None, [])})
    return out, bool(out)


#: 区服代码 -> 人话。接口给的是 cn_gf01 / os_usa 这种
_REGION = {"cn": "国服", "os_usa": "美服", "os_euro": "欧服",
           "os_asia": "亚服", "os_cht": "台港澳服",
           "cn_gf01": "官服", "cn_qd01": "B服",
           "na": "美服", "eu": "欧服", "asia": "亚服", "tw": "台港澳服"}


def _norm_value(prop: str, value):
    """把接口的词条值换成**算分用的单位**。

    **这一步不做，分数会大 100 倍。** 接口对百分比词条给的是百分数
    （充能效率 `24` 表示 24%），而莫娜那张 EFF 表用的是小数（`0.065` = 6.5%）。
    实测踩过：没对齐单位时一件圣遗物能算出一千多分。
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return value
    if prop in _PERCENT_PROPS:
        return num / 100.0
    return num


def _art_score(art: dict, weights: dict | None) -> float | None:
    """一件圣遗物的「有效词条」分（莫娜占卜铺那套）。

    只在**原神**上算 —— 崩铁的遗器和绝区零的驱动盘是另一套词条体系，
    莫娜本身也只做原神。
    """
    raw = art.get("subs_raw")
    if not raw or weights is None:
        return None
    return mona.score(raw, art.get("rank") or 5, weights)


def _detail_reply(game: str, label: str, nick: str, uid: str, who: dict,
                  weights: dict | None = None) -> str:
    """单个角色的完整面板：武器/光锥/音擎 + 五~六件装备（主词条 + 副词条）。"""
    term = _TERM.get(game, _TERM["gi"])
    lines = ["%s · %s（UID %s）" % (label, nick, uid),
             "%s Lv.%s%s" % (who["name"], who["level"],
                             ("  " + who["cons"]) if who["cons"] else "")]
    weapon, arts = who.get("build") or (None, [])
    if weapon:
        w = "%s：%s" % (term["weapon"], weapon["name"])
        if weapon.get("level"):
            w += " Lv.%s" % weapon["level"]
        if weapon.get("rank"):
            w += " %d★" % weapon["rank"]
        if weapon.get("refine"):
            w += " 精%d" % weapon["refine"]
        lines.append(w)
    if not arts:
        lines.append("%s：没看到（展柜里没放，或者详细数据没开放）" % term["equip"])
        return "\n".join(lines)
    lines.append("%s：%s" % (term["equip"], _set_summary(arts)))
    scored: list[float] = []
    unscored = 0
    for a in arts:
        subs = "  ".join("%s%s" % (n, v) for n, v in a["subs"])
        sc = _art_score(a, weights)
        tail = ""
        if sc is not None:
            # 没满级的标 * —— 莫娜那边给的是「强化满级的期望分」，
            # 这里给的是当前分，不标出来会被当成同一个数字
            star = "" if (a.get("level") or 0) >= 21 else "*"
            tail = "  → %.1f%s" % (sc, star)
            scored.append(sc)
            if star:
                unscored += 1
        lines.append("  %s %s %s · %s%s%s%s"
                     % (a["slot"], a.get("lv") or "", a["set"],
                        a["main"], a["main_val"],
                        ("  |  " + subs) if subs else "", tail))
    if scored:
        lines.append("有效词条合计：%.1f（莫娜占卜铺那套：%s，各自权重 1）"
                     % (sum(scored), "、".join(_weight_names(weights))))
        if unscored:
            lines.append("带 * 的没强化满 —— 这里是**当前分**，"
                         "莫娜那边显示的是强化满级的**期望分**")
    return "\n".join(lines)


def _weight_names(weights: dict | None) -> list[str]:
    return list((weights or {}).keys())


def _gi_payload(data: dict) -> dict:
    """原神。"""
    pi = data.get("playerInfo") or {}
    line2 = []
    if pi.get("level"):
        line2.append("等级 %s" % pi["level"])
    if pi.get("worldLevel"):
        line2.append("世界等级 %s" % pi["worldLevel"])
    if pi.get("finishAchievementNum"):
        line2.append("成就 %s" % pi["finishAchievementNum"])
    f, c = pi.get("towerFloorIndex"), pi.get("towerLevelIndex")
    if f and c:
        line2.append("深境 %s-%s" % (f, c))
    avatars, summary_only = _gi_avatars(data)
    return {"nick": pi.get("nickname") or "?", "line2": line2,
            "region": _region(data), "avatars": avatars,
            "summary_only": summary_only}


def _hsr_payload(data: dict) -> dict:
    """崩铁。

    **注意：崩铁的响应结构和原神完全不一样。** 它没有 `playerInfo`，
    角色在 `detailInfo.avatarDetailList` 里，昵称/等级也在 `detailInfo`
    下面 —— 用原神那套去取会一个字段都拿不到。实测踩过：第一版就是这样，
    查崩铁只会回一句「没看到角色展柜」。
    """
    di = data.get("detailInfo") or {}
    line2 = []
    if di.get("level"):
        line2.append("等级 %s" % di["level"])
    if di.get("worldLevel"):
        line2.append("世界等级 %s" % di["worldLevel"])
    rec = di.get("recordInfo") or {}
    if rec.get("achievementCount"):
        line2.append("成就 %s" % rec["achievementCount"])
    if rec.get("avatarCount"):
        line2.append("角色 %s 个" % rec["avatarCount"])

    out: list[dict] = []
    for a in di.get("avatarDetailList") or []:
        # 光锥。`_flat.name` 是个 hash 字符串，要去 hsr 的文本表查
        eq = a.get("equipment") or {}
        weapon = None
        if eq:
            weapon = {"name": hsr_text((eq.get("_flat") or {}).get("name")) or "光锥",
                      "level": eq.get("level"), "refine": eq.get("rank"),
                      "rank": None}
        # 遗器。`_flat.props` 是算好的值，**第一条是主词条** ——
        # 这一点验过，不是猜的：1 号位首项全是 HPDelta、2 号位全是
        # AttackDelta（部位决定主词条），3~6 号位首项也都落在各自
        # 合法的主词条集合里；props 条数正好 = 1 主 + 副词条数。
        arts = []
        for r in a.get("relicList") or []:
            rf = r.get("_flat") or {}
            props = rf.get("props") or []
            main = props[0] if props else {}
            arts.append({
                "slot": _HSR_SLOTS.get(r.get("type"), "%s号位" % r.get("type")),
                "set": hsr_text(rf.get("setName")) or "遗器%s" % rf.get("setID"),
                "level": r.get("level"), "rank": None,
                "lv": _equip_level("hsr", r.get("level")),
                "main": hsr_text(main.get("type")) or str(main.get("type") or "?"),
                "main_val": _hsr_stat_value(main.get("type"), main.get("value")),
                "subs": [(hsr_text(s.get("type")) or str(s.get("type")),
                          _hsr_stat_value(s.get("type"), s.get("value")))
                         for s in props[1:]],
            })
        order = {v: i for i, v in enumerate(_HSR_SLOTS.values())}
        arts.sort(key=lambda x: order.get(x["slot"], 99))
        rl = a.get("rankList") or []
        out.append({"name": avatar_name("hsr", a.get("avatarId")),
                    "level": str(a.get("level") or "?"),
                    "cons": ("星魂%d" % len(rl)) if rl else "",
                    "build": (weapon, arts)})

    return {"nick": di.get("nickname") or "?", "line2": line2,
            "region": _region(data), "avatars": out, "summary_only": False}


def _zzz_payload(data: dict) -> dict:
    """绝区零。字段是 PascalCase，装备在 `EquippedList` / `Weapon` 里。"""
    pi = data.get("PlayerInfo") or {}
    pd = (pi.get("SocialDetail") or {}).get("ProfileDetail") or {}
    line2 = []
    if pd.get("Level"):
        line2.append("绳网等级 %s" % pd["Level"])

    out: list[dict] = []
    for a in (pi.get("ShowcaseDetail") or {}).get("AvatarList") or []:
        w = a.get("Weapon") or {}
        weapon = None
        if w:
            wi = _zzz_table("weapon").get(str(w.get("Id"))) or {}
            weapon = {"name": wi.get("n") or "音擎", "level": w.get("Level"),
                      "rank": wi.get("r"), "refine": None}
        arts = []
        for item in a.get("EquippedList") or []:
            e = item.get("Equipment") or {}
            # 驱动盘主词条要乘等级/稀有度系数，所以得知道这个盘的稀有度
            info = _zzz_table("disc").get(str(e.get("Id"))) or {}
            mains = e.get("MainPropertyList") or []
            main = mains[0] if mains else {}
            arts.append({
                "slot": _ZZZ_SLOTS.get(item.get("Slot"),
                                       "%s号位" % item.get("Slot")),
                "set": info.get("s") or "驱动盘%s" % e.get("Id"),
                "level": e.get("Level"), "rank": info.get("r"),
                "lv": _equip_level("zzz", e.get("Level")),
                "main": _zzz_stat(main.get("PropertyId"))[0],
                "main_val": _zzz_main_value(main.get("PropertyId"),
                                            main.get("PropertyValue"),
                                            e.get("Level"), info.get("r")),
                # 副词条：PropertyValue 是**每次**的量，总量 = 值 * 次数
                "subs": [(_zzz_stat(x.get("PropertyId"))[0],
                          _zzz_sub_value(x.get("PropertyId"),
                                         x.get("PropertyValue"),
                                         x.get("PropertyLevel")))
                         for x in (e.get("RandomPropertyList") or [])],
            })
        order = {v: i for i, v in enumerate(_ZZZ_SLOTS.values())}
        arts.sort(key=lambda x: order.get(x["slot"], 99))
        mind = a.get("TalentLevel") or 0
        out.append({"name": avatar_name("zzz", a.get("Id")),
                    "level": str(a.get("Level") or "?"),
                    "cons": ("命%d" % mind) if mind else "",
                    "build": (weapon, arts)})

    return {"nick": pd.get("Nickname") or "?", "line2": line2,
            "region": _region(data), "avatars": out, "summary_only": False}


def parse_payload(game: str, data: dict) -> dict:
    """接口响应 -> 精简结构。

    **缓存的就是它**，不是渲染好的文本 —— 这样「列表」和「点名看某个角色」
    两种渲染可以共用同一份缓存，点名时不会再多打一次接口
    （Enka 的作者明确要求别反复打同一个 UID）。
    """
    if game == "zzz":
        return _zzz_payload(data)
    if game == "hsr":
        return _hsr_payload(data)
    return _gi_payload(data)


def render(game: str, uid: str, parsed: dict, focus: str | None = None,
           weights: dict | None = None) -> str:
    """精简结构 -> 要发出去的文字。可以反复调（不改 parsed）。"""
    label = GAME_LABEL.get(game, game)
    nick = parsed.get("nick") or "?"
    avatars = parsed.get("avatars") or []
    summary_only = bool(parsed.get("summary_only"))
    region = parsed.get("region") or ""

    if focus:
        key = focus.strip().lower()
        hit = next((a for a in avatars if key and key in a["name"].lower()), None)
        if hit:
            return _detail_reply(game, label, nick, uid, hit, weights)
        have = "、".join(a["name"] for a in avatars[:12])
        cause = ("展柜只有概要，看不到角色详情" if summary_only
                 else "展柜里没有这个角色")
        return ("%s · %s（UID %s）里没找到「%s」—— %s。\n展柜里有：%s"
                % (label, nick, uid, focus, cause, have))

    head = "%s · %s（UID %s%s）" % (
        label, nick, uid, (" · %s" % region) if region else "")
    lines = [head]
    if parsed.get("line2"):
        lines.append(" · ".join(parsed["line2"]))

    if not avatars:
        lines.append("没看到角色展柜 —— 游戏里「展柜」关了或者没放角色。")
        return "\n".join(lines)

    shown = avatars[:12]
    lines.append("展柜 %d 个：" % len(avatars))
    for a in shown:
        lines.append("  " + _build_line(a["name"], a["level"], a["cons"],
                                        a.get("build") or (None, []),
                                        weights))
    if len(avatars) > len(shown):
        lines.append("  ……还有 %d 个" % (len(avatars) - len(shown)))
    if summary_only:
        # 只说明缺了什么 + 怎么才能有。不猜原因（从响应里看不出是
        # 「展柜详情没开」还是「这次没抓到」），但把出路写清楚。
        lines.append("（只有概要：拿不到命座、武器、圣遗物。"
                     "游戏里把角色展柜的详细数据打开就有了）")
    else:
        term = _TERM.get(game, _TERM["gi"])["equip"]
        lines.append("想看某个角色的完整面板（%s词条）：在指令后面加名字，"
                     "例如「查询 %s %s %s」"
                     % (term, label, uid,
                        avatars[0]["name"] if avatars else "可莉"))
    return "\n".join(lines)


def format_reply(game: str, uid: str, data: dict,
                 focus: str | None = None,
                 weights: dict | None = None) -> str:
    """解析 + 渲染。给测试和一次性调用用；query() 走缓存那条路。"""
    return render(game, uid, parse_payload(game, data), focus=focus,
                  weights=weights)


# ------------------------------------------------------------------ 对外入口
def query(game: str | None, uid: str, cfg, cache: Cache,
          focus: str | None = None,
          weights: dict | None = None) -> tuple[bool, str]:
    """查一个 UID。返回 (成功?, 要发出去的文字)。

    game 为 None 时按 gi -> hsr -> zzz 依次试。

    **间隔限制按「一次用户查询」算，不是按内部尝试算。** 一开始写在循环里，
    结果自动识别永远走不通：第一个游戏 404 之后，第二个会被自己的
    「别点这么快」挡住。每小时上限仍然按真实请求数算（note_call 每次请求都记）。

    `focus`（角色名）**不进缓存** —— 缓存的是「这个 UID 的列表回复」，
    点名看某个角色的面板时每次都重新格式化（数据在缓存里，不会多打接口）。
    """
    order = [game] if game else ["gi", "hsr", "zzz"]

    # 先把三个游戏的缓存都看一遍 —— 命中就不该消耗配额。
    # **点名看某个角色也走这里**：缓存的是解析后的结构，能直接重渲染，
    # 所以「先看列表、再点一个人」不会多打一次接口。
    for g in order:
        hit = cache.get_payload("%s:%s" % (g, uid))
        if hit:
            return True, render(g, uid, hit, focus=focus, weights=weights)

    why = cache.allow(getattr(cfg, "min_interval_seconds", 3.0),
                      getattr(cfg, "max_per_hour", 60))
    if why:
        return False, why

    last_err = ""
    for g in order:
        cache.note_call()          # 每次真实请求都记账（算每小时上限）
        try:
            data = fetch(g, uid, timeout=getattr(cfg, "timeout_seconds", 20))
        except EnkaError as exc:
            last_err = str(exc)
            # 只有「查不到」才值得换下一个游戏试；
            # 限流 / 维护 / 网络问题换游戏也一样，直接回报
            if "查不到这个玩家" not in last_err:
                return False, last_err
            continue
        parsed = parse_payload(g, data)
        cache.put_payload("%s:%s" % (g, uid), parsed, float(data.get("ttl") or 0))
        if game is None:
            logging.info("enka: UID %s 命中 %s", uid, GAME_LABEL.get(g, g))
        return True, render(g, uid, parsed, focus=focus, weights=weights)

    return False, last_err or "三个游戏都查不到这个 UID。"


def _zzz_stat_value(prop_id, value) -> str:
    """旧名字，等于「按总量格式化」。保留给测试和一次性调用。"""
    return _zzz_stat_name_value(prop_id, value)
