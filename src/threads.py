"""连续对话线程：被 @ 一次之后，那个人接着说话不用再 @。

为什么要按人分开
----------------
原来的做法是「每群记一个最近回复时间」，简单但不够：
群里两个人同时跟你说话时，你分不清哪句是谁的后续。
所以现在按 (群, 人) 维护独立的线程，每个线程各有自己的话题和寿命。

引用是显式的路标
----------------
wxauto4 会给出 quote_nickname / quote_content（实测：
content='回复正文'，quote_nickname='<机器人昵称>'，quote_content='被引用的原文'）。
所以「右键引用」可以被精确识别：
  - 引用的是机器人 -> 明确是在跟机器人说话，即使窗口过期也重新接上
  - 引用的是别人   -> 明确不是跟机器人说话，直接关掉这个人的线程

相关性判定必须是免费的
----------------------
用户明确要求继续省 token。所以这里**不调用模型**，用字符二元组的重合度
加上一组「这句话是不是对别人说的」的信号来打分。中文二元组不需要分词，
对「同一话题」的判别足够用，而且一次判断成本是零。
判错的代价也可控：判成"无关"只是不回复，判成"有关"才花一次调用，
所以阈值偏保守（宁可不接）。

真要拿不准时，还有第二道闸：提示词里让模型在「明显不是对我说」时
只回一个哨兵字符，程序看到就不发出去。那一次调用的输出只有一两个 token。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import ThreadConfig

# 做二元组时要丢掉的常见虚词（减少"的了呢"这类噪音造成的假重合）
_FILLER = set("的了吗呢啊吧呀嘛哦嗯哈我你他她它是不在有和就都也很才还这那"
              "什么怎么可以一个这个那个我们你们他们")

_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]+")

# 问句、第二人称、反应词——都是"这句话是对机器人说的"的信号
_QUESTION = re.compile(r"[?？]|吗|呢|怎么|为什么|为啥|多少|哪个|哪儿|是否|能不能|可不可以")
_SECOND_PERSON = re.compile(r"你|您|你们|咱|机器人")
# 纯反应：哈哈哈哈 / 6 / 笑死 / 牛 / ？——这些通常就是在应机器人上一句
_REACTION = re.compile(r"^[\s哈嘿嘻呵笑6牛草靠？?！!。.~～ㅋㅎ这那行好嗯哦啊哎唉诶草]{1,8}$")
# @ 别人
_AT_OTHER = re.compile(r"@\s*([^\s\u2005\u00a0]{1,24})")


def bigrams(text: str) -> set[str]:
    """字符二元组。中文不用分词也能反映话题重合。"""
    t = _PUNCT.sub("", text or "")
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


@dataclass
class Thread:
    group: str
    sender: str
    opened_at: float
    last_at: float = 0.0
    turns: int = 0
    # 话题指纹：这个人最近几句话的二元组
    terms: set[str] = field(default_factory=set)
    last_bot_reply: str = ""
    # 上一次判定出来的相关度，只用于日志
    last_score: float = 0.0

    def age(self) -> float:
        return max(0.0, time.time() - self.last_at)

    def remember(self, text: str) -> None:
        self.terms |= bigrams(text)
        # 别无限涨，超了就保留最近写入的那批
        if len(self.terms) > 400:
            self.terms = set(list(self.terms)[-200:])


@dataclass
class Verdict:
    ok: bool
    score: float
    why: str


class ThreadBook:
    def __init__(self, cfg: ThreadConfig, path: Path | str | None = None) -> None:
        self.cfg = cfg
        self.path = Path(path) if path else None
        self._threads: dict[tuple[str, str], Thread] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    # ------------------------------------------------------------ 存盘
    def save(self) -> None:
        """把线程落盘。

        只存内存的话，重启一次所有「正在聊的话题」就没了 —— 实测用户
        重启机器人之后接着说一句，机器人完全不认识他，体验很差。
        """
        if not (self.cfg.persist and self.path):
            return
        now = time.time()
        data = {
            "saved_at": now,
            "threads": [
                {
                    "g": t.group, "u": t.sender,
                    "opened": t.opened_at, "last": t.last_at,
                    "turns": t.turns,
                    "terms": sorted(t.terms)[-200:],
                    "reply": t.last_bot_reply[:120],
                }
                for t in self._threads.values()
                if now - t.last_at <= max(1.0, self.cfg.window_seconds)
            ],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            logging.getLogger("wechat-bot.conv").warning("线程存盘失败：%s", exc)

    def load(self) -> int:
        """从盘上恢复线程，返回恢复了几条。"""
        if not (self.cfg.persist and self.path and self.path.exists()):
            return 0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        now = time.time()
        window = max(1.0, self.cfg.window_seconds)
        count = 0
        for item in data.get("threads") or []:
            try:
                last = float(item.get("last") or 0)
                if now - last > window:
                    continue
                t = Thread(
                    group=str(item["g"]), sender=str(item["u"]),
                    opened_at=float(item.get("opened") or last), last_at=last,
                    turns=int(item.get("turns") or 0),
                    terms=set(item.get("terms") or []),
                    last_bot_reply=str(item.get("reply") or ""),
                )
            except (KeyError, TypeError, ValueError):
                continue
            self._threads[(t.group, t.sender)] = t
            count += 1
        return count

    # ------------------------------------------------------------ 增删查
    def get(self, group: str, sender: str) -> Thread | None:
        t = self._threads.get((group, sender))
        if t is None:
            return None
        if t.age() > max(1.0, self.cfg.window_seconds):
            self._threads.pop((group, sender), None)
            return None
        return t

    def open(self, group: str, sender: str, text: str,
             bot_reply: str = "") -> Thread:
        now = time.time()
        key = (group, sender)
        t = self._threads.get(key)
        if t is None or t.age() > max(1.0, self.cfg.window_seconds):
            t = Thread(group=group, sender=sender, opened_at=now, last_at=now)
            self._threads[key] = t
        t.last_at = now
        t.turns += 1
        t.remember(text)
        if bot_reply:
            t.last_bot_reply = bot_reply
            t.remember(bot_reply)
        return t

    def close(self, group: str, sender: str) -> None:
        self._threads.pop((group, sender), None)

    def close_group(self, group: str) -> None:
        for key in [k for k in self._threads if k[0] == group]:
            self._threads.pop(key, None)

    def open_threads(self, group: str) -> list[Thread]:
        return [t for (g, _), t in self._threads.items() if g == group]

    # ------------------------------------------------------------ 判定
    def judge(self, group: str, sender: str, text: str, *,
              bot_names: list[str], members: list[str] | None = None,
              quote_nickname: str = "", bot_spoke_last: bool = False,
              silent_seconds: float = 0.0) -> Verdict:
        """这条没 @ 的消息，算不算在跟机器人接着聊？

        判定方向很重要：**默认接，只排除明确不是对机器人说的**。
        反过来（默认不接，要凑够相关度才接）实测是行不通的——
        「那要不要带伞」这种明显在接着问的话，和上文一个字都不重合，
        按打分会判成不相关，然后线程就被关掉了，体验很差。

        所以这里只用几条**否决**条件：
          - @ 了别人
          - 提到了别的群友昵称
          - 又长又和上文毫无关系（基本是在跟别人聊别的事）
          - 这个话题已经聊够轮数了
        剩下的都算接着聊；真判错了还有第二道闸——提示词里让模型只回一个
        减号，程序看到就不发出去。

        `bot_spoke_last` 是一条独立的、很强的信号：如果群里**最后一条消息就是
        机器人说的**，那下一个开口的人几乎一定是在跟它说话。实测踩过：
        机器人 13:56 回完，群里 11 分钟没人说话，14:08 有人冒一句
        「干嘛的不回话」——没有线程、没有 @，被判成"没有进行中的对话"丢掉了，
        用户看到的就是"它不理我"。这条规则正好覆盖这种情况，
        而且一旦有别人插话就立刻失效，不会一直黏着。
        """
        if not self.cfg.enabled:
            return Verdict(False, 0.0, "连续对话已关闭")

        # 引用是最强的信号，先处理
        if quote_nickname:
            if quote_nickname in bot_names:
                return Verdict(True, 99.0, "引用了机器人的消息")
            if quote_nickname == sender:
                return Verdict(True, 50.0, "引用了自己刚说的话")
            return Verdict(False, -99.0, f"引用的是「{quote_nickname}」，不是机器人")

        thread = self.get(group, sender)
        fresh = False
        if thread is None:
            if not (self.cfg.answer_when_last and bot_spoke_last
                    and silent_seconds <= max(1.0, self.cfg.dangling_window)):
                return Verdict(False, 0.0, "没有进行中的对话")
            # 临时开一条：最后一句是机器人说的，那这句大概率是接它的话
            thread = self.open(group, sender, "")
            fresh = True

        if thread.turns >= max(1, self.cfg.max_turns):
            return Verdict(False, 0.0,
                           f"这个话题已经聊了 {thread.turns} 轮，先收住")

        body = (text or "").strip()
        why = ["最后一句是机器人说的" if fresh else "有进行中的对话"]
        score = 2.5 if fresh else 2.0

        # ---- 否决条件 ----
        for hit in _AT_OTHER.findall(body):
            if hit not in bot_names:
                self.close(group, sender)
                return Verdict(False, -5.0, f"@ 的是别人（{hit}）")

        for name in (members or []):
            if name and name not in bot_names and len(name) >= 2 and name in body:
                self.close(group, sender)
                return Verdict(False, -3.0, f"提到的是群友（{name}）")

        ov = _overlap(bigrams(body), thread.terms)
        if len(body) > self.cfg.long_chars and ov < 0.08:
            self.close(group, sender)
            return Verdict(False, -3.0,
                           f"{len(body)} 字长文且和上文零重合，像是在跟别人说")

        # ---- 只是记录参考信号，不再用来卡门槛 ----
        if _QUESTION.search(body):
            score += 1
            why.append("是问句")
        if _SECOND_PERSON.search(body):
            score += 1
            why.append("在说「你」")
        if _REACTION.match(body):
            score += 1
            why.append("是短反应")
        if ov >= 0.20:
            score += 1
            why.append(f"话题重合 {ov:.0%}")
        elif ov > 0:
            why.append(f"略有重合 {ov:.0%}")
        else:
            why.append("字面不重合但也没指向别人")

        return Verdict(True, score, "，".join(why))


def format_thread_hint(turns: int) -> str:
    """给提示词用的极短标记：让模型知道这是在连续对话里。"""
    return f"连续第{turns}轮"
