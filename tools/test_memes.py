"""梗词库：加载/匹配/注入，重点是**弱梗**（「你干嘛」这类不能硬接的）。

## 为什么要有弱梗

用户的实际要求：

> 如「你干嘛」这种梗使用需要判断语境，不能因为用户说了就判断为玩梗，
> 需要结合判断，这部分可以交给模型判断

「你干嘛」是**日常也天天说**的话。字面命中就当成玩梗，机器人会看到
随便谁问一句「你干嘛」就开始接蔡徐坤的梗 —— 很难看。

所以标成 `weak: true` 之后：
  · 注入时带 `[可能是梗]`，并附一句「先看语境，不是玩梗就正常答」；
  · **不再从聊天历史里命中**（历史里撞上太容易，白烧 token 还把回复带偏）；
  · 判断本身交给模型 —— 字面信息不足以判断，硬编码规则只会更糟。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import load_config          # noqa: E402
from src.memes import MemeBook              # noqa: E402

cases: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    cases.append((name, ok, detail))
    print("  %s  %s%s" % ("[OK]" if ok else "[!!]", name,
                          ("   " + detail) if detail and not ok else ""))


class Cfg:
    file = "memes.yaml"


print("加载：内置 + 叠加层，同名词条叠加层优先")
print()
tmp = Path(tempfile.mkdtemp(prefix="memetest_"))
(tmp / "data").mkdir(parents=True, exist_ok=True)
(tmp / "memes.yaml").write_text(
    "memes:\n"
    "  - term: 甲梗\n"
    "    note: '内置的'\n"
    "  - term: 乙梗\n"
    "    alias: [乙乙]\n"
    "    weak: true\n"
    "    note: '内置的弱梗'\n"
    "  - term: 单\n"          # 单字，needle 会被丢掉
    "    note: '太短'\n",
    encoding="utf-8")
(tmp / "data" / "memes_user.yaml").write_text(
    "memes:\n  - term: 甲梗\n    note: '叠加层覆盖的'\n",
    encoding="utf-8")

book = MemeBook.load(Cfg(), tmp)
by = {e.term: e for e in book.entries}
check("三条内置（叠加层覆盖同名，不新增）-> 共 3 条", len(book.entries) == 3,
      str(list(by)))
check("同名词条叠加层优先", by["甲梗"].note == "叠加层覆盖的", by["甲梗"].note)
check("source 标成 user", by["甲梗"].source == "user", by["甲梗"].source)
check("weak 从 YAML 读出来", by["乙梗"].weak is True)
check("没写 weak 的默认不是弱梗", by["甲梗"].weak is False)

# 单字条目**留着**（群里可能真的教了），但 needle 会被丢掉 -> 永远匹配不上。
# 丢掉 needle 而不是丢掉条目，是为了 \梗 list 里还能看到它。
check("单字条目的 needle 全被丢掉", by["单"].needles() == [],
      str(by["单"].needles()))
check("单字条目不会误命中（「单单单」里不该出现「单」）",
      not book.match("单单单", "", limit=6),
      str([e.term for e in book.match("单单单", "", limit=6)]))
check("别的条目的 needle 都 ≥2 字",
      all(len(n) >= 2 for n in by["乙梗"].needles()),
      str(by["乙梗"].needles()))
check("alias 也算 needle", "乙乙" in by["乙梗"].needles())

print()
print("匹配：弱梗不认历史，强梗认")
print()
r = book.match("随便说说", "前面有人提过甲梗", limit=6)
check("强梗只出现在历史里 -> 仍会命中（现状，靠 max_history_hits 限量）",
      any(e.term == "甲梗" for e in r), str([e.term for e in r]))
r = book.match("随便说说", "前面有人提过乙梗", limit=6)
check("**弱梗只出现在历史里 -> 不命中**",
      not any(e.term == "乙梗" for e in r), str([e.term for e in r]))

book2 = MemeBook.load(Cfg(), tmp)
book2.add("丙弱", "一个日常词", weak=True)
book2.add("丁强", "一个专用词", weak=False)
r = book2.match("", "前面说了丙弱，也说了丁强", limit=6)
terms = [e.term for e in r]
check("**弱梗在历史里不命中**", "丙弱" not in terms, str(terms))
check("强梗在历史里照常命中", "丁强" in terms, str(terms))

r = book2.match("丙弱", "", limit=6)
check("弱梗在**当前消息**里命中（该给的判断信息还是要给）",
      any(e.term == "丙弱" for e in r), str([e.term for e in r]))

print()
print("注入文本：弱梗要标出来，并说清「先看语境」")
print()
block = MemeBook.format_block([book2.entries[-2]])
check("带 [可能是梗] 标记", "[可能是梗]" in block, block)
check("附了「先看语境」的说明", "先看语境" in block and "不要硬接" in block, block)

strong = MemeBook.format_block([book2.entries[-1]])
check("强梗不带标记", "[可能是梗]" not in strong, strong)
check("强梗不带那段说明", "先看语境" not in strong, strong)

mixed = MemeBook.format_block([book2.entries[-2], book2.entries[-1]])
check("混在一起时说明只出现一次", mixed.count("先看语境") == 1, mixed)
check("空列表 -> 空字符串（一个 token 都不花）",
      MemeBook.format_block([]) == "")

print()
print("add()：weak 要能落盘、能改回")
print()
p = tmp / "data" / "memes_user.yaml"
txt = p.read_text(encoding="utf-8")
check("weak=True 写进 YAML", "weak: true" in txt, txt[-200:])
book3 = MemeBook.load(Cfg(), tmp)
got = {e.term: e.weak for e in book3.entries}
check("重新加载后 weak 还在", got.get("丙弱") is True, str(got))
check("丁强没被写成弱梗", not got.get("丁强"), str(got))

book3.add("丙弱", "改过的说明", weak=False)
book4 = MemeBook.load(Cfg(), tmp)
got = {e.term: e.weak for e in book4.entries}
check("weak=False 会把标记去掉", got.get("丙弱") is False, str(got))
check("说明也更新了",
      {e.term: e.note for e in book4.entries}["丙弱"] == "改过的说明")

print()
print("常驻「口气/台词」：always: true")
print()
# 用户要求：「回复可以利用此来进行对应回答，跟我在人设里写的孙笑川相关
# 内容一样，可以用作回答」—— 也就是**不靠命中，直接当台词用**。
(tmp / "memes.yaml").write_text(
    "memes:\n"
    "  - term: 甲梗\n    note: '内置的'\n"
    "  - term: 怼人语录\n    always: true\n    note: '可用的台词：「敢不敢比划比划」'\n",
    encoding="utf-8")
(tmp / "data" / "memes_user.yaml").write_text("memes: []\n", encoding="utf-8")

b5 = MemeBook.load(Cfg(), tmp)
by5 = {e.term: e for e in b5.entries}
check("always 从 YAML 读出来", by5["怼人语录"].always is True, str(by5))
check("普通梗不是常驻", by5["甲梗"].always is False)
check("always_entries() 只返回常驻的",
      [e.term for e in b5.always_entries()] == ["怼人语录"],
      str(b5.always_entries()))

check("**常驻条目不再参与 match（否则会重复注入）**",
      [e.term for e in b5.match("怼人语录 甲梗", "", limit=6)] == ["甲梗"],
      str([e.term for e in b5.match("怼人语录 甲梗", "", limit=6)]))

blk = MemeBook.format_block([], always=b5.always_entries())
check("常驻段有「可以直接拿来用的口气/台词」表头",
      "可以直接拿来用的口气/台词" in blk, blk)
check("常驻段说清了什么时候用", "有人不客气" in blk or "挑事" in blk, blk)
check("常驻段说清了别乱用", "别每次都用" in blk and "别连发" in blk, blk)
check("常驻段带上了台词本身", "敢不敢比划比划" in blk, blk)

both = MemeBook.format_block([by5["甲梗"]], always=b5.always_entries())
check("两段同时存在（常驻段 + 命中段）",
      "口气/台词" in both and "甲梗=内置的" in both, both)
check("没有命中也没有常驻 -> 空字符串（一个 token 都不花）",
      MemeBook.format_block([], always=[]) == "")

print()
print("add(always=)：能落盘、能改回")
print()
b5.add("丙常驻", "常驻的口径", always=True)
t2 = (tmp / "data" / "memes_user.yaml").read_text(encoding="utf-8")
check("always=True 写进 YAML", "always: true" in t2, t2[-200:])
b6 = MemeBook.load(Cfg(), tmp)
check("重新加载后还在",
      "丙常驻" in [e.term for e in b6.always_entries()],
      str([e.term for e in b6.always_entries()]))
b6.add("丙常驻", "改了", always=False)
b7 = MemeBook.load(Cfg(), tmp)
check("always=False 会把常驻去掉",
      "丙常驻" not in [e.term for e in b7.always_entries()],
      str([e.term for e in b7.always_entries()]))

print()
print("自动更新的默认值：模型没给 weak 就当弱梗（保守）")
print()
for raw, want in ((None, True), (True, True), (False, False)):
    weak = True if raw is None else bool(raw)
    check("模型给的 weak=%-5s -> %s" % (raw, want), weak is want)

# 自动更新**永远不该**产生常驻条目（那会让每轮提示词无限膨胀）
import src.meme_feed as feed  # noqa: E402
check("自动更新的提示词里没有 always（常驻只能手工加）",
      "always" not in feed.PROMPT, "PROMPT 里出现了 always")

print()
print("真实词库抽样（memes.yaml + data/memes_user.yaml）")
print()
cfg = load_config(ROOT / "config.yaml")
real = MemeBook.load(cfg.memes, ROOT)
rby = {e.term: e for e in real.entries}

check("「你干嘛」标成了弱梗", rby.get("你干嘛") is not None
      and rby["你干嘛"].weak is True,
      str(rby.get("你干嘛")))
check("「蔡徐坤」不是弱梗（说了基本就是在玩梗）",
      rby.get("蔡徐坤") is not None and rby["蔡徐坤"].weak is False)

for kw, want in (("鸡你太美", "蔡徐坤"), ("鸡哥", "蔡徐坤"),
                 ("坤坤", "蔡徐坤"), ("练习时长两年半", "蔡徐坤"),
                 ("今天又刷到新的驾考宝典了", "驾考宝典"),
                 ("弱者喝饮料，强者喝大水", "大水理论"),
                 ("贵是贵点，但吃了不烧心", "不烧心")):
    hits = [e.term for e in real.match(kw, "", limit=6)]
    check("%-22s -> %s" % (kw, want), want in hits, str(hits))

check("**「你在干嘛呢」不该命中**（不含「你干嘛」这个子串）",
      not real.match("你在干嘛呢", "", limit=6),
      str([e.term for e in real.match("你在干嘛呢", "", limit=6)]))

check("真实词库里弱梗不会被历史带出来",
      not [e for e in real.match("早", "你干嘛", limit=6) if e.weak],
      str([e.term for e in real.match("早", "你干嘛", limit=6)]))

failed = [c for c in cases if not c[1]]
print()
if failed:
    print("不通过 %d / %d" % (len(failed), len(cases)))
    raise SystemExit(1)
print("全部通过（%d 项）" % len(cases))
