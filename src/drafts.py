"""草稿确认模式：生成的回复先落盘，人工确认后才发出去。

用 append-only 的 JSONL 存，每次状态变更追加一条新记录，
读取时以每个 id 的最后一条为准。这样进程被杀也不会丢数据。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Draft:
    id: str
    group: str
    sender: str
    incoming: str
    reply: str
    created_at: int
    status: str = "pending"      # pending | sent | rejected
    updated_at: int = 0


class DraftStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, draft: Draft) -> None:
        draft.updated_at = int(time.time())
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(draft), ensure_ascii=False) + "\n")

    def _load(self) -> dict[str, Draft]:
        latest: dict[str, Draft] = {}
        if not self.path.exists():
            return latest
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                latest[json.loads(line)["id"]] = Draft(**json.loads(line))
            except Exception:  # noqa: BLE001
                continue
        return latest

    def add(self, group: str, sender: str, incoming: str, reply: str) -> Draft:
        draft = Draft(
            id=uuid.uuid4().hex[:8],
            group=group,
            sender=sender,
            incoming=incoming,
            reply=reply,
            created_at=int(time.time()),
        )
        self._append(draft)
        return draft

    def list(self, status: str | None = "pending") -> list[Draft]:
        drafts = list(self._load().values())
        if status:
            drafts = [d for d in drafts if d.status == status]
        drafts.sort(key=lambda d: d.created_at)
        return drafts

    def get(self, draft_id: str) -> Draft | None:
        return self._load().get(draft_id)

    def update(self, draft_id: str, status: str) -> Draft | None:
        draft = self._load().get(draft_id)
        if not draft:
            return None
        draft.status = status
        self._append(draft)
        return draft

    def purge_old(self, days: int = 30) -> int:
        """清掉很久以前的草稿记录。"""
        latest = self._load()
        cutoff = time.time() - days * 86400
        keep = [d for d in latest.values() if d.created_at >= cutoff]
        if len(keep) == len(latest):
            return 0
        removed = len(latest) - len(keep)
        self.path.write_text(
            "".join(json.dumps(asdict(d), ensure_ascii=False) + "\n" for d in keep),
            encoding="utf-8",
        )
        return removed
