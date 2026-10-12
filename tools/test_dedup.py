"""回归测试：新消息判定（窗口序列重叠，下标空间必须一致）。

## 这个测试守的四个 bug

  1. 指纹去重把「重发的同一句」判成旧的       -> 消息被吞
  2. 以为 msg_key 是稳定唯一标识（它不是）    -> 同一条消息重复处理
  3. 签名用过滤后的 parsed，丢掉了时间戳/图片 -> 窗口撞满后无法区分
  4. **k 算在 sig 空间、取值却用 parsed[k:]** -> 永远取到空，必然漏消息

第 4 个最隐蔽：它只在「窗口里有非文本消息」时才发作，因为那时
len(sig) > len(parsed)，下标才对不上。所以本测试**必须构造含
system 时间戳和机器人自己图片的窗口** —— 只用纯文本假数据是测不出来的，
前一轮我正是这么被自己骗过去的。

widgets: type='text' 文本 / type='system' 时间戳 / type='image' 机器人图片
"""
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
# 这里原来是 `sys.path.insert(0, r"ROOT")` —— 一个**没被替换掉的占位符**。
# 后果是 `python tools\test_dedup.py` 必然报
# ModuleNotFoundError: No module named 'src'，这个测试一直是**跑不起来**的。
# 注意 sys.path[0] 是「脚本所在目录」（tools\），不是当前工作目录，
# 所以必须自己把仓库根加进去。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402
from src.wechat_io import WxAutoAdapter  # noqa: E402

cfg = load_config()


@dataclass
class FakeRaw:
    """冒充 wxauto4 的消息对象。id/hash 故意留空 —— 真实情况就是拿不到稳定的。"""
    type: str = "text"
    sender: str = "10001"
    content: str = "x"
    id: str = ""
    hash: str = ""
    raw: dict = field(default_factory=lambda: {"chat_type": "group",
                                               "src": "friend", "type": "text"})


def T(c):                       # 用户发的文本
    return FakeRaw(type="text", sender="10001", content=c)


def S(c):                       # 系统时间戳分隔
    return FakeRaw(type="system", sender="system", content=c,
                   raw={"chat_type": "group", "src": "system", "type": "system"})


def IMG():                      # 机器人自己发的截图
    return FakeRaw(type="image", sender="self", content="图片",
                   raw={"chat_type": "group", "src": "self", "type": "image"})


class FakeAdapter(WxAutoAdapter):
    def __init__(self, cfg):
        self.cfg = cfg
        self.groups = ["test"]
        self._seen = set()
        self._seen_fp = set()
        self._baselined = set()
        self._prev_window = {}
        self._cycle = 0
        self._identity_cache = {}
        self._self_names_cache = None
        self._id_locks = {}
        self.window = []

    def _chat_with(self, group):
        return True

    def list_sessions(self):
        return [("test", "", "group")]

    class _Wx:
        def GetAllMessage(self_inner):
            return FakeAdapter._window_ref[0]
    _window_ref = [None]

    @property
    def wx(self):
        return FakeAdapter._Wx()


def run(a, *msgs):
    FakeAdapter._window_ref[0] = list(msgs)
    return [m.content for m in a._read_group("test")]


CASES = []

# ---------------- 含非文本消息的真实窗口（第 4 个 bug 的复现条件）----------
a = FakeAdapter(cfg)
base = [T("旧1"), S("20:54"), T("旧2"), IMG(), T("截图"), T("截图")]
got = run(a, *base)
CASES.append(("真实窗口首轮建基线（应不出声）", got, []))

# 窗口追加一条新消息（同时最老的被挤掉，模拟 13 条上限）
w2 = base[1:] + [T("截图")]
got = run(a, *w2)
CASES.append(("窗口滚动 + 新消息（sig 比 parsed 长，关键用例）", got, ["截图"]))

w3 = w2[1:] + [IMG(), T("截图")]
got = run(a, *w3)
CASES.append(("再来一条（中间夹机器人图片）", got, ["截图"]))

w4 = w3
got = run(a, *w4)
CASES.append(("窗口没变（不能重复处理）", got, []))

# ---------------- 纯文本窗口（第 1 个 bug 的复现条件）-------------------
b = FakeAdapter(cfg)
run(b, T("旧1"), T("旧2"), T("旧3"))
got = run(b, T("旧1"), T("旧2"), T("旧3"), T("截图"))
CASES.append(("纯文本：新增一条", got, ["截图"]))
got = run(b, T("旧1"), T("旧2"), T("旧3"), T("截图"), T("截图"))
CASES.append(("纯文本：重发完全相同（第 1 个 bug）", got, ["截图"]))
got = run(b, T("旧1"), T("旧2"), T("旧3"), T("截图"), T("截图"))
CASES.append(("纯文本：窗口没变（第 2 个 bug）", got, []))

# ---------------- 窗口整体换掉 ----------------
c = FakeAdapter(cfg)
run(c, T("A"), T("B"), T("C"))
got = run(c, T("完全"), T("不同的"), T("内容"))
CASES.append(("无重叠（应保守不出声）", got, []))

# ---------------- 用户原始现象：连发三条相同指令 ----------------
d = FakeAdapter(cfg)
run(d, *[T(f"历史{i}") for i in range(6)])
seq = []
for i in range(3):
    seq.append(run(d, *[T(f"历史{i}") for i in range(6)], *([T("截图")] * (i + 1))))
CASES.append(("连发三条相同指令（应 1/1/1）", seq, [["截图"], ["截图"], ["截图"]]))

print("=" * 78)
print("新消息判定：窗口序列重叠（下标空间必须一致）")
print("=" * 78)
bad = 0
for name, got, want in CASES:
    ok = got == want
    if not ok:
        bad += 1
    print(f"\n  {'✓' if ok else '✗'} {name}")
    print(f"     实得 {got}")
    if not ok:
        print(f"     期望 {want}")

print()
print("=" * 78)
print("全部通过" if bad == 0 else f"!! {bad} 个用例失败")
print("=" * 78)
raise SystemExit(0 if bad == 0 else 1)
