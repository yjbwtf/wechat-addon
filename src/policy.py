"""安全策略层：白名单、@ 判定、频率限制、消息去重、紧急停止。

这里的所有判断都是「默认拒绝」：拿不准就不回复。
"""

from __future__ import annotations

import logging

import hashlib
import random
import re
import time
from collections import deque
from pathlib import Path

from .config import Config


# --------------------------------------------------------------------------
# 紧急停止开关
# --------------------------------------------------------------------------
class StopSwitch:
    """存在 data/STOP 文件时立即停止一切回复（不发送、不生成）。"""

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / "STOP"

    @property
    def stopped(self) -> bool:
        return self.path.exists()

    def engage(self, reason: str = "") -> None:
        self.path.write_text(
            f"stopped at {time.strftime('%Y-%m-%d %H:%M:%S')}\n{reason}\n",
            encoding="utf-8",
        )

    def release(self) -> None:
        self.path.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# 频率限制
# --------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg.rate_limit
        self._hits: dict[str, deque[float]] = {}
        self._last: dict[str, float] = {}

    def check(self, group: str) -> tuple[bool, str]:
        """只判断「每小时总上限」。

        「两条回复的最小间隔」不在这里拒绝，而是在发送前用
        seconds_until_allowed() 等待——否则开机补回多条时，
        第 2、3 条会因为间隔不足被静默丢掉。
        """
        now = time.time()
        hits = self._hits.setdefault(group, deque())
        while hits and now - hits[0] > 3600:
            hits.popleft()

        if len(hits) >= self.cfg.per_group_per_hour:
            return False, f"该群本小时已达上限 {self.cfg.per_group_per_hour} 条"
        return True, ""

    def seconds_until_allowed(self, group: str) -> float:
        """距离可以再次回复还需要等多少秒。"""
        last = self._last.get(group)
        if last is None:
            return 0.0
        return max(0.0, self.cfg.min_interval_seconds - (time.time() - last))

    def commit(self, group: str) -> None:
        now = time.time()
        self._hits.setdefault(group, deque()).append(now)
        self._last[group] = now

    def reply_delay(self) -> float:
        lo, hi = self.cfg.reply_delay
        if hi <= lo:
            return max(0.0, lo)
        return random.uniform(lo, hi)

    def reply_delay_range(self, span: tuple[float, float]) -> float:
        """分段发送时，段与段之间的随机间隔。"""
        lo, hi = span
        if hi <= lo:
            return max(0.0, lo)
        return random.uniform(lo, hi)


# --------------------------------------------------------------------------
# 消息去重
# --------------------------------------------------------------------------
class Deduper:
    def __init__(self, window_seconds: int) -> None:
        self.window = max(1, int(window_seconds))
        self._seen: dict[str, float] = {}

    def seen(self, key: str) -> bool:
        now = time.time()
        if len(self._seen) > 5000:  # 防止无限增长
            self._seen = {k: v for k, v in self._seen.items() if now - v < self.window}
        last = self._seen.get(key)
        self._seen[key] = now
        return last is not None and now - last < self.window


def make_msg_key(group: str, sender: str, content: str, stamp: str = "") -> str:
    raw = f"{group}\x1f{sender}\x1f{content}\x1f{stamp}"
    # errors="replace"：终端编码异常时可能收到代理字符，这里保证不抛异常
    return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()


# --------------------------------------------------------------------------
# @ 判定
# --------------------------------------------------------------------------
_MENTION_CLEANUP = re.compile(r"@\S+[\s\u2005\u00a0]*")


def strip_mention(text: str, names: list[str]) -> tuple[str, bool]:
    """识别并去掉 @机器人。返回 (清理后的文本, 是否被 @)。"""
    text = text or ""
    if not names:
        return text.strip(), False

    mentioned = False
    cleaned = text
    for name in names:
        name = name.strip()
        if not name:
            continue
        pattern = re.compile(r"@\s*" + re.escape(name) + r"[\s\u2005\u00a0]*")
        if pattern.search(cleaned):
            mentioned = True
            cleaned = pattern.sub("", cleaned)
        elif name and name in cleaned:
            # 有些客户端 @ 后不带可见昵称，退化为包含判断
            mentioned = True

    if mentioned:
        # 去掉残留的孤立 @
        cleaned = re.sub(r"^\s*@\s*", "", cleaned)
    return cleaned.strip(), mentioned


