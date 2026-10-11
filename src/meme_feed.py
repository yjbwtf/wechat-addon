"""梗词库的定时联网更新。

## 为什么要自动更新

梗的生命周期很短 —— 半年前的热梗现在说出来只会显得过时。
手工维护一份词库必然烂掉，所以让机器人自己定期去网上捞。

## 写到哪里

**不碰 `memes.yaml`**。那份是给人看、给人改的内置库，注释和排版都有意义，
程序回写会把它们毁掉。更新写进 `data/memes_user.yaml` —— 就是「群里现教的梗」
用的那个叠加层，`MemeBook.reload()` 时它优先于内置库。这样：

  - 内置库保持可读、可版本控制；
  - 自动捞到的梗和群里教的混在一起，用同一个 `\\\\梗 del` 就能删；
  - 更新失败最多是没捞到，不会破坏任何东西。

## 防重复

模型不知道我们已经有什么，所以：
  1. 把现有 term 列表塞进提示词，明确要求不要重复；
  2. 拿回来之后再按 term/alias 过滤一遍（不信模型的自律）；
  3. 一次最多收 max_new 条，防止一次灌进来几十条把提示词撑爆。

## 调用成本

默认 14 天一次。用 `wechat` profile（有联网搜索），一次调用量级和查一次天气
差不多。频率、条数、开关都在 config.yaml 的 memes.autoupdate 下面。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from .config import AutoUpdateConfig
from .memes import MemeBook

log = logging.getLogger("wechat-bot.memes")

PROMPT = """用联网搜索查一下**最近流行**的中文网络热梗（最近三个月内为主，可以带少量仍在高频使用的）。

已经收录的（不要重复，包括它们的别名）：
{existing}

挑出最多 {max_new} 个最值得知道的，只输出一个 JSON 数组，不要任何别的文字、不要代码块围栏：

[
  {{"term": "梗的标准写法", "alias": ["别名", "变体"], "note": "来源/用法。表达什么情绪。接梗姿势：顺着演还是别接。"}}
]

要求：
- note 控制在 60 字以内，要具体到「另一个模型看了就知道怎么接」；
- note 里不要出现英文双引号，也不要有换行；
- term 用最常被写出来的那个形式；
- 拿不准来源的宁可不写，不要编。"""


def state_path(root: Path) -> Path:
    return Path(root) / "data" / "memes_update.json"


def load_state(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(p: Path, data: dict) -> None:
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    except OSError as exc:
        log.warning("写更新状态失败：%s", exc)


def days_since(state: dict) -> float:
    last = float(state.get("last_update") or 0)
    if last <= 0:
        return 1e9
    return (time.time() - last) / 86400.0


def due(cfg: AutoUpdateConfig, root: Path, *, force: bool = False) -> bool:
    if force:
        return True
    if not cfg.enabled:
        return False
    return days_since(load_state(state_path(root))) >= max(1.0, cfg.interval_days)


def parse_entries(text: str) -> list[dict]:
    """从模型回复里抠出 JSON 数组。

    模型很喜欢加 ```json 围栏和一两句解释，所以不能直接 json.loads。
    策略：先试整体，失败就找第一段方括号配对的内容。
    """
    text = (text or "").strip()
    if not text:
        return []
    for candidate in (text, _first_array(text)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            data = data.get("memes") or data.get("items") or []
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
    return []


def _first_array(text: str) -> str:
    start = text.find("[")
    if start < 0:
        return ""
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def clean_note(note: str) -> str:
    """note 会进提示词，得先弄干净：去换行、去英文双引号、截断。"""
    s = re.sub(r"\s+", " ", str(note or "")).strip()
    s = s.replace('"', "「").replace("'", "")
    return s[:120]


def update(book: MemeBook, brain, cfg: AutoUpdateConfig, root: Path,
           *, force: bool = False) -> str:
    """跑一次更新。返回给人看的摘要（会写进日志）。"""
    if not cfg.enabled and not force:
        return "自动更新已关闭"

    known = set()
    for e in book.entries:
        known.add(e.term.strip().lower())
        for a in e.aliases:
            known.add(str(a).strip().lower())

    existing = "、".join(sorted({e.term for e in book.entries}))
    prompt = PROMPT.format(existing=existing[:1500], max_new=cfg.max_new)

    try:
        reply = brain.ask(prompt, user="系统", group="")
    except Exception as exc:  # noqa: BLE001
        log.warning("梗库自动更新失败（模型调用出错）：%s", exc)
        return f"更新失败：{exc}"

    raw = parse_entries(reply)
    if not raw:
        log.warning("梗库自动更新：模型没返回可解析的 JSON，原文前 200 字：%s",
                    (reply or "")[:200])
        return "更新失败：回复里没有 JSON"

    added, skipped = [], 0
    for item in raw:
        term = str(item.get("term") or "").strip()
        note = clean_note(item.get("note"))
        if not term or not note:
            skipped += 1
            continue
        aliases = [str(a).strip() for a in (item.get("alias") or []) if str(a).strip()]
        keys = {term.lower()} | {a.lower() for a in aliases}
        if keys & known:
            skipped += 1
            continue
        # 走 MemeBook.add，落进 data/memes_user.yaml（内置库一个字都不动）
        result = book.add(term, note, aliases=aliases)
        if result.startswith("记住"):
            known |= keys
            added.append(term)
        else:
            skipped += 1

    if added:
        book.reload()

    st = load_state(state_path(root))
    st["last_update"] = time.time()
    st["last_added"] = added
    st["last_skipped"] = skipped
    st["last_error"] = ""
    save_state(state_path(root), st)

    msg = f"新增 {len(added)} 条，跳过 {skipped} 条"
    if added:
        msg += "：" + "、".join(added[:12])
    log.warning("梗库自动更新：%s", msg)
    return msg
