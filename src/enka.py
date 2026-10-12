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

_NAMES: dict[str, dict[str, str]] | None = None


class EnkaError(RuntimeError):
    """查不到 / 被限流 / 服务端出错 —— message 是可以直接发到群里的中文。"""


# ------------------------------------------------------------------ 名字表
def names(game: str) -> dict[str, str]:
    """角色 ID -> 中文名。文件缺失时返回空表（照常显示 ID，不崩）。"""
    global _NAMES
    if _NAMES is None:
        try:
            _NAMES = json.loads(NAMES_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logging.warning("读不到 %s（%s），角色名会显示成 ID", NAMES_PATH, exc)
            _NAMES = {}
    return _NAMES.get(game) or {}


def avatar_name(game: str, avatar_id) -> str:
    return names(game).get(str(avatar_id)) or "ID:%s" % avatar_id


# ------------------------------------------------------------------ 参数解析
def parse_args(text: str) -> tuple[str | None, str | None, str]:
    """解析 `[游戏] <uid>`。返回 (游戏, uid, 错误说明)。

    游戏可以省略（省略时自动试三个）。uid 必须是纯数字。
    """
    parts = [p for p in (text or "").replace("，", " ").split() if p]
    if not parts:
        return None, None, "要查谁的？给我一个 UID，例如：uid 618285856"
    game = None
    uid = None
    for p in parts:
        low = p.lower()
        if low in ALIASES:
            game = ALIASES[low]
            continue
        if p.isdigit():
            uid = p
            continue
        # 既不是游戏名也不是纯数字
        return None, None, ("「%s」我看不懂。格式是：uid [游戏] <UID>，"
                            "游戏可选（原神/星铁/绝区零）" % p)
    if uid is None:
        return None, None, "没看到 UID（要纯数字）。"
    if not 6 <= len(uid) <= 10:
        # Enka 对格式不对的 UID 直接 400；本地先拦一道，省一次请求
        return None, None, "UID 一般是 9 位数字，你给的 %d 位，检查一下？" % len(uid)
    return game, uid, ""


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

    def get(self, key: str) -> str | None:
        """没过期的缓存回复。"""
        e = self.entries.get(key)
        if not e:
            return None
        if float(e.get("expire", 0)) < time.time():
            return None
        return str(e.get("reply") or "") or None

    def put(self, key: str, reply: str, ttl: float) -> None:
        # ttl 是「下次真的去问 MHY」的秒数；最小留 60 秒，
        # 免得接口返回 0 导致我们每次都真的去打
        self.entries[key] = {"expire": time.time() + max(60.0, ttl),
                            "reply": reply}
        if len(self.entries) > 300:
            # 只留最新的一批，别让文件无限长
            keep = sorted(self.entries.items(),
                          key=lambda kv: kv[1].get("expire", 0))[-200:]
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
def _gi_avatars(data: dict) -> tuple[list[tuple[str, str, str]], bool]:
    """原神：优先用 avatarInfoList（有命座），没有就退回展柜概要。

    返回 (角色列表, 是不是只有概要)。

    **实测这两种情况都会遇到**：某个 UID 的 `avatarInfoList` 是空的，
    而 `showAvatarInfoList` 有 12 个角色 —— 展柜开着、但详细数据拿不到
    （游戏里没开「展柜详情」之类）。这时候只能列出「名字 + 等级」，
    没有命座，得跟用户说清楚为什么，否则会以为是程序坏了。
    """
    out: list[tuple[str, str, str]] = []
    for a in data.get("avatarInfoList") or []:
        lvl = ((a.get("propMap") or {}).get("4001") or {}).get("ival")
        cons = len(a.get("talentIdList") or [])
        out.append((avatar_name("gi", a.get("avatarId")),
                    str(lvl or "?"), ("C%d" % cons) if cons else ""))
    if out:
        return out, False
    for a in ((data.get("playerInfo") or {}).get("showAvatarInfoList") or []):
        out.append((avatar_name("gi", a.get("avatarId")),
                    str(a.get("level") or "?"), ""))
    return out, bool(out)


def _zzz_avatars(data: dict) -> tuple[list[tuple[str, str, str]], bool]:
    """绝区零：字段是 PascalCase，和原神完全两套（文档里写的）。"""
    pi = data.get("PlayerInfo") or {}
    detail = (pi.get("ShowcaseDetail") or {}).get("AvatarList") or []
    out = []
    for a in detail:
        mind = a.get("TalentLevel")
        out.append((avatar_name("zzz", a.get("Id")),
                    str(a.get("Level") or "?"),
                    ("命%d" % mind) if mind else ""))
    return out, False


def _hsr_avatars(data: dict) -> tuple[list[tuple[str, str, str]], bool]:
    """星穹铁道：Enka 没给公开文档，按原神那套防御性地取。

    取不到就算了 —— 宁可少显示一行，也不要因为字段名猜错而整条报错。
    """
    out: list[tuple[str, str, str]] = []
    for a in data.get("avatarInfoList") or []:
        lvl = ((a.get("propMap") or {}).get("4001") or {}).get("ival") or a.get("level")
        cons = len(a.get("talentIdList") or a.get("rankList") or [])
        out.append((avatar_name("hsr", a.get("avatarId") or a.get("avatarID")),
                    str(lvl or "?"), ("E%d" % cons) if cons else ""))
    if out:
        return out, False
    for a in ((data.get("playerInfo") or {}).get("showAvatarInfoList") or []):
        out.append((avatar_name("hsr", a.get("avatarId")),
                    str(a.get("level") or "?"), ""))
    return out, bool(out)


_REGION = {"cn": "国服", "os_usa": "美服", "os_euro": "欧服",
           "os_asia": "亚服", "os_cht": "台港澳服",
           "cn_gf01": "官服", "cn_qd01": "B服",
           "na": "美服", "eu": "欧服", "asia": "亚服", "tw": "台港澳服"}


def format_reply(game: str, uid: str, data: dict) -> str:
    """把响应变成一段能直接发群里的中文。"""
    label = GAME_LABEL.get(game, game)

    if game == "zzz":
        pi = data.get("PlayerInfo") or {}
        pd = (pi.get("SocialDetail") or {}).get("ProfileDetail") or {}
        nick = pd.get("Nickname") or "?"
        lvl = pd.get("Level")
        line2 = []
        if lvl:
            line2.append("绳网等级 %s" % lvl)
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
        if game == "gi":
            avatars, summary_only = _gi_avatars(data)
        else:
            avatars, summary_only = _hsr_avatars(data)

    region = _REGION.get(str(data.get("region") or "").lower(), data.get("region"))
    head = "%s · %s（UID %s%s）" % (
        label, nick, uid, (" · %s" % region) if region else "")

    lines = [head]
    if line2:
        lines.append(" · ".join(line2))
    if avatars:
        # 原神展柜最多 12 个，就别只列 8 个了 —— 少列的那几个正是用户想看的
        shown = avatars[:12]
        body = "、".join(
            "%s %s%s" % (n, lv, (" " + c) if c else "") for n, lv, c in shown)
        if len(avatars) > len(shown):
            body += " 等 %d 个" % len(avatars)
        lines.append("展柜 %d 个：%s" % (len(avatars), body))
        if summary_only:
            # 只说明缺了什么 + 怎么才能有。不猜原因（从响应里看不出是
            # 「展柜详情没开」还是「这次没抓到」），但把出路写清楚。
            lines.append("（只有概要：拿不到命座和武器。"
                         "游戏里把角色展柜的详细数据打开就有了）")
    else:
        lines.append("没看到角色展柜 —— 游戏里「展柜」关了或者没放角色。")
    return "\n".join(lines)


# ------------------------------------------------------------------ 对外入口
def query(game: str | None, uid: str, cfg, cache: Cache) -> tuple[bool, str]:
    """查一个 UID。返回 (成功?, 要发出去的文字)。

    game 为 None 时按 gi -> hsr -> zzz 依次试。

    **间隔限制按「一次用户查询」算，不是按内部尝试算。** 一开始写在循环里，
    结果自动识别永远走不通：第一个游戏 404 之后，第二个会被自己的
    「别点这么快」挡住。每小时上限仍然按真实请求数算（note_call 每次请求都记）。
    """
    order = [game] if game else ["gi", "hsr", "zzz"]

    # 先把三个游戏的缓存都看一遍 —— 命中就不该消耗配额
    for g in order:
        hit = cache.get("%s:%s" % (g, uid))
        if hit:
            return True, hit

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
        reply = format_reply(g, uid, data)
        cache.put("%s:%s" % (g, uid), reply, float(data.get("ttl") or 0))
        if game is None:
            logging.info("enka: UID %s 命中 %s", uid, GAME_LABEL.get(g, g))
        return True, reply

    return False, last_err or "三个游戏都查不到这个 UID。"
