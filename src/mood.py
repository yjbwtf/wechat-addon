"""心情值 / 好感度：让机器人对每个人的态度不一样。

为什么要有这个
--------------
提示词里写「被怼了会回嘴」是一句空话——模型每次都是白纸一张，
记不住「这个人刚才骂过我」。心情值把这件事变成一个**持久化的数字**，
按人记，并且每次都作为一行极短的上下文注入。

数值怎么变
----------
**不额外调用模型**。做法是在同一次回复的末尾让模型附带一个标记：

    哈？母的就得挨踩啊？你这踩背瘾挺大喵
    [mood:-6]

程序把它解析出来、从正文里删掉，然后落到库里。代价只有几个输出 token，
比「先回复、再单独问一次模型这个人该扣几分」便宜得多。

数值范围 1~100，默认 100（一视同仁地友好），被冒犯了往下掉，
聊得开心可以回升。注入格式刻意做得极短：
    正常：  [MOOD] 张三=82
    偏低：  [MOOD] 张三=25（不太想搭理他，话少一点、别热情）
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from pathlib import Path

from .config import MoodConfig

SCHEMA = """
CREATE TABLE IF NOT EXISTS affinity (
    group_id   TEXT    NOT NULL,
    sender     TEXT    NOT NULL,
    value      INTEGER NOT NULL DEFAULT 100,
    note       TEXT    NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (group_id, sender)
);
"""

# 模型在正文末尾附带的情绪标记。写得很短，输出代价可以忽略。
_MOOD_TAG = re.compile(
    r"[\[【(（]\s*mood\s*[:：]\s*([+-]?\d{1,3})\s*([^\]】)）\n]{0,40})[\]】)）]",
    re.I,
)


def parse_mood_tag(reply: str) -> tuple[str, int, str]:
    """从回复里摘出 [mood:N] 标记。返回 (去掉标记的正文, 增减值, 原因)。"""
    text = reply or ""
    m = None
    for cand in _MOOD_TAG.finditer(text):
        m = cand                      # 取最后一个，避免正文里偶然出现
    if not m:
        return text, 0, ""
    delta = int(m.group(1))
    note = (m.group(2) or "").strip(" -—:：")
    cleaned = (text[:m.start()] + text[m.end():]).strip()
    return cleaned or text, delta, note


class MoodStore:
    def __init__(self, db_path: Path | str, cfg: MoodConfig) -> None:
        self.cfg = cfg
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._cache: dict[tuple[str, str], tuple[int, str, float]] = {}

    def close(self) -> None:
        self._conn.close()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    # ---------------------------------------------------------------- 读
    def get(self, group: str, sender: str) -> tuple[int, str]:
        key = (group, sender)
        hit = self._cache.get(key)
        if hit and time.time() - hit[2] < 30:
            return hit[0], hit[1]
        row = self._conn.execute(
            "SELECT value, note FROM affinity WHERE group_id = ? AND sender = ?",
            (group, sender),
        ).fetchone()
        if row is None:
            value, note = self.cfg.initial, ""
        else:
            value, note = int(row[0]), str(row[1] or "")
        self._cache[key] = (value, note, time.time())
        return value, note

    # ---------------------------------------------------------------- 写
    def adjust(self, group: str, sender: str, delta: int, note: str = "") -> int:
        """按 AI 的判断调整心情值，返回调整后的值。"""
        if not self.cfg.enabled or not delta:
            return self.get(group, sender)[0]

        step = max(-self.cfg.max_down, min(self.cfg.max_up, int(delta)))
        current, old_note = self.get(group, sender)
        value = max(self.cfg.floor, min(self.cfg.ceiling, current + step))
        merged = note or old_note
        self._conn.execute(
            "INSERT INTO affinity (group_id, sender, value, note, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(group_id, sender) DO UPDATE SET"
            "   value = excluded.value, note = excluded.note,"
            "   updated_at = excluded.updated_at",
            (group, sender, value, merged, int(time.time())),
        )
        self._conn.commit()
        self._cache[(group, sender)] = (value, merged, time.time())
        logging.info("心情值 [%s] %s：%d -> %d（%+d，%s）",
                     group, sender, current, value, step, note or "无说明")
        return value

    def reset(self, group: str, sender: str) -> None:
        self._conn.execute("DELETE FROM affinity WHERE group_id = ? AND sender = ?",
                           (group, sender))
        self._conn.commit()
        self._cache.pop((group, sender), None)

    def snapshot(self, group: str, limit: int = 20) -> list[tuple[str, int]]:
        rows = self._conn.execute(
            "SELECT sender, value FROM affinity WHERE group_id = ?"
            " ORDER BY value ASC LIMIT ?", (group, limit)).fetchall()
        return [(str(r[0]), int(r[1])) for r in rows]

    # ---------------------------------------------------------------- 注入
    def hint(self, group: str, sender: str) -> str:
        """给提示词的一行。刻意做得极短——它每轮都要发。"""
        if not self.cfg.enabled:
            return ""
        value, note = self.get(group, sender)
        if value >= self.cfg.neutral:
            return f"{sender}={value}"
        band = ("已经很不耐烦了，回得非常短、别热情、别主动找话",
                "有点不爽，语气冷一点，话少一点",
                "略微不悦，正常回答但别献殷勤")[
            0 if value <= self.cfg.angry else (1 if value <= self.cfg.neutral - 25 else 2)]
        tail = f"，{note}" if note else ""
        return f"{sender}={value}（{band}{tail}）"
