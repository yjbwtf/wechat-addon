"""把一段长回复拆成几条发出去。

为什么要拆
----------
微信群里没人会发一整屏文字。真人想到什么发什么，一条一句。
机器人一口气吐 200 字，比内容本身更像机器人。

规则
----
1. 短回复不拆（低于 min_chars）。
2. 优先按**空行**拆——模型被要求「要分几条发就用空行隔开」，
   所以空行是模型自己的分段意图，优先级最高。
3. 还是太长的一段，按句号/问号/感叹号再拆。
4. 太短的碎片并回上一段（避免刷屏式的一字一条）。
5. 最多 max_parts 段；超出的部分并进最后一段，宁可长一点也不要刷屏。
"""

from __future__ import annotations

import re

from .config import SplitConfig

# 句子边界。中英文标点都算，句尾标点跟着前一句走。
_SENTENCE = re.compile(r"[^。！？!?…~\n]*[。！？!?…~]+|[^。！？!?…~\n]+$")
# 分句边界。中文口语经常一个句号串到底、中间全是逗号
# （「今天晴26到14，明天多云21到11，后天……」），
# 只在整句超长时才退到这一层来切。
_CLAUSE = re.compile(r"[^，,；;、\n]*[，,；;、]+|[^，,；;、\n]+$")
_PARAGRAPH = re.compile(r"\n\s*\n+")


def _split_sentences(text: str) -> list[str]:
    out = [m.group(0).strip() for m in _SENTENCE.finditer(text)]
    return [s for s in out if s]


def _split_clauses(text: str) -> list[str]:
    out = [m.group(0).strip() for m in _CLAUSE.finditer(text)]
    return [s for s in out if s]


def _pack(pieces: list[str], limit: int) -> list[str]:
    """把句子按长度上限打包成段。"""
    packed: list[str] = []
    cur = ""
    for piece in pieces:
        if not cur:
            cur = piece
        elif len(cur) + len(piece) <= limit:
            cur += piece
        else:
            packed.append(cur)
            cur = piece
    if cur:
        packed.append(cur)
    return packed


def _balance(pieces: list[str], total: int, cfg: SplitConfig) -> list[str]:
    """按「想拆几段」来定每段目标长度，而不是简单地往下塞。

    直接贪心塞会得到 54 字 + 22 字 这种一段长一段短的结果，
    看着还是像机器人。先算该拆几段，再按平均长度打包，两边就匀了。
    """
    want = max(1, -(-total // max(1, cfg.max_chars)))       # 向上取整
    want = min(want, max(1, cfg.max_parts))
    if want <= 1:
        return [ "".join(pieces) ] if pieces else []

    # 留一点余量，免得因为标点算差一两个字就多拆一段
    limit = -(-total // want) + max(4, cfg.max_chars // 6)
    packed = _pack(pieces, limit)

    # 只在超过 max_parts 时才合并。不去硬凑 want——
    # 如果中间有一句特别长（比如一整串天气数据），凑成 2 段会变成
    # 一段 90 字一段 20 字，还不如老实拆 3 段。
    while len(packed) > max(1, cfg.max_parts) and len(packed) > 1:
        idx = min(range(len(packed) - 1), key=lambda i: len(packed[i]) + len(packed[i + 1]))
        packed[idx:idx + 2] = [packed[idx] + packed[idx + 1]]
    return packed


def split_reply(text: str, cfg: SplitConfig) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if not cfg.enabled:
        return [text]

    # 1) 空行分段 = 模型自己的意图，优先级最高，且不看 min_chars
    #    （它主动留了空行就是想说两条，硬并回去反而毁了节奏）
    #    只挡掉「空行两边有一边短得没意义」的情况。
    paragraphs = [p.strip() for p in _PARAGRAPH.split(text) if p.strip()]
    if len(paragraphs) > 1 and all(len(p) >= 3 for p in paragraphs):
        parts = paragraphs[: cfg.max_parts]
        if len(paragraphs) > cfg.max_parts:
            parts[-1] = "".join(paragraphs[cfg.max_parts - 1:])
        return parts

    if len(text) <= max(1, cfg.min_chars):
        return [text]

    # 2) 没有空行：按句子（不够就退到分句）拆，再按目标长度匀成几段
    pieces = _split_sentences(text)
    if len(pieces) <= 1:
        pieces = _split_clauses(text)
    if len(pieces) <= 1:
        return [text]                  # 没有可断的地方，别硬切
    parts = _balance(pieces, len(text), cfg)

    # 3) 碎片并回上一段，避免一字一条
    merged: list[str] = []
    for part in parts:
        if merged and len(part) < cfg.min_part:
            merged[-1] = merged[-1] + part
        else:
            merged.append(part)
    parts = merged

    # 4) 段数上限：多出来的并进最后一段
    if len(parts) > cfg.max_parts:
        parts = parts[: cfg.max_parts - 1] + ["".join(parts[cfg.max_parts - 1:])]

    # 收尾：段尾多余的逗号/顿号去掉，读起来才像人打的
    cleaned: list[str] = []
    for p in parts:
        p = p.strip()
        while p and p[-1] in "，,；;、":
            p = p[:-1].rstrip()
        if p:
            cleaned.append(p)

    if len(cleaned) <= 1:
        return [text]
    return cleaned
