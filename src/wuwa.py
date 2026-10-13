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
def query(cfg, cache: Cache | None, selector: str = "") -> tuple[bool, str]:
    """查一次。返回 (成功?, 要发出去的文本)。"""
    if not getattr(cfg, "enabled", True):
        return False, "鸣潮查询没开。"

    selector = (selector or "").strip()
    key = "wuwa:" + (selector or "*")
    ttl = float(getattr(cfg, "cache_seconds", 60) or 0)
    if cache is not None and ttl > 0:
        hit = cache.get(key, ttl)
        if hit:
            log.info("鸣潮查询命中缓存：%r", selector or "(全部)")
            return True, hit

    tool = "waves_character_build" if selector else "waves_all_character_builds"
    args: dict = {}
    if selector:
        args["selector"] = selector
    account = str(getattr(cfg, "default_account", "") or "").strip()
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
    log.warning("鸣潮查询 [%s] -> %s | 成功",
                selector or "(全部)", "单个角色" if selector else "全角色")
    return True, text
