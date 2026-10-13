"""鸣潮账号查询：通过 `wuthering-waves-platform` 的 MCP 服务拿数据。

## 跟查原神/星铁/绝区零（src/enka.py）不是一回事

| | Enka | 这里 |
|---|---|---|
| 数据来源 | 公开的 UID 展柜 | **你自己的库街区账号**（手机号+短信登录） |
| 鉴权 | 不需要 | 需要本地凭据（`~/.waves-platform/credentials.json`） |
| 查谁 | 任何人 | **只能查自己** |

所以它**不是公开指令**，是一条控制指令（要授权 + 口令），跟 `\\截图` 同级。
默认只认私聊/可信会话，免得把体力、抽卡这些个人数据摆到群里。

## 为什么走 MCP 而不是 HTTP

`wuthering-waves-platform` 目前只提供 MCP stdio（README 里说 CLI/HTTP 适配器
「会复用同一套核心服务」，但还没做）。所以这边直接用 `src/mcp_stdio.py`
起它的服务进程、说 JSON-RPC —— 跟查 Enka 一样，**确定的事不过 LLM**。

## 首次调用会慢

`npx` 第一次要下包（实测 12 秒），之后就 2~3 秒。默认超时给 120 秒，
别按普通 HTTP 的 20 秒来。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .mcp_stdio import McpError, McpStdio

log = logging.getLogger("wechat-bot.wuwa")

#: 没登录时平台返回的错误码，用来给一句人能看懂的提示
CREDENTIAL_ERROR = "CREDENTIAL_ERROR"

NOT_LOGGED_IN = (
    "还没登录库街区账号。在电脑上跑一次：\n"
    "    wuthering-waves-platform login\n"
    "（按提示输手机号和短信验证码；登录凭据存在 "
    "%USERPROFILE%\\.waves-platform\\credentials.json）"
)

#: 账号列表缓存多久。切账号、加账号都很罕见，而每次查询要起一个 node 进程。
ACCOUNT_TTL = 600

#: 列账号 / 切账号 / 登录的关键词（中文、英文都认）
LIST_WORDS = ("账号", "列表", "list", "accounts")
SWITCH_WORDS = ("切换", "切到", "换", "switch", "use")
LOGIN_WORDS = ("登录", "login", "绑定")
CODE_WORDS = ("验证码", "code")
LOGOUT_WORDS = ("注销", "退出登录", "logout")

#: 登录流程多久没走完就作废（秒）。验证码本身也就几分钟有效。
LOGIN_TTL = 600


def mask_mobile(mobile: str) -> str:
    """手机号打码之后再往外发。

    群里发的消息所有成员都看得到 —— 手机号不该原样留在聊天记录里。
    **验证码一个字都不回显**（它等于一次登录机会）。
    """
    m = "".join(ch for ch in str(mobile or "") if ch.isdigit())
    if len(m) >= 7:
        return "%s****%s" % (m[:3], m[-4:])
    if len(m) > 3:
        return m[:2] + "*" * (len(m) - 3) + m[-1]
    return "*" * len(m)


def member_key(identity: dict | None, sender: str, *,
               allow_nickname: bool = False) -> tuple[str, str]:
    """把一个群成员映射成平台里的账号 ID。返回 `(key, 显示名)`。

    ## 为什么优先用微信号

    群昵称**谁都能改**。拿它当钥匙的话，A 把昵称改成 B 的，就能翻 B 的账号数据。
    微信号改不了，所以它才可信。

    微信号**哈希**之后才当 key —— 它要进平台自己的凭据文件、也会进日志，
    没必要留明文。

    `allow_nickname=True` 时才退回群昵称（默认不允许；拿不到微信号就拒绝，
    并且告诉用户为什么）。
    """
    import hashlib
    ident = identity or {}
    wid = str(ident.get("id") or "").strip()
    name = str(ident.get("nickname") or ident.get("group_nickname")
               or sender or "").strip()
    if wid:
        return "wx-" + hashlib.sha1(wid.encode("utf-8")).hexdigest()[:12], \
            name or sender
    if allow_nickname:
        return ("nick-" + hashlib.sha1(
            ("nick:" + (sender or "")).encode("utf-8")).hexdigest()[:12],
            name or sender)
    return "", name or sender


NO_IDENTITY = (
    "拿不到你的微信号，这一步做不了。\n"
    "群里昵称**谁都能改** —— 拿它当钥匙的话，别人把昵称改成你的就能翻你的号。\n"
    "（非要放宽就把 config.yaml 里 `wuwa.allow_nickname_identity` 改成 true。）"
)

LOGIN_START = (
    "手机号收到：%s\n\n"
    "接下来用**这个手机号**登录库街区 App，点「获取短信验证码」，"
    "拿到 6 位码之后发我：\n"
    "    %s验证码 <6位码>\n\n"
    "（%.0f 分钟内有效。平台的接口只负责「手机号 + 验证码」这一步，"
    "**它自己不发短信** —— 所以短信得你在 App 里点。）"
)


@dataclass
class Cache:
    """结果缓存。平台那边的数据变动很慢（体力几分钟才动），而每次查询都要
    起一个 node 进程（2~3 秒），所以短缓存很划算。"""

    path: Path
    entries: dict[str, tuple[float, str]] = field(default_factory=dict)

    def get(self, key: str, ttl: float) -> str | None:
        hit = self.entries.get(key)
        if not hit:
            return None
        ts, value = hit
        if time.time() - ts > ttl:
            return None
        return value

    def put(self, key: str, value: str) -> None:
        self.entries[key] = (time.time(), value)
        self.save()

    # -------------------------------------------------------------- 落盘
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        now = time.time()
        for k, v in (raw.get("entries") or {}).items():
            if isinstance(v, list) and len(v) == 2:
                self.entries[k] = (float(v[0]), str(v[1]))
        del now

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {"entries": {k: [t, v] for k, (t, v) in
                                list(self.entries.items())[-60:]}}
            self.path.write_text(json.dumps(data, ensure_ascii=False),
                                 encoding="utf-8")
        except OSError as exc:
            log.debug("写鸣潮缓存失败：%s", exc)


# ------------------------------------------------------------------ 调用
def call_tool(cfg, tool: str, args: dict | None = None, timeout: float = 0):
    """起一次 MCP 服务、调一个工具、关掉。返回结构化结果。

    每次都重起是有意的：MCP 服务进程长期挂着，一旦它内部状态坏了（token 过期、
    网络断了），机器人这边会一直拿到坏结果，而且没人会想到去重启它。
    重起的代价是 2~3 秒，对聊天指令可以接受。
    """
    cmd = list(getattr(cfg, "command", None) or [])
    if not cmd:
        raise McpError("没配置启动命令（config.yaml 的 wuwa.command）")
    t = float(timeout or getattr(cfg, "timeout_seconds", 120) or 120)
    with McpStdio(cmd, timeout=t) as c:
        c.initialize()
        return c.call_tool(tool, args or {})


# ------------------------------------------------------------------ 账号
def list_accounts(cfg, cache: Cache | None, *, force: bool = False) -> list[dict]:
    """拿本地已保存的账号列表（带缓存）。

    平台那边每个账号是 `{id, label, userId, serverId, roleId}`（token 被隐藏）。
    这个列表**很少变**，而每次调用要起一个 node 进程，所以默认缓存 10 分钟；
    切换账号时会强制刷新。
    """
    key = "wuwa:accounts"
    if cache is not None and not force:
        hit = cache.get(key, ACCOUNT_TTL)
        if hit:
            try:
                got = json.loads(hit)
                if isinstance(got, list):
                    return [a for a in got if isinstance(a, dict)]
            except ValueError:
                pass
    data = call_tool(cfg, "waves_account_list")
    if isinstance(data, dict):
        items = data.get("items")
        if items is None:
            # 有的实现直接返回 {accountId: …} 或者数组包在别的键里，兜一下
            items = data.get("accounts") or data.get("list") or []
    else:
        items = data
    out = [a for a in (items or []) if isinstance(a, dict)]
    if cache is not None:
        cache.put(key, json.dumps(out, ensure_ascii=False))
    return out


def _is_active(acc: dict) -> bool:
    for k in ("active", "isActive", "current", "isCurrent"):
        if acc.get(k) is True:
            return True
    return False


def format_accounts(items: list[dict], *, active_id: str = "") -> str:
    if not items:
        return NOT_LOGGED_IN
    lines = ["鸣潮 · 已保存 %d 个账号：" % len(items)]
    for i, acc in enumerate(items, 1):
        aid = str(acc.get("id") or acc.get("accountId") or "")
        name = str(acc.get("label") or acc.get("name") or aid or "未命名")
        bits = [name]
        if aid and aid != name:
            bits.append("(%s)" % aid)
        mark = ""
        if active_id and aid == active_id:
            mark = "  ← 当前"
        elif not active_id and _is_active(acc):
            mark = "  ← 当前"
        lines.append("  %d. %s%s" % (i, " ".join(bits), mark))
    lines.append("用法：「鸣潮 切换 2」把 2 号设为当前；"
                 "「鸣潮 @2 角色名」临时查 2 号（不切换）。")
    return "\n".join(lines)


def switch_account(cfg, cache: Cache | None, index: int) -> tuple[bool, str]:
    """把第 index 个账号（1 开始）设为当前账号。"""
    items = list_accounts(cfg, cache, force=True)
    if not items:
        return False, NOT_LOGGED_IN
    if not 1 <= index <= len(items):
        return False, ("没有第 %d 个账号 —— 现在有 %d 个。"
                       "发「鸣潮 账号」看列表。" % (index, len(items)))
    acc = items[index - 1]
    aid = str(acc.get("id") or acc.get("accountId") or "")
    if not aid:
        return False, "第 %d 个账号没有 ID，切不了。" % index
    call_tool(cfg, "waves_account_set_active", {"accountId": aid})
    if cache is not None:
        cache.entries.pop("wuwa:accounts", None)      # 下次重新拉
        cache.save()
    name = str(acc.get("label") or aid)
    return True, "已切到账号 %d：%s" % (index, name)


def has_account(cfg, cache: Cache | None, key: str) -> bool:
    """这个人是不是已经登录过了（他的 key 在不在本地账号库里）。"""
    if not key:
        return False
    try:
        items = list_accounts(cfg, cache)
    except McpError:
        # 拿不到列表时**别拦着** —— 让他去查，真没登录平台会报
        # CREDENTIAL_ERROR，那时给的提示更准确。
        return True
    return any(str(a.get("id") or a.get("accountId") or "") == key
               for a in items)


def start_login(cache: Cache | None, key: str, mobile: str, label: str
                ) -> tuple[bool, str]:
    """记下「某人正在登录」，等他回验证码。

    平台没有「发验证码」这个接口（`loginBySms` 要手机号和验证码一起给），
    所以这里只能先把手机号存着，等用户自己去 App 里点发送。
    """
    digits = "".join(ch for ch in str(mobile or "") if ch.isdigit())
    if not (6 <= len(digits) <= 15):
        return False, "手机号看着不对：%r。用法：「鸣潮 登录 13800138000」" % mobile
    if cache is not None:
        cache.put("login:" + key,
                  json.dumps({"mobile": digits, "label": label,
                              "at": time.time()}, ensure_ascii=False))
    return True, digits


def pending_mobile(cache: Cache | None, key: str) -> tuple[str, str]:
    """取回「正在登录」的手机号。返回 (手机号, 显示名)，没在进行中就是空。"""
    if cache is None:
        return "", ""
    raw = cache.get("login:" + key, LOGIN_TTL)
    if not raw:
        return "", ""
    try:
        data = json.loads(raw)
    except ValueError:
        return "", ""
    return str(data.get("mobile") or ""), str(data.get("label") or "")


def finish_login(cfg, cache: Cache | None, key: str, code: str
                 ) -> tuple[bool, str]:
    """拿手机号 + 验证码去登录，登录成功后账号归到 key 名下。"""
    mobile, label = pending_mobile(cache, key)
    if not mobile:
        return False, ("没有正在进行的登录。先发「鸣潮 登录 <手机号>」，"
                       "再去库街区 App 拿验证码。")
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if not (4 <= len(code) <= 8):
        return False, "验证码看着不对（应该是 6 位数字）。"
    try:
        acc = call_tool(cfg, "waves_account_login_sms", {
            "mobile": mobile, "code": code,
            "accountId": key, "label": label or key,
        })
    except McpError as exc:
        msg = str(exc)
        log.warning("鸣潮登录失败：%s", msg)
        # 验证码错 / 过期是最常见的，把它翻成人话
        if any(w in msg for w in ("验证码", "code", "失效", "过期", "错误")):
            return False, ("登录失败：验证码不对或已过期。重新发一次"
                           "「鸣潮 登录 %s」，再去 App 拿新码。"
                           % mask_mobile(mobile))
        return False, "登录失败：%s" % msg[:300]
    except Exception as exc:  # noqa: BLE001
        log.warning("鸣潮登录异常：%r", exc)
        return False, "登录出错：%s" % exc

    if cache is not None:
        cache.entries.pop("login:" + key, None)      # 用掉了
        cache.entries.pop("wuwa:accounts", None)     # 账号列表变了
        cache.save()
    name = ""
    if isinstance(acc, dict):
        name = str(acc.get("label") or acc.get("id") or "")
    log.warning("鸣潮登录成功：key=%s 手机=%s", key[:12], mask_mobile(mobile))
    return True, ("登录成功%s。现在可以直接发「鸣潮」或「鸣潮 角色名」查你自己的号了。"
                  % (("：" + name) if name else ""))


def remove_account(cfg, cache: Cache | None, key: str) -> tuple[bool, str]:
    """注销：把本地凭据里属于这个人的那份删掉。"""
    try:
        call_tool(cfg, "waves_account_remove", {"accountId": key})
    except McpError as exc:
        msg = str(exc)
        if CREDENTIAL_ERROR in msg or "没有找到账号凭据" in msg:
            return False, NOT_LOGGED_IN
        return False, "注销失败：%s" % msg[:300]
    if cache is not None:
        cache.entries.pop("wuwa:accounts", None)
        cache.save()
    return True, "已注销（本地凭据里属于你的那份删掉了）。"


# ------------------------------------------------------------------ 格式化
def _num(v):
    try:
        f = float(v)
        return int(f) if f == int(f) else f
    except (TypeError, ValueError):
        return v


def _account_line(acc: dict | None) -> str:
    if not isinstance(acc, dict):
        return ""
    bits = []
    for key in ("label", "name", "nickname", "accountId"):
        v = acc.get(key)
        if v:
            bits.append(str(v))
            break
    for key in ("roleName", "serverName", "serverId", "userId", "level"):
        v = acc.get(key)
        if v not in (None, ""):
            bits.append(str(v))
    return " · ".join(bits)


def _echo_score(build: dict) -> str:
    sc = build.get("echoScore") if isinstance(build, dict) else None
    if not isinstance(sc, dict):
        return ""
    if not sc.get("available"):
        return ""
    total = _num(sc.get("totalScore"))
    avg = _num(sc.get("averageScore"))
    rank = sc.get("rank") or ""
    out = "声骸评分 %s" % total
    if avg not in (None, ""):
        out += "（均 %s）" % avg
    if rank:
        out += " %s" % rank
    return out


def _char_line(item: dict) -> str:
    """角色列表里的一行：名字、等级、武器、声骸评分。"""
    ch = item.get("character") or {}
    build = item.get("build") or {}
    if not item.get("ok", True) and not build:
        return "  %s（取数据失败：%s）" % (ch.get("roleName") or "?",
                                          item.get("error") or "?")
    name = ch.get("roleName") or (build.get("role") or {}).get("name") or "?"
    lv = ch.get("level") or (build.get("role") or {}).get("level")
    star = ch.get("starLevel") or (build.get("role") or {}).get("starLevel")
    bits = [str(name)]
    if lv not in (None, ""):
        bits.append("Lv%s" % _num(lv))
    if star not in (None, ""):
        bits.append("%s★" % _num(star))
    w = build.get("weapon") or {}
    if w.get("name"):
        bits.append(str(w["name"]))
        if w.get("resonanceLevel") not in (None, ""):
            bits.append("精%s" % _num(w["resonanceLevel"]))
    chain = build.get("chain") or {}
    if chain.get("unlocked") not in (None, ""):
        tot = chain.get("total")
        bits.append("共鸣链 %s%s" % (_num(chain["unlocked"]),
                                     "/%s" % _num(tot) if tot else ""))
    sc = _echo_score(build)
    if sc:
        bits.append(sc)
    return "  " + " · ".join(bits)


def format_build(data: dict, *, verbose: bool = True) -> str:
    """单个角色的详情。"""
    acc = _account_line(data.get("account"))
    ch = data.get("character") or {}
    build = data.get("build") or {}
    role = build.get("role") or {}
    name = ch.get("roleName") or role.get("name") or "?"
    head = "鸣潮 · %s" % name
    if acc:
        head += "（%s）" % acc
    lines = [head]

    meta = []
    lv = ch.get("level") or role.get("level")
    if lv not in (None, ""):
        meta.append("等级 %s" % _num(lv))
    star = ch.get("starLevel") or role.get("starLevel")
    if star not in (None, ""):
        meta.append("%s★" % _num(star))
    attr = role.get("attributeName")
    if attr:
        meta.append(str(attr))
    if meta:
        lines.append(" · ".join(meta))

    chain = build.get("chain") or {}
    if chain.get("unlocked") not in (None, ""):
        lines.append("共鸣链 %s/%s" % (_num(chain["unlocked"]),
                                      _num(chain.get("total"))))

    w = build.get("weapon") or {}
    if w.get("name"):
        wl = "武器：%s" % w["name"]
        if w.get("level") not in (None, ""):
            wl += " Lv%s" % _num(w["level"])
        if w.get("starLevel") not in (None, ""):
            wl += " %s★" % _num(w["starLevel"])
        if w.get("resonanceLevel") not in (None, ""):
            wl += " 精%s" % _num(w["resonanceLevel"])
        lines.append(wl)

    if verbose:
        skills = build.get("skills") or []
        if skills:
            shown = "、".join(
                "%s%s" % (s.get("name") or "?",
                          "" if s.get("level") in (None, "") else _num(s["level"]))
                for s in skills[:6])
            lines.append("技能：" + shown)
        echoes = build.get("echoes") or []
        if echoes:
            lines.append("声骸 %d 个" % len(echoes))
            for e in echoes[:5]:
                en = e.get("name") or e.get("phantomName") or "?"
                cost = e.get("cost")
                mains = "、".join(
                    str(p.get("attributeName") or p.get("name") or "")
                    for p in (e.get("mainProps") or [])[:2] if p)
                seg = "  %s%s" % (en, (" C%s" % cost) if cost else "")
                if mains:
                    seg += " · " + mains
                lines.append(seg)

    sc = _echo_score(build)
    if sc:
        lines.append(sc)
    else:
        reason = ((build.get("echoScore") or {}).get("reason")
                  if isinstance(build.get("echoScore"), dict) else None)
        if reason:
            lines.append("声骸评分：%s" % reason)

    rec = build.get("recommendations") or {}
    if rec.get("lowSkillCount"):
        low = "、".join(str(s.get("name")) for s in
                        (rec.get("lowSkills") or [])[:4])
        lines.append("技能等级偏低（<8）：%s" % (low or rec["lowSkillCount"]))
    if rec.get("missingEchoSlots"):
        lines.append("声骸还缺 %s 个位置" % rec["missingEchoSlots"])
    return "\n".join(lines)


def format_all(data: dict, *, max_chars: int = 1200) -> str:
    """全角色练度汇总。太长就截断 —— 群里发一大坨没人看。"""
    acc = _account_line(data.get("account"))
    head = "鸣潮 · 全角色练度"
    if acc:
        head += "（%s）" % acc
    chars = data.get("characters") or []
    lines = [head, "共 %d 个角色：" % len(chars)]
    body: list[str] = []
    used = len(head)
    for item in chars:
        if not isinstance(item, dict):
            continue
        line = _char_line(item)
        if used + len(line) > max_chars:
            body.append("  …（还有 %d 个，用「鸣潮 角色名」看单个）"
                        % (len(chars) - len(body)))
            break
        body.append(line)
        used += len(line)
    lines.extend(body)

    summary = data.get("summary")
    if isinstance(summary, dict) and summary:
        bits = []
        for k, label in (("total", "角色"), ("averageScore", "平均声骸分"),
                         ("averageLevel", "平均等级")):
            v = summary.get(k)
            if v not in (None, ""):
                bits.append("%s %s" % (label, _num(v)))
        if bits:
            lines.append(" · ".join(bits))
    return "\n".join(lines)


# ------------------------------------------------------------------ 入口
def parse_request(text: str) -> tuple[str, str, int | None]:
    """把参数解析成 (动作, 角色名, 账号序号)。

    动作：
        "list"    列出账号
        "switch"  切换当前账号（角色名位置放的是序号）
        "query"   查数据

    认这几种写法（`@` 是账号序号，**刻意选一个不会跟角色名撞的符号**）：

        鸣潮                 当前账号的全角色
        鸣潮 维里奈           当前账号的某个角色
        鸣潮 账号             列出所有账号
        鸣潮 切换 2           把 2 号设为当前
        鸣潮 @2              第 2 个账号的全角色
        鸣潮 @2 维里奈        第 2 个账号的某个角色
    """
    s = (text or "").strip()
    if not s:
        return "query", "", None

    # @N 前缀（账号序号）
    index: int | None = None
    m = re.match(r"^@\s*(\d+)\s*(.*)$", s)
    if m:
        index = int(m.group(1))
        s = m.group(2).strip()

    if not s:
        return "query", "", index
    head, _, rest = s.partition(" ")
    head_low = head.lower()
    if head_low in [w.lower() for w in LIST_WORDS]:
        return "list", "", index
    if head_low in [w.lower() for w in SWITCH_WORDS]:
        arg = (rest or "").strip().lstrip("@")
        try:
            return "switch", "", int(arg)
        except ValueError:
            return "switch", "", None
    if head_low in [w.lower() for w in LOGIN_WORDS]:
        # 手机号允许带空格 / 横线 / 加号，这里只留数字
        digits = "".join(ch for ch in (rest or "") if ch.isdigit())
        return "login", digits, index
    if head_low in [w.lower() for w in CODE_WORDS]:
        code = "".join(ch for ch in (rest or "") if ch.isdigit())
        return "code", code, index
    if head_low in [w.lower() for w in LOGOUT_WORDS]:
        return "logout", "", index
    return "query", s, index


def query(cfg, cache: Cache | None, selector: str = "",
          account_index: int | None = None,
          account_id: str = "") -> tuple[bool, str]:
    """查一次。返回 (成功?, 要发出去的文本)。

    账号按这个优先级：
      1. `account_id` —— **按人归属**那个（群里每个群友各查各的，见 member_key）
      2. `account_index` —— 序号（1 开始，从「鸣潮 账号」那份列表数）
      3. 配置里的 `default_account`
      4. 都不给 = 平台那边当前激活的那个
    """
    if not getattr(cfg, "enabled", True):
        return False, "鸣潮查询没开。"

    selector = (selector or "").strip()
    account = str(account_id or "").strip() \
        or str(getattr(cfg, "default_account", "") or "").strip()
    if account_index is not None:
        try:
            items = list_accounts(cfg, cache)
        except McpError as exc:
            msg = str(exc)
            if CREDENTIAL_ERROR in msg or "没有找到账号凭据" in msg:
                return False, NOT_LOGGED_IN
            return False, "拿账号列表失败：%s" % msg[:200]
        if not items:
            return False, NOT_LOGGED_IN
        if not 1 <= account_index <= len(items):
            return False, ("没有第 %d 个账号 —— 现在有 %d 个。"
                           "发「鸣潮 账号」看列表。" % (account_index, len(items)))
        account = str(items[account_index - 1].get("id")
                      or items[account_index - 1].get("accountId") or "")

    # **key 必须带上账号**，否则「@1 维里奈」和「@2 维里奈」会互相串。
    key = "wuwa:%s:%s" % (account or "*", selector or "*")
    ttl = float(getattr(cfg, "cache_seconds", 60) or 0)
    if cache is not None and ttl > 0:
        hit = cache.get(key, ttl)
        if hit:
            log.info("鸣潮查询命中缓存：%r 账号=%r",
                     selector or "(全部)", account or "(当前)")
            return True, hit

    tool = "waves_character_build" if selector else "waves_all_character_builds"
    args: dict = {}
    if selector:
        args["selector"] = selector
    if account:
        args["accountId"] = account

    try:
        data = call_tool(cfg, tool, args)
    except McpError as exc:
        msg = str(exc)
        log.warning("鸣潮查询失败：%s", msg)
        if CREDENTIAL_ERROR in msg or "没有找到账号凭据" in msg:
            return False, NOT_LOGGED_IN
        return False, "鸣潮查询失败：%s" % msg[:300]
    except Exception as exc:  # noqa: BLE001
        log.warning("鸣潮查询异常：%r", exc)
        return False, "鸣潮查询出错：%s" % exc

    if not isinstance(data, dict):
        return False, "鸣潮那边返回了看不懂的东西。"

    text = (format_build(data) if selector
            else format_all(data, max_chars=int(
                getattr(cfg, "max_chars", 1200) or 1200)))
    if cache is not None:
        cache.put(key, text)
    log.warning("鸣潮查询 [%s] -> %s%s | 成功",
                selector or "(全部)",
                ("账号 %s " % account_index) if account_index else "",
                "单个角色" if selector else "全角色")
    return True, text
