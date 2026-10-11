"""连续消息聚合：被 @ 之后先别急着回，听人把话说完。

为什么需要这一层
----------------
真人聊天不是一问一答的接口调用。群里很常见的节奏是：

    @蓝色大肥鱼
    我问你个事
    就是那个……
    算了你直接说吧，北京明天天气

如果每收到一条就调用一次大脑，机器人会在第一句「@蓝色大肥鱼」之后
立刻抢答一句「在呢在呢」，等对方把真正的问题打完，它已经说完了，
场面非常像机器人。

这里做的事
----------
1. 被 @ 的那条消息**不立刻回复**，而是开一个「会话」（Turn），
   同时把此刻的历史记录快照下来（快照用来喂上下文，
   保证聚合进来的这几条不会被重复算两遍）。
2. 之后这个群里来的消息（在 followup_sender 允许的范围内）
   继续挂进同一个会话，并把静默计时器清零。
3. 满足任一条件就认为「对方说完了」，整段整合成一次请求：
   - 距最后一条消息 quiet_seconds 秒内没有新的（正常情况）
   - 从第一句起已经过了 max_wait_seconds（对方一直发个不停）
   - 已经攒够 max_messages 条

聚合后的正文形如：

    —— 张三 说：我问你个事
    —— 张三 说：北京明天天气

模型看到的是一个整体，只回一次。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .config import AggregateConfig
from .history import StoredMessage

# 只 @ 了一下、什么都没说时喂给模型的正文。
EMPTY_BODY = "（只 @ 了你一下，没有说别的内容）"

# 静默时间下限。配成 0 会变成「立刻回复」，等于关掉聚合，
# 但仍然允许显式配置 0（有人就想要立即回）。
_MIN_QUIET = 0.0


@dataclass
class TurnItem:
    sender: str
    content: str
    at: float = 0.0


@dataclass
class Turn:
    """一次待回复的发言。"""

    group: str
    trigger_sender: str
    items: list[TurnItem] = field(default_factory=list)
    open_at: float = 0.0
    last_at: float = 0.0
    # 触发这条 @ 之前的历史快照。聚合进来的消息本身也会落库，
    # 所以上下文必须用快照，否则同一句话会在 prompt 里出现两遍。
    history: list[StoredMessage] = field(default_factory=list)
    # 触发这条是「引用机器人的消息」时记下被引用的人，用来给提示词加标记
    quote_nickname: str = ""
    quote_content: str = ""
    # 不是明确 @ 进来的（靠启发式判定接上的）：允许模型自己说「不是跟我说的」
    may_decline: bool = False

    def senders(self) -> list[str]:
        seen: list[str] = []
        for item in self.items:
            if item.sender and item.sender not in seen:
                seen.append(item.sender)
        return seen

    def has_content(self) -> bool:
        return any(item.content.strip() for item in self.items)

    def body(self) -> str:
        """拼给模型的正文。"""
        lines = []
        for item in self.items:
            text = (item.content or "").strip()
            if not text:
                continue
            if len(self.items) == 1:
                return text
            lines.append(f"—— {item.sender} 说：{text}")
        if not lines:
            return EMPTY_BODY
        return "\n".join(lines)

    def is_multi(self) -> bool:
        return len(self.items) > 1

    def elapsed(self) -> float:
        return max(0.0, self.last_at - self.open_at)


class Aggregator:
    """按群维护「正在收集」的会话。"""

    def __init__(self, cfg: AggregateConfig) -> None:
        self.cfg = cfg
        self._turns: dict[str, Turn] = {}

    # -------------------------------------------------------------- 查询
    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    def is_collecting(self, group: str) -> bool:
        return group in self._turns

    def open_groups(self) -> list[str]:
        return list(self._turns)

    def pending_count(self) -> int:
        return len(self._turns)

    # -------------------------------------------------------------- 写入
    def trigger(self, group: str, sender: str, text: str,
                history: list[StoredMessage]) -> Turn:
        """收到一条 @ 机器人的消息：开新会话或并入已有会话。"""
        now = time.time()
        turn = self._turns.get(group)
        if turn is None:
            turn = Turn(
                group=group,
                trigger_sender=sender,
                open_at=now,
                last_at=now,
                history=list(history),
            )
            self._turns[group] = turn
        turn.items.append(TurnItem(sender=sender, content=text, at=now))
        turn.last_at = now
        return turn

    def followup(self, group: str, sender: str, text: str) -> bool:
        """把一条后续消息并入正在收集的会话。返回是否被吸收。"""
        turn = self._turns.get(group)
        if turn is None:
            return False
        if self.cfg.followup_sender == "same" and sender != turn.trigger_sender:
            return False
        now = time.time()
        turn.items.append(TurnItem(sender=sender, content=text, at=now))
        turn.last_at = now
        return True

    def drop(self, group: str) -> Turn | None:
        """丢弃某群正在收集的内容（收到控制指令时用）。"""
        return self._turns.pop(group, None)

    def clear(self) -> None:
        self._turns.clear()

    # -------------------------------------------------------------- 到期
    def due(self, now: float | None = None) -> list[Turn]:
        """取出所有「对方已经说完」的会话，并从收集队列里移除。"""
        now = time.time() if now is None else now
        ready: list[Turn] = []
        for group in list(self._turns):
            turn = self._turns[group]
            quiet = now - turn.last_at >= max(_MIN_QUIET, self.cfg.quiet_seconds)
            waited = (self.cfg.max_wait_seconds > 0
                      and now - turn.open_at >= self.cfg.max_wait_seconds)
            full = len(turn.items) >= max(1, self.cfg.max_messages)
            if quiet or waited or full:
                ready.append(self._turns.pop(group))
        return ready

    def wait_hint(self, group: str) -> str:
        """日志用：还剩几秒。"""
        turn = self._turns.get(group)
        if turn is None:
            return ""
        left = max(0.0, self.cfg.quiet_seconds - (time.time() - turn.last_at))
        return f"{len(turn.items)} 条，静默倒计时 {left:.1f}s"
