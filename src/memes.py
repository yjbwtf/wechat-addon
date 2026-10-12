"""梗词库：让机器人认得出网络热梗，并且**接得住**。

为什么要单独一个库
------------------
群里一个「ccb」，模型要么当成拼写错误、要么当成认真的问题去解释一遍，
两种反应都很出戏。光靠提示词里写一句「你要懂梗」是没用的——
模型不知道你们群的暗号是什么。

所以做法是：把「词 → 这个梗在表达什么情绪 → 该怎么接」写成一张表，
**命中才注入**，不是每次都全量塞进提示词（那样反而白烧 token）。

两个文件
--------
- `memes.yaml`        随程序分发的词库，可以用 \\梗 add 追加
- `data/memes_user.yaml`  群里现教的，优先于上面那份

匹配规则
--------
拿 term 和 alias 去当前消息和最近聊天记录里找子串。
排序：当前消息命中 > 只出现在历史里的；同档按词长从长到短（长的更具体）。
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class MemeEntry:
    term: str
    note: str
    aliases: list[str] = field(default_factory=list)
    source: str = "builtin"      # builtin | user
    #: 弱梗：**字面命中不代表在玩梗**。
    #:
    #: 「你干嘛」这种日常也天天说的词，一命中就当成玩梗肯定是错的。
    #: 标成弱梗之后：
    #:   · 注入时会被明确标出来，并告诉模型「先看语境，不是玩梗就正常答」
    #:   · **不再从聊天历史里命中**（历史里撞上太容易，等于每轮都白烧 token
    #:     还把回复带偏）
    #: 判断本身交给模型 —— 字面信息不足以判断，硬编码规则只会更糟。
    weak: bool = False

    def needles(self) -> list[str]:
        out = [self.term] + list(self.aliases)
        # 太短的词满世界都是，会误命中（「6」「典」单字太容易撞）
        return [n for n in out if n and len(n) >= 2]


@dataclass
class MemeHit:
    entry: MemeEntry
    where: str                   # "now" | "ctx"
    pos: int


def _ascii_ci(hay: str) -> str:
    """ASCII 部分转小写，用来做大小写不敏感的子串查找（ccb / CCB）。"""
    return hay.lower()


class MemeBook:
    def __init__(self, entries: list[MemeEntry], cfg, root: Path) -> None:
        self.cfg = cfg
        self.root = Path(root)
        self.user_path = Path(root) / "data" / "memes_user.yaml"
        self.builtin_path = Path(root) / (getattr(cfg, "file", "memes.yaml") or "memes.yaml")
        self.entries = entries
        self._mtimes: dict[str, float] = {}
        self._stamp()

    # ------------------------------------------------------------ 加载
    @classmethod
    def load(cls, cfg, root: Path) -> "MemeBook":
        book = cls([], cfg, root)
        book.reload()
        return book

    def _stamp(self) -> None:
        for p in (self.builtin_path, self.user_path):
            try:
                self._mtimes[str(p)] = p.stat().st_mtime
            except OSError:
                self._mtimes[str(p)] = 0.0

    def changed_on_disk(self) -> bool:
        for p in (self.builtin_path, self.user_path):
            try:
                m = p.stat().st_mtime
            except OSError:
                m = 0.0
            if abs(m - self._mtimes.get(str(p), 0.0)) > 1e-6:
                return True
        return False

    def _read(self, path: Path, source: str) -> list[MemeEntry]:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            return []
        except yaml.YAMLError as exc:
            # 一定要出声：静默返回空表会让人以为「梗库坏了但不知道为啥」
            print(f"[memes] {path.name} 解析失败，这个词库这次不生效：{exc}",
                  file=sys.stderr)
            return []
        out: list[MemeEntry] = []
        for item in raw.get("memes") or []:
            if isinstance(item, str):
                out.append(MemeEntry(term=item, note="", source=source))
                continue
            if not isinstance(item, dict):
                continue
            term = str(item.get("term") or "").strip()
            if not term:
                continue
            alias = item.get("alias") or []
            if isinstance(alias, str):
                alias = [alias]
            out.append(MemeEntry(
                term=term,
                note=str(item.get("note") or "").strip(),
                aliases=[str(a).strip() for a in alias if str(a).strip()],
                source=source,
                weak=bool(item.get("weak", False)),
            ))
        return out

    def reload(self) -> None:
        builtin = self._read(self.builtin_path, "builtin")
        user = self._read(self.user_path, "user")
        # 用户教的覆盖同名词条
        merged: dict[str, MemeEntry] = {e.term.lower(): e for e in builtin}
        for e in user:
            merged[e.term.lower()] = e
        self.entries = list(merged.values())
        self._stamp()

    # ------------------------------------------------------------ 匹配
    def match(self, now_text: str, history_text: str,
              limit: int = 6, max_chars: int = 420,
              max_history_hits: int = 2) -> list[MemeEntry]:
        """找出这段话里出现的梗，按相关度排好，并且受长度预算约束。

        当前消息里命中的永远排在前面、且不受 max_history_hits 限制；
        历史里命中的只取最近几个——因为只要扫历史，就很容易每轮都命中同一个词，
        那样等于每轮都白花这笔 token，还会把回复带偏到那个梗上。
        """
        if not self.entries:
            return []
        now_key = _ascii_ci(now_text or "")
        ctx_key = _ascii_ci(history_text or "")

        hits: list[MemeHit] = []
        for entry in self.entries:
            best: MemeHit | None = None
            for needle in entry.needles():
                key = _ascii_ci(needle)
                pos = now_key.find(key)
                if pos >= 0:
                    cand = MemeHit(entry, "now", pos)
                else:
                    pos = ctx_key.rfind(key)
                    if pos < 0:
                        continue
                    cand = MemeHit(entry, "ctx", pos)
                if best is None or (cand.where == "now" and best.where != "now"):
                    best = cand
                if best.where == "now":
                    break
            if best is not None:
                hits.append(best)

        # 当前消息命中优先；同档里位置靠后（更近）的优先
        hits.sort(key=lambda h: (0 if h.where == "now" else 1, -h.pos))

        out: list[MemeEntry] = []
        used = 0
        ctx_used = 0
        for hit in hits:
            if len(out) >= max(1, limit):
                break
            # 弱梗只认**当前这条消息**里的命中。历史里撞上太容易 ——
            # 「你干嘛」在前几条消息里出现过，不代表这条在玩梗，
            # 白烧 token 还会把回复带偏。
            if hit.entry.weak and hit.where == "ctx":
                continue
            if hit.where == "ctx" and ctx_used >= max(0, max_history_hits):
                continue
            cost = len(hit.entry.term) + len(hit.entry.note) + 4
            if out and used + cost > max_chars:
                break
            out.append(hit.entry)
            used += cost
            if hit.where == "ctx":
                ctx_used += 1
        return out

    @staticmethod
    def format_block(entries: list[MemeEntry]) -> str:
        """把命中的梗变成给模型看的一小段。

        弱梗单独标出来，并且附一句「先看语境」——判断交给模型，
        因为「你干嘛」到底是在玩梗还是真的在问，只有看上下文才知道。
        """
        if not entries:
            return ""
        lines = []
        has_weak = False
        for e in entries:
            note = e.note or "群里的梗，顺着接就行"
            if e.weak:
                has_weak = True
                lines.append(f"[可能是梗]{e.term}={note}")
            else:
                lines.append(f"{e.term}={note}")
        out = "\n".join(lines)
        if has_weak:
            out += ("\n（标了[可能是梗]的那几条，字面**日常也这么说** —— "
                    "别一看到就当玩梗：先看语境（有没有人接同一个梗、"
                    "语气是不是在闹、整条消息是不是就这几个字）。"
                    "不是在玩梗就正常回答，**不要硬接，也不要科普这个梗**）")
        return out

    # ------------------------------------------------------------ 增删
    def add(self, term: str, note: str, aliases: list[str] | None = None,
            weak: bool = False) -> str:
        """教一个新梗。返回给群里看的回执。

        `aliases` 是给自动更新用的 —— 联网捞回来的条目常带一堆变体写法
        （「我要验牌」还有「给我擦皮鞋」「牌没有问题」），不带上就浪费了。
        群里手打的 `\\\\梗 add` 不用传，保持简单。

        `weak` 是给自动更新用的：模型判断这个梗「字面日常也这么说」时标上，
        注入时会附带「先看语境」的说明（判断交给模型，见 MemeEntry.weak）。
        """
        term = (term or "").strip()
        note = (note or "").strip()
        if not term:
            return "用法：\\\\梗 add 词 | 说明"
        clean_alias = []
        for a in (aliases or []):
            a = str(a).strip()
            if a and a.lower() != term.lower() and a not in clean_alias:
                clean_alias.append(a)

        data = {"memes": []}
        if self.user_path.exists():
            try:
                data = yaml.safe_load(self.user_path.read_text(encoding="utf-8")) or data
            except yaml.YAMLError:
                data = {"memes": []}
        items = data.get("memes")
        if not isinstance(items, list):
            items = []
            data["memes"] = items

        for item in items:
            if isinstance(item, dict) and str(item.get("term", "")).lower() == term.lower():
                item["note"] = note
                if clean_alias:
                    item["alias"] = clean_alias
                if weak:
                    item["weak"] = True
                else:
                    item.pop("weak", None)
                break
        else:
            entry: dict = {"term": term, "note": note}
            if clean_alias:
                entry["alias"] = clean_alias
            if weak:
                entry["weak"] = True
            items.append(entry)

        self.user_path.parent.mkdir(parents=True, exist_ok=True)
        with self.user_path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False,
                           default_flow_style=False)
        self.reload()
        return f"记住了：「{term}」"

    def remove(self, term: str) -> str:
        term = (term or "").strip().lower()
        if not self.user_path.exists():
            return "自己教的梗里没有这个词"
        try:
            data = yaml.safe_load(self.user_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            return "读不懂 memes_user.yaml"
        items = data.get("memes") or []
        keep = [i for i in items
                if not (isinstance(i, dict) and str(i.get("term", "")).lower() == term)]
        if len(keep) == len(items):
            return "自己教的梗里没有这个词"
        data["memes"] = keep
        with self.user_path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False,
                           default_flow_style=False)
        self.reload()
        return f"忘了：「{term}」"

    def listing(self, limit: int = 40) -> str:
        user = [e for e in self.entries if e.source == "user"]
        if not user:
            return "你还没教过我梗"
        names = [e.term for e in user[:limit]]
        more = "" if len(user) <= limit else f" 等 {len(user)} 个"
        return "我会的：" + "、".join(names) + more


# --------------------------------------------------------------------------
# 从聊天记录里挑「疑似新梗」
# --------------------------------------------------------------------------
# 用来提示用户「这几个词反复出现但你还没教我」，不参与回复生成。
_CAND = re.compile(r"[\u4e00-\u9fa5A-Za-z]{2,6}")


def candidates(texts: list[str], book: MemeBook, min_count: int = 3) -> list[str]:
    """在最近记录里找出高频、但词库里没有的短词。"""
    from collections import Counter

    known = {n.lower() for e in book.entries for n in e.needles()}
    counter: Counter[str] = Counter()
    for text in texts:
        for token in _CAND.findall(text or ""):
            if len(token) >= 2 and token.lower() not in known:
                counter[token] += 1
    return [w for w, c in counter.most_common(20) if c >= min_count]


def now_ts() -> float:
    return time.time()
