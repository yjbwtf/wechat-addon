"""UI 文案的两个一致性检查（都不需要浏览器）。

## 为什么要有

用户报的：「设置里部分文案不匹配，如默认单个反斜杠，但是括号里还是双反斜杠」。

真实的群里前缀是**一个**反斜杠（config.yaml 里 `prefix: '\\'`，读出来长度 1），
但 `ui/schema.json` 里两处文案写成了两个：

  · `config:commands.prefix` 的 hint：「默认是**单个反斜杠**（\\\\截图…）」
  · 人设页的 note：「在群里教新梗：\\\\梗 add 词 | 说明」

根因：这些文案最早照 Markdown 写，Markdown 里 `\\\\` 才渲染成一个反斜杠。
但 schema.json 里的字符串**直接进 DOM**，不转义 —— 界面上就真的是两个，
用户照着打就打错了。这类「文案和数据不一致」没有测试就会一直漂。

## 检查什么

1. **文案里不出现双反斜杠**（`ui/schema.json` 的文本字段）；
2. **前缀示例和真实前缀一致**（单反斜杠）；
3. **字段索引自洽**：0..n-1 连续、每个恰好被引用一次、引用都存在。
   （索引是 UI 提交时回填用的，错位会导致「改了 A 却写进 B」）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import load_config          # noqa: E402

SCHEMA = ROOT / "ui" / "schema.json"
TEXT_KEYS = ("hint", "note", "title", "text", "label", "desc", "placeholder")

cases: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    cases.append((name, ok, detail))
    print("  %s  %s%s" % ("[OK]" if ok else "[!!]", name,
                          ("   " + detail) if detail and not ok else ""))


d = json.loads(SCHEMA.read_text(encoding="utf-8"))
cfg = load_config(ROOT / "config.yaml")


def texts(items):
    """遍历页面/分组里的所有文本字段，产出 (位置, 键, 值)。"""
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        for k in TEXT_KEYS:
            v = it.get(k)
            if isinstance(v, str):
                yield "%s[%d].%s" % (it.get("t"), i, k), k, v
        if it.get("t") == "group":
            yield from texts(it.get("items") or [])


print("1) 文案里不该出现双反斜杠")
print()
bad = []
for p in d["pages"]:
    for where, k, v in texts(p.get("items") or []):
        if "\\\\" in v:
            bad.append(("%s/%s" % (p.get("name"), where), v))
for f in d.get("fields") or []:
    for where, k, v in texts([f]):
        if "\\\\" in v:
            bad.append(("fields/%s" % f.get("key"), v))

check("没有任何文案含双反斜杠", not bad, str(bad))
check("**用一条假的双反斜杠自检**（确保上面的检查真的会抓到）",
      "\\\\" in "假文案 \\\\截图", "检查逻辑本身失效了")

print()
print("2) 前缀示例要跟真实前缀一致")
print()
real = cfg.commands.prefix
check("配置里的前缀确实是一个字符：%r" % real, len(real) == 1, repr(real))

hint = ""
for f in d["fields"]:
    if f.get("key") == "config:commands.prefix":
        hint = f.get("hint") or ""
check("hint 里写着「单个反斜杠」", "单个反斜杠" in hint, hint[:60])
check("hint 的示例是真前缀（%s截图）" % real,
      ("%s截图" % real) in hint and ("%sagent 任务" % real) in hint,
      hint[:160])
check("hint 里没有双反斜杠示例", ("%s%s截图" % (real, real)) not in hint,
      hint[:160])

note = ""
for p in d["pages"]:
    for where, k, v in texts(p.get("items") or []):
        if "梗 add" in v:
            note = v
check("人设页的「教新梗」用的是真前缀（%s梗 add）" % real,
      ("%s梗 add" % real) in note, note[:120])

short = ""
for f in d["fields"]:
    if f.get("key") == "config:commands.short_prefix":
        short = f.get("hint") or ""
check("私聊简写前缀的示例也对（%sagent）" % cfg.commands.short_prefix,
      ("%sagent" % cfg.commands.short_prefix) in short, short[:120])

print()
print("3) 字段索引自洽")
print()
fields = d.get("fields") or []
check("index 从 0 连续到 n-1",
      all(f.get("index") == i for i, f in enumerate(fields)),
      str([f.get("index") for f in fields][:10]))

refs: list[int] = []


def collect(items):
    for it in items:
        if it.get("t") == "field":
            refs.append(it["index"])
        elif it.get("t") == "group":
            collect(it.get("items") or [])


for p in d["pages"]:
    collect(p.get("items") or [])

check("页面里引用的索引都存在",
      all(0 <= i < len(fields) for i in refs),
      str([i for i in refs if not (0 <= i < len(fields))]))
check("每个字段恰好被引用一次（不多不少）",
      len(refs) == len(set(refs)) == len(fields),
      "引用 %d 个 / 去重 %d 个 / 字段 %d 个" % (len(refs), len(set(refs)),
                                                len(fields)))
check("没有孤儿字段（定义了但页面上没有）",
      set(refs) == set(range(len(fields))),
      str(sorted(set(range(len(fields))) - set(refs))[:10]))

keys = [f.get("key") for f in fields]
check("字段 key 不重复", len(keys) == len(set(keys)),
      str([k for k in keys if keys.count(k) > 1]))

print()
print("4) schema.json 的格式约定")
print()
raw = SCHEMA.read_bytes()
check("结尾没有换行（改文件时别加）", not raw.endswith(b"\n"), repr(raw[-2:]))
check("缩进是 1（json.dumps(indent=1)）",
      b'\n  "fields"' not in raw and b'\n "fields"' in raw)
check("中文没有被转义（ensure_ascii=False）", "\\u" not in raw.decode("utf-8")[:2000])

failed = [c for c in cases if not c[1]]
print()
if failed:
    print("不通过 %d / %d" % (len(failed), len(cases)))
    raise SystemExit(1)
print("全部通过（%d 项）" % len(cases))