# --------------------------------------------------------------------------
# 白名单
# --------------------------------------------------------------------------
class Gatekeeper:
    """把一次入站消息判定为「处理 / 丢弃 + 原因」。"""

    def __init__(self, cfg: Config) -> None:
        self._warned_trusted = False
        self.cfg = cfg
        self.limiter = RateLimiter(cfg)
        self.deduper = Deduper(cfg.dedup_window_seconds)

    def authorize_command(self, sender: str, identity: dict | None = None,
                          *, loose: bool = False) -> bool:
        """这个发送者有没有群内控制权。

        ## 为什么不能只看群昵称

        群昵称是**群成员自己就能改的**，而且全群可见。别人把昵称改成你的名字，
        就拿到了控制权。所以这里改用「多别名匹配」：

        微信里同一个人有好几个名字，可靠程度差别很大：

        | 来源 | 群里其他人看得到吗 | 自己能改吗 | 可靠程度 |
        |---|---|---|---|
        | 群昵称（msg.sender 常见值） | 看得到 | 能改 | 最弱 |
        | 备注 display_name | **看不到**（只有机器人账号自己看得到） | 改不了 | 中 |
        | 微信昵称 nickname | 看得到 | 能改 | 弱 |
        | **微信号 id** | 基本看不到 | **改不了** | **最强** |

        备注那一层是用户点出来的关键性质：机器人账号给「大号」设的备注，
        群成员根本不知道字符串是什么，所以拿它当口令比群昵称强得多。
        但要注意：**冒用者只要把自己的群昵称改成同一个字符串，
        msg.sender 也会等于那个串**，所以「备注」单独用仍然不保险，
        真正防冒用要靠微信号。

        `identity_mode: id` 是严格模式：只认微信号，而且必须是这次真的
        读到了微信号才放行（读不到就拒绝，不退回昵称）。

        `loose=True` 强制走宽松匹配。**急停和恢复必须用它** ——
        否则一旦 sender_info() 读不到微信号，主人会被自己的严格模式锁在门外，
        机器人既停不下来也起不来。
        """
        cfg = self.cfg.commands
        allowed = {s.strip().lower() for s in (cfg.allowed_senders or []) if str(s).strip()}
        if not allowed:
            return False

        info = identity or {}
        if cfg.identity_mode == "id" and not loose:
            wid = str(info.get("id") or "").strip().lower()
            return bool(wid) and wid in allowed

        # 宽松模式：任何一个别名对上就算过
        names = {str(sender or "").strip().lower()}
        for key in ("id", "display_name", "nickname", "group_nickname",
                    "alias", "remark"):
            value = str(info.get(key) or "").strip().lower()
            if value:
                names.add(value)
        names.discard("")
        return bool(names & allowed)

    def check_secret(self, body: str, *, trusted: bool = False) -> bool:
        """校验口令。没配置口令、或来自可信会话，就直接通过。

        可信会话免口令是安全的：私聊的发送者没法冒充。群里**不能**这么做，
        因为 identity_mode=any 时群昵称谁都能改。
        """
        if trusted:
            return True
        secret = (self.cfg.commands.secret or "").strip()
        if not secret:
            return True
        return secret in (body or "")

    def strip_secret(self, body: str) -> str:
        """把口令从指令正文里摘掉，剩下的才是真正的指令词。"""
        secret = (self.cfg.commands.secret or "").strip()
        if not secret:
            return body
        return (body or "").replace(secret, " ").strip()

    def as_command(self, content: str, *, trusted: bool = False) -> str | None:
        """消息里出现 prefix 就当作指令，返回指令正文（没有则返回 None）。

        用 find 而不是 startswith：允许「@机器人 \\stop」这种带 @ 的写法。
        """
        cfg = self.cfg.commands
        if not cfg.enabled:
            return None

        if trusted:
            # 可信会话（自己的私聊）：只认**开头**的短前缀。
            # 不做 find：/ 在正常句子里太常见（路径、网址、分数），
            # 只有出现在最前面才当成指令。
            sp = (cfg.short_prefix or "").strip()
            if sp:
                stripped = (content or "").lstrip()
                # 允许「@机器人 /cmd」这种写法
                while stripped.startswith("@"):
                    sp_at = stripped.find(" ")
                    if sp_at < 0:
                        break
                    stripped = stripped[sp_at + 1:].lstrip()
                if stripped.startswith(sp):
                    return stripped[len(sp):].strip()

        if not cfg.prefix:
            return None
        index = content.find(cfg.prefix)
        if index < 0:
            return None
        return content[index + len(cfg.prefix):].strip()

    def public_command(self, group: str, content: str) -> str | None:
        """群里的**公开指令**：`@机器人 /查询 原神 618285856`。返回指令正文。

        和 `as_command()` 是两条完全不同的路，别混淆：

        | | `as_command`（控制指令） | `public_command`（公开指令） |
        |---|---|---|
        | 前缀 | 群 `\\` / 私聊 `/`（只认开头） | `enka.prefix`（默认 `/`） |
        | 谁能用 | 授权名单 + 口令 | **群里任何人** |
        | 白名单群 | **不检查**（主人可从任何群控制） | **必须检查**（在 main 里，走 prefilter） |
        | 内容 | 停/继续/重启/截图/agent… | 只有查账号这一件事 |

        为什么群里的公开指令要另立前缀：`\\` 开头的一律要口令，而群友不知道
        口令；把查询混进 `\\` 体系就得给群友发口令，等于口令全群公开。

        **@ 是硬要求**（`require_at` 生效时）：没有 @ 就不当指令，免得群里
        正常聊到「/」开头的网址、路径、分数时被误触发。
        """
        cfg = self.cfg
        if not cfg.commands.enabled or not cfg.enka.enabled:
            return None
        if not cfg.enka.allow_group_members:
            return None
        prefix = (cfg.enka.prefix or "").strip()
        if not prefix:
            return None

        text, mentioned = self.mention(content)
        trusted = self.is_trusted_chat(group)
        if cfg.require_at and not mentioned and not trusted:
            return None
        text = (text or "").lstrip()
        # 只认开头：`/` 在正常句子里太常见（路径、网址、分数）
        if not text.startswith(prefix):
            return None
        return text[len(prefix):].strip()

    def is_trusted_chat(self, group: str) -> bool:
        """这个会话是不是「可信会话」（免 @、免口令）。

        匹配时忽略大小写和首尾空白，并且**会警告把群名填进来的情况** ——
        那等于开了个后门：identity_mode=any 时群昵称谁都能改。
        """
        want = {str(n).strip().lower() for n in (self.cfg.commands.trusted_chats or [])}
        want.discard("")
        if not want:
            return False
        hit = str(group or "").strip().lower() in want
        if hit and not self._warned_trusted:
            self._warned_trusted = True
            logging.getLogger(__name__).warning(
                "可信会话「%s」已启用：里面的指令**不需要口令**。"
                "如果这是个群，请立刻从 trusted_chats 里删掉 —— "
                "identity_mode=any 时群昵称谁都能改，等于把机器人控制权交出去。",
                group,
            )
        return hit

    def command_table(self, words: dict[str, list[str]] | None = None
                      ) -> list[tuple[str, list[str]]]:
        """返回 (类别, 词表) 的完整表。给分类和「认不出时的提示」共用。"""
        cfg = self.cfg.commands
        table = [
            ("restart", cfg.restart_words),
            ("stop", cfg.stop_words),
            ("start", cfg.start_words),
            ("status", cfg.status_words),
            ("meme", cfg.meme_words),
        ]
        for name, lst in (words or {}).items():
            table.append((name, lst))
        return table

    #: 认不出指令时给用户看的说明（类别 -> 人话）
    ACTION_LABEL = {
        "stop": "停一下", "start": "继续", "status": "看状态",
        "meme": "教梗", "agent": "让本机 agent 干活", "shot": "截屏",
    }

    def classify_command(self, body: str, words: dict[str, list[str]] | None = None) -> str | None:
        """把指令正文归类。

        三级匹配，从严格到宽松：

          1. **第一个词精确等于**某个指令词  —— `\\截图`、`\\agent …`
          2. **第一个词以指令词开头**        —— `\\截图给我`、`\\开一条龙` -> 截图/开
          3. **指令词出现在第一个词里面**    —— `\\桌面截图给我` -> 截图

        为什么要 2 和 3：原来只做第 1 级，用户写「\\桌面截图给我」这种
        顺口的说法就认不出来，而且当时是**静默忽略**，看起来像机器人死了。
        实测踩过：`收到无法识别的指令 ... '桌面截图给我 ccb'`。

        2、3 级只对**长度 ≥2 的指令词**生效。单字词（「停」「梗」）只走精确匹配，
        否则「\\这个梗怎么来的」会被误判成教梗指令。
        按词长从长到短试，长的更具体、优先。
        """
        word = (body or "").strip().lower()
        if not word:
            return None
        first = re.split(r"[\s,，。!！?？|]+", word)[0]
        table = self.command_table(words)
        norm = [(name, [str(w).strip().lower() for w in (lst or []) if str(w).strip()])
                for name, lst in table]

        # 1) 精确
        for name, lst in norm:
            if first in lst:
                return name

        # 候选按词长降序 —— 「截图」比「截」更具体，先试长的
        all_words = sorted(((w, name) for name, lst in norm for w in lst),
                           key=lambda x: -len(x[0]))
        # 2) 前缀。**允许单字**：`\\开一条龙`、`\\停一下` 这类要能认。
        #    顺序是从长到短，所以单字不会抢掉更具体的词。
        for w, name in all_words:
            if w and first.startswith(w):
                return name
        # 3) 包含。**要求 >= 2 字符**：否则「这个梗怎么来的」会被单字「梗」
        #    命中、误判成教梗指令。
        for w, name in all_words:
            if len(w) >= 2 and w in first:
                return name
        # 4) 子序列：指令词的字符**按顺序**出现在第一个词里。
        #    中文会往词中间插字（截图 -> 截个图 / 截张图 / 截一下图），
        #    穷举变体补不完，这一级才是根治。
        #    限制跨度是为了防长句误判：「随便说点什么」里偶尔凑出两个
        #    字符就命中，那不是指令。跨度上限 = 词长 + 3。
        for w, name in all_words:
            if len(w) < 2:
                continue          # 单字词满世界命中，不参与
            pos = 0
            start = -1
            end = -1
            for i, ch in enumerate(first):
                if pos < len(w) and ch == w[pos]:
                    if start < 0:
                        start = i
                    end = i
                    pos += 1
            if pos == len(w) and (end - start + 1) <= len(w) + 3:
                return name
        return None

    def command_words(self, words: dict[str, list[str]] | None = None) -> str:
        """给「认不出指令」时用的提示文本。"""
        parts = []
        for name, lst in self.command_table(words):
            ws = [str(w).strip() for w in (lst or []) if str(w).strip()]
            if not ws:
                continue
            head = ws[0]
            label = self.ACTION_LABEL.get(name)
            parts.append(f"{head}（{label}）" if label else head)
        return "、".join(parts)

    def strict_command_kind(self, body: str,
                            words: dict[str, list[str]]) -> str | None:
        """**严格**归类：第一个词必须等于指令词、或者以它开头。

        和 `classify_command` 的区别就是**不做包含/子序列匹配**：

        | 写法 | classify_command | strict_command_kind |
        |---|---|---|
        | `查询 原神 123` | enka | enka |
        | `查询一下 原神 123` | enka | enka |
        | `不是查询`    | **enka**（包含） | None |
        | `查一下`      | None | None |

        为什么公开指令必须用这个：`classify_command` 的宽匹配是给**授权者**
        打错字用的（`\\桌面截图给我` 要能认出来），而那条路上有口令和授权名单
        兜着。公开指令面向全群、无口令 —— 宽匹配等于把「/随便说点什么」
        也变成入口。
        """
        first = re.split(r"[\s,，。!！?？|]+", (body or "").strip().lower())[0]
        if not first:
            return None
        for name, lst in (words or {}).items():
            for w in sorted((str(x).strip().lower() for x in (lst or [])),
                            key=len, reverse=True):
                if w and (first == w or first.startswith(w)):
                    return name
        return None

    def command_args(self, body: str) -> str:
        """取指令词后面的参数部分，例如「梗 add ccb | 踩踩背」-> 「add ccb | 踩踩背」。"""
        word = (body or "").strip()
        m = re.match(r"^\S+\s*(.*)$", word, re.S)
        return (m.group(1) if m else "").strip()

    def prefilter(self, group: str, msg_key: str, *, is_bot: bool = False) -> tuple[bool, str]:
        """与「@ / 频率」无关的通用过滤：白名单、自己发的、重复消息。

        聚合模式下，被 @ 之后的后续消息是**不带 @** 的，
        不能走 accept()（那里会因为没有 @ 而拒掉），
        所以把这一段单独拆出来复用。
        """
        if not self.cfg.whitelist_groups:
            return False, "白名单为空"
        if group not in self.cfg.whitelist_groups:
            return False, f"「{group}」不在白名单"
        if is_bot and self.cfg.ignore_self:
            return False, "自己发的消息"
        if self.deduper.seen(msg_key):
            return False, "重复消息"
        return True, ""

    def mention(self, content: str) -> tuple[str, bool]:
        """去掉 @机器人，返回 (剩余文本, 是否被 @)。"""
        return strip_mention(content, self.cfg.bot_names)

    def accept(self, group: str, sender: str, content: str, msg_key: str,
               *, is_bot: bool = False) -> tuple[bool, str, str]:
        """返回 (是否处理, 清理后的文本, 丢弃原因)。"""
        ok, reason = self.prefilter(group, msg_key, is_bot=is_bot)
        if not ok:
            return False, "", reason

        text, mentioned = self.mention(content)
        # 可信会话里不用 @：那本来就是你自己在跟自己说话
        trusted = self.is_trusted_chat(group)
        if self.cfg.require_at and not mentioned and not trusted:
            return False, "", "没有 @ 机器人"
        # 内容为空**不再拦截**：只 @ 一下机器人也该有回应。
        # 空内容会在 main.py 里换成一句提示再喂给模型。

        ok, reason = self.limiter.check(group)
        if not ok:
            return False, "", reason

        return True, text, ""
