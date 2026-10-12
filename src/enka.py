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
import time
import urllib.error
import urllib.request
from pathlib import Path

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
#: 用户可能怎么叫这几个游戏
ALIASES = {
    "gi": "gi", "genshin": "gi", "原神": "gi", "原": "gi",
    "hsr": "hsr", "starrail": "hsr", "星铁": "hsr", "崩铁": "hsr",
    "星穹铁道": "hsr", "崩坏星穹铁道": "hsr",
    "zzz": "zzz", "zenless": "zzz", "绝区零": "zzz", "绝": "zzz",
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
                "rank": flat.get("rankLevel"),
                "main": _short(main.get("mainPropId")),
                "main_val": _stat_value(main.get("mainPropId"),
                                        main.get("statValue")),
                "subs": [(_short(s.get("appendPropId")),
                          _stat_value(s.get("appendPropId"), s.get("statValue")))
                         for s in (flat.get("reliquarySubstats") or [])],
            })
    # 按部位固定顺序排（花/羽/沙/杯/冠），接口给的顺序不保证
    order = {v: i for i, v in enumerate(_SLOTS.values())}
    arts.sort(key=lambda a: order.get(a["slot"], 99))
    return weapon, arts


def _build_line(name: str, level: str, cons: str, build) -> str:
    """展柜列表里的一行：名字 等级 命座 · 武器 · 圣遗物套装。"""
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
    return " · ".join(bits)


# ------------------------------------------------------------------ 参数解析
def parse_args(text: str) -> tuple[str | None, str | None, str, str]:
    """解析 `[游戏] <uid> [角色名]`。返回 (游戏, uid, 角色名, 错误说明)。

    - 游戏可以省略（省略时自动试三个）
    - uid 必须是纯数字
    - 角色名可以省略；给了就只输出那个角色的完整面板。
      它允许**不完整**（子串匹配），所以「可莉」「莉」都行 —— 但为了不
      误吃参数，只在前面两项都认出来之后才把它当角色名。
    """
    parts = [p for p in (text or "").replace("，", " ").split() if p]
    if not parts:
        return None, None, "", "要查谁的？给我一个 UID，例如：uid 618285856"
    game = None
    uid = None
    rest: list[str] = []
    for p in parts:
        low = p.lower()
        if uid is None and low in ALIASES:
            game = ALIASES[low]
            continue
        if uid is None and p.isdigit():
            uid = p
            continue
        rest.append(p)
    if uid is None:
        bad = rest[0] if rest else ""
        if bad:
            return None, None, "", ("「%s」我看不懂。格式是："
                                    "查询 [游戏] <UID> [角色名]" % bad)
        return None, None, "", "没看到 UID（要纯数字）。"
    if not 6 <= len(uid) <= 10:
        # Enka 对格式不对的 UID 直接 400；本地先拦一道，省一次请求
        return None, None, "", "UID 一般是 9 位数字，你给的 %d 位，检查一下？" % len(uid)
    return game, uid, " ".join(rest).strip(), ""


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


def _detail_reply(label: str, nick: str, uid: str, who: dict) -> str:
    """单个角色的完整面板：武器 + 五件圣遗物（主词条 + 四个副词条）。"""
    lines = ["%s · %s（UID %s）" % (label, nick, uid),
             "%s Lv.%s%s" % (who["name"], who["level"],
                             ("  " + who["cons"]) if who["cons"] else "")]
    weapon, arts = who.get("build") or (None, [])
    if weapon:
        w = "武器：%s" % weapon["name"]
        if weapon.get("level"):
            w += " Lv.%s" % weapon["level"]
        if weapon.get("rank"):
            w += " %d★" % weapon["rank"]
        if weapon.get("refine"):
            w += " 精%d" % weapon["refine"]
        lines.append(w)
    if not arts:
        lines.append("圣遗物：没看到（展柜里没放，或者详细数据没开放）")
        return "\n".join(lines)
    lines.append("圣遗物：%s" % _set_summary(arts))
    for a in arts:
        subs = "  ".join("%s%s" % (n, v) for n, v in a["subs"])
        lines.append("  %s +%s %s · %s%s%s"
                     % (a["slot"], (a.get("level") or 1) - 1, a["set"],
                        a["main"], a["main_val"],
                        ("  |  " + subs) if subs else ""))
    return "\n".join(lines)


def parse_payload(game: str, data: dict) -> dict:
    """接口响应 -> 精简结构。

    **缓存的就是它**，不是渲染好的文本 —— 这样「列表」和「点名看某个角色」
    两种渲染可以共用同一份缓存，点名时不会再多打一次接口
    （Enka 的作者明确要求别反复打同一个 UID）。
    """
    if game == "zzz":
        pi = data.get("PlayerInfo") or {}
        pd = (pi.get("SocialDetail") or {}).get("ProfileDetail") or {}
        nick = pd.get("Nickname") or "?"
        line2 = []
        if pd.get("Level"):
            line2.append("绳网等级 %s" % pd["Level"])
        avatars, summary_only = _zzz_avatars(data)
    else:
        pi = data.get("playerInfo") or {}
        nick = pi.get("nickname") or "?"
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
        avatars, summary_only = (_gi_avatars(data) if game == "gi"
                                 else _hsr_avatars(data))

    region = _REGION.get(str(data.get("region") or "").lower(), data.get("region"))
    return {"nick": nick, "line2": line2, "region": region or "",
            "avatars": avatars, "summary_only": summary_only}


def render(game: str, uid: str, parsed: dict, focus: str | None = None) -> str:
    """精简结构 -> 要发出去的文字。可以反复调（不改 parsed）。"""
    label = GAME_LABEL.get(game, game)
    nick = parsed.get("nick") or "?"
    avatars = parsed.get("avatars") or []
    summary_only = bool(parsed.get("summary_only"))
    region = parsed.get("region") or ""

    if focus and game != "zzz":
        key = focus.strip().lower()
        hit = next((a for a in avatars if key and key in a["name"].lower()), None)
        if hit:
            return _detail_reply(label, nick, uid, hit)
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
                                        a.get("build") or (None, [])))
    if len(avatars) > len(shown):
        lines.append("  ……还有 %d 个" % (len(avatars) - len(shown)))
    if summary_only:
        # 只说明缺了什么 + 怎么才能有。不猜原因（从响应里看不出是
        # 「展柜详情没开」还是「这次没抓到」），但把出路写清楚。
        lines.append("（只有概要：拿不到命座、武器、圣遗物。"
                     "游戏里把角色展柜的详细数据打开就有了）")
    else:
        lines.append("想看某个角色的完整面板（圣遗物词条）："
                     "在指令后面加名字，例如「查询 原神 %s 可莉」" % uid)
    return "\n".join(lines)


def format_reply(game: str, uid: str, data: dict,
                 focus: str | None = None) -> str:
    """解析 + 渲染。给测试和一次性调用用；query() 走缓存那条路。"""
    return render(game, uid, parse_payload(game, data), focus=focus)


# ------------------------------------------------------------------ 对外入口
def query(game: str | None, uid: str, cfg, cache: Cache,
          focus: str | None = None) -> tuple[bool, str]:
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
            return True, render(g, uid, hit, focus=focus)

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
        return True, render(g, uid, parsed, focus=focus)

    return False, last_err or "三个游戏都查不到这个 UID。"
