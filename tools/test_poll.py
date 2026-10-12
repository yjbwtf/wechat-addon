"""轮询的「读哪些群」逻辑 —— 这是**漏读**的根因，必须钉住。

## 背景（用户实际踩到的）

用户在群里发 `/查绝区零 14327029`，机器人没回，**日志里那条消息一个字都没有**。
查下来是这样一个机制：

  微信的未读小红点**只在你不看那个聊天的时候才出现**。用户正在群里打字时，
  微信认为这条已读 —— 没有小红点，于是 `fetch()` 把该群判成「跳过」，
  只能等下一次全量扫描（默认 6 轮 ≈ 18 秒）。用户等不到就以为机器人坏了。

而且机器人刚回过话的群**恰恰就是当前开着的群**，所以这个场景很常见。

修法：**当前正开着的聊天也读** —— 它本来就在前台，不用切窗口，
不增加「抢焦点」的代价。

## 这里测什么

`fetch()` 每轮决定读哪些群，取决于三件事：
  1. 会话列表里有没有未读小红点
  2. 是不是轮到全量扫描那一轮
  3. **当前开着的是哪个聊天**（新加的）

不需要真微信 —— 把 WxAutoAdapter 的关键方法打桩就行。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.wechat_io import WxAutoAdapter  # noqa: E402

cases: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    cases.append((name, ok, detail))
    print("  %s  %s%s" % ("[OK]" if ok else "[!!]", name,
                          ("   " + detail) if detail and not ok else ""))


class FakePoll:
    def __init__(self, full_scan_every: int) -> None:
        self.full_scan_every = full_scan_every


class FakeCfg:
    def __init__(self, every: int) -> None:
        self.poll = FakePoll(every)


def make(every: int, unread, current: str, groups=("群A", "群B", "群C")):
    """造一个不用真微信的 adapter。

    `unread`    = _unread_groups() 的返回（set 或 None）
    `current`   = 当前开着的聊天名（"" 表示不是白名单群）
    """
    a = object.__new__(WxAutoAdapter)
    a.groups = list(groups)
    a.cfg = FakeCfg(every)
    a._cycle = 0
    a.read_log = []
    a._unread_groups = lambda: unread
    a._chat_name = lambda: current
    a._read_group = lambda g: (a.read_log.append(g), [])[1]
    return a


print("没未读时：当前开着的群要被读，其它跳过")
print()
a = make(6, set(), "群B")
a.fetch()
check("开着的群B被读了", "群B" in a.read_log, str(a.read_log))
check("没开着的群A/群C被跳过",
      "群A" not in a.read_log and "群C" not in a.read_log, str(a.read_log))

a = make(6, set(), "")
a.fetch()
check("**没开着白名单群时全跳过**（不自作主张去切窗口）",
      a.read_log == [], str(a.read_log))

print()
print("有未读时：不管开着哪个都要读")
print()
a = make(6, {"群A"}, "群B")
a.fetch()
check("未读的群A被读", "群A" in a.read_log, str(a.read_log))
check("开着的群B也被读（两个都要）", "群B" in a.read_log, str(a.read_log))
check("只有群C被跳过", "群C" not in a.read_log, str(a.read_log))

print()
print("全量扫描那一轮：全都读")
print()
a = make(6, set(), "")
a._cycle = 5          # 下一轮 _cycle=6，6 % 6 == 0 -> 全量
a.fetch()
check("全量轮读所有群", sorted(a.read_log) == ["群A", "群B", "群C"],
      str(a.read_log))

print()
print("无法判断未读（返回 None）时：全都读")
print()
a = make(6, None, "")
a.fetch()
check("读所有群", sorted(a.read_log) == ["群A", "群B", "群C"], str(a.read_log))

print()
print("边界")
print()
a = make(6, set(), "别的群")
a.fetch()
check("当前聊的不是白名单群 -> 全跳过", a.read_log == [], str(a.read_log))

a = make(1, set(), "")
a._cycle = 0
a.fetch()
check("full_scan_every=1 时每轮都全读（用户想「不漏」可以这么配）",
      sorted(a.read_log) == ["群A", "群B", "群C"], str(a.read_log))

a = make(6, set(), "群A")
a._cycle = 5          # 同时也轮到全量
a.fetch()
check("开着的群 + 全量轮，不会重复读", a.read_log.count("群A") == 1,
      str(a.read_log))

# _chat_name 抛异常时不能把整个 fetch 带崩
a = make(6, set(), "")


def boom():
    raise RuntimeError("ChatInfo 炸了")


a._chat_name = boom
try:
    a.fetch()
    check("读当前聊天失败时不崩，退回「全跳过」", a.read_log == [],
          str(a.read_log))
except Exception as exc:  # noqa: BLE001
    check("读当前聊天失败时不崩，退回「全跳过」", False, repr(exc))

failed = [c for c in cases if not c[1]]
print()
if failed:
    print("不通过 %d / %d" % (len(failed), len(cases)))
    raise SystemExit(1)
print("全部通过（%d 项）" % len(cases))
