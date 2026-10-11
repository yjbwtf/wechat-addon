"""历史记录层：SQLite 存储 + 读取上限 + 滚动保留 + 到期自动清理。

保留语义
--------
- retention=daily   滚动保留 1 天
- retention=weekly  滚动保留 N 天（默认 7）
- retention=forever 永久（仍受 per_group_max 条数上限约束）

清理任务做三件事：
1. 删除超过保留期的消息；
2. 把每群条数裁剪到 per_group_max；
3. 若 reset_on_expiry 且某群窗口已过期且无剩余消息，则重置该群的
   记录窗口，实现「期限结束后重新开始记录」。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .config import HistoryConfig

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   TEXT    NOT NULL,
    sender     TEXT    NOT NULL DEFAULT '',
    content    TEXT    NOT NULL,
    msg_key    TEXT    NOT NULL,
    is_bot     INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_dedup
    ON messages(group_id, msg_key);
CREATE INDEX IF NOT EXISTS idx_group_time
    ON messages(group_id, created_at);

CREATE TABLE IF NOT EXISTS group_state (
    group_id     TEXT PRIMARY KEY,
    window_start INTEGER NOT NULL,
    last_purge   INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass
class StoredMessage:
    sender: str
    content: str
    is_bot: bool
    created_at: int


class HistoryStore:
    def __init__(self, db_path: Path | str, cfg: HistoryConfig) -> None:
        self.cfg = cfg
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---------------------------------------------------------------- 写入
    def add(
        self,
        group_id: str,
        sender: str,
        content: str,
        msg_key: str,
        *,
        is_bot: bool = False,
        created_at: int | None = None,
    ) -> bool:
        """写入一条消息。msg_key 重复则返回 False（去重）。"""
        if not self.cfg.enabled or not content.strip():
            return False
        now = int(created_at or time.time())
        try:
            self._conn.execute(
                "INSERT INTO messages"
                " (group_id, sender, content, msg_key, is_bot, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (group_id, sender, content.strip(), msg_key, 1 if is_bot else 0, now),
            )
        except sqlite3.IntegrityError:
            return False
        self._conn.execute(
            "INSERT INTO group_state (group_id, window_start) VALUES (?, ?)"
            " ON CONFLICT(group_id) DO NOTHING",
            (group_id, now),
        )
        self._conn.commit()
        return True

    # ---------------------------------------------------------------- 读取
    def recent(self, group_id: str) -> list[StoredMessage]:
        """按 read_limit 返回最近若干条消息（时间正序）。"""
        if not self.cfg.enabled:
            return []
        limit = self.cfg.read_limit
        now = int(time.time())
        floor = now - max(0, limit.max_age_days) * 86400

        rows = self._conn.execute(
            "SELECT sender, content, is_bot, created_at FROM messages"
            " WHERE group_id = ? AND created_at >= ?"
            " ORDER BY id DESC LIMIT ?",
            (group_id, floor, max(1, limit.max_messages)),
        ).fetchall()

        picked: list[StoredMessage] = []
        total = 0
        for row in rows:  # 从新到旧累加，超出字符预算就停
            msg = StoredMessage(
                sender=row["sender"],
                content=row["content"],
                is_bot=bool(row["is_bot"]),
                created_at=int(row["created_at"]),
            )
            total += len(msg.content) + len(msg.sender) + 4
            if picked and total > limit.max_chars:
                break
            picked.append(msg)
        picked.reverse()
        return picked

    def count(self, group_id: str | None = None) -> int:
        if group_id is None:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE group_id = ?", (group_id,)
            ).fetchone()
        return int(row["n"])

    # ---------------------------------------------------------------- 清理
    def purge(self, *, force: bool = False) -> int:
        """执行清理任务，返回删除的消息条数。"""
        if not self.cfg.enabled:
            return 0
        now = int(time.time())
        deleted = 0

        days = self.cfg.effective_retention_days
        if days is not None:
            cutoff = now - max(1, days) * 86400
            cur = self._conn.execute(
                "DELETE FROM messages WHERE created_at < ?", (cutoff,)
            )
            deleted += cur.rowcount or 0

        # 每群条数裁剪
        if self.cfg.per_group_max > 0:
            groups = [
                r["group_id"]
                for r in self._conn.execute(
                    "SELECT DISTINCT group_id FROM messages"
                ).fetchall()
            ]
            for gid in groups:
                cur = self._conn.execute(
                    "DELETE FROM messages WHERE group_id = ? AND id NOT IN ("
                    "  SELECT id FROM messages WHERE group_id = ?"
                    "  ORDER BY id DESC LIMIT ?"
                    ")",
                    (gid, gid, self.cfg.per_group_max),
                )
                deleted += cur.rowcount or 0

        # 窗口过期且已无记录 -> 重置窗口，实现「到期后重新开始记录」
        if self.cfg.reset_on_expiry and days is not None:
            window = max(1, days) * 86400
            rows = self._conn.execute(
                "SELECT s.group_id AS gid, s.window_start AS ws,"
                " (SELECT COUNT(*) FROM messages m WHERE m.group_id = s.group_id) AS n"
                " FROM group_state s"
            ).fetchall()
            for row in rows:
                if int(row["n"]) == 0 and now - int(row["ws"]) >= window:
                    self._conn.execute(
                        "UPDATE group_state SET window_start = ? WHERE group_id = ?",
                        (now, row["gid"]),
                    )

        self._conn.commit()
        return deleted

    def vacuum(self) -> None:
        self._conn.execute("VACUUM")
        self._conn.commit()
