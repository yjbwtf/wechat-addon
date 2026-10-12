"""`tools/check_command.py` 的测试 —— 重点是**它不能和机器人说两套话**。

这个脚本存在的意义就是「查机器人会怎么处理」，所以它一旦和机器人分叉
就比没有还糟。因此这里两头都跑：

  1. 表格比对：一批文本 -> 期望的判定
  2. **分歧检查**：对每一条会走到查询的文本，再真的跑一遍
     `Bot.handle_enka`（把 enka.query 打桩），断言两边解析出的
     游戏/UID/角色名**完全一致**

第 2 条是核心 —— 脚本里 `analyze()` 的判定顺序一旦和 `Bot.handle` 不一致
（比如白名单判早了、控制指令传错了参数），这里就会红。

（这个文件本身就是「写个脚本检测文本与指令匹配」这个要求的另一半：
脚本给人看，测试保证脚本不撒谎。）
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import check_command  # noqa: E402
from src import enka  # noqa: E402
from src.config import load_config  # noqa: E402
from src.policy import Gatekeeper  # noqa: E402
from src.wechat_io import IncomingMessage  # noqa: E402
import src.main as main  # noqa: E402

cfg = load_config(ROOT / "config.yaml")
BOT = cfg.bot_names[0]
GROUP = cfg.whitelist_groups[0]          # 白名单群
OTHER = "一个不在白名单的群"
# 可信会话从配置里取（不写字面量 —— 那会把主人的会话名写进仓库）
PRIVATE = cfg.commands.trusted_chats[0] if cfg.commands.trusted_chats else ""

cases: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    cases.append((name, ok, detail))
    print("  %s  %s%s" % ("[OK]" if ok else "[!!]", name,
                          ("   " + detail) if detail and not ok else ""))


# ------------------------------------------------------------------ 打桩
seen: list[tuple] = []
_real_query = enka.query


def stub_query(game, uid, c, cache, focus=None, weights=None):
    seen.append((game, uid, focus))
    return True, "（打桩）"


class StubStop:
    stopped = False


class FakeBot:
    """只为跑 handle_enka —— 它需要的东西很少。"""

    def __init__(self) -> None:
        self.cfg = cfg
        self.gate = Gatekeeper(cfg)
        self.paused = False
        self.stop = StubStop()
        self.enka_cache = None

    def _say(self, *a, **k):
        pass


FakeBot.handle_enka = main.Bot.handle_enka


def bot_parse(text: str, group: str) -> tuple | None:
    """真的跑一遍机器人的那条路，返回它解析出的 (game, uid, focus)。"""
    gate = Gatekeeper(cfg)
    body = gate.public_command(group, text)
    if body is None:
        body = gate.as_command(text, trusted=gate.is_trusted_chat(group))
    if body is None:
        return None
    seen.clear()
    FakeBot().handle_enka(
        IncomingMessage(group=group, sender=PRIVATE or "主人", content=text,
                        msg_key=text),
        body, public=True)
    return seen[0] if seen else None


# ------------------------------------------------------------------ 表格
EC = "enka"      # 会走到查询
CHAT = "chat"
IGNORED = "ignored"
BLOCKED = "blocked"

TABLE: list[tuple[str, str, str, tuple | None]] = [
    # (文本, 群, 期望 kind, 期望的 (game, uid, focus) 或 None)
    ("@%s /查询 原神 137602053 可莉" % BOT, GROUP, EC,
     ("gi", "137602053", "可莉")),
    # ↓ 用户实际发出来的那条（他的触发词里有「查」）
    ("@%s\u2005/查 原神 UID137602053 可莉" % BOT, GROUP, EC,
     ("gi", "137602053", "可莉")),
    ("@%s /查询 原神 UID137602053 可莉" % BOT, GROUP, EC,
     ("gi", "137602053", "可莉")),
    ("@%s /查询 uid 137602053 可莉" % BOT, GROUP, EC,
     (None, "137602053", "可莉")),
    ("@%s /查询原神UID137602053可莉" % BOT, GROUP, EC,
     ("gi", "137602053", "可莉")),
    ("@%s/查询原神137602053可莉" % BOT, GROUP, EC,
     ("gi", "137602053", "可莉")),
    ("@%s /查询原神 137602053 可莉" % BOT, GROUP, EC,
     ("gi", "137602053", "可莉")),
    ("@%s /查询137602053" % BOT, GROUP, EC, (None, "137602053", None)),
    ("@%s /查询 原神 137602053" % BOT, GROUP, EC, ("gi", "137602053", None)),
    # UID 后面的名字不能被当指令词剥掉
    ("@%s /查询 绝区零 14327029 11号" % BOT, GROUP, EC,
     ("zzz", "14327029", "11号")),
    ("@%s /查询 14327029 雅" % BOT, GROUP, EC, (None, "14327029", "雅")),
    # 控制指令：反斜杠，不要求白名单
    ("@%s \\uid 137602053" % BOT, GROUP, "control:enka", (None, "137602053", None)),
    ("@%s \\uid 137602053" % BOT, OTHER, "control:enka", (None, "137602053", None)),
    # 公开指令要求白名单
    ("@%s /查询 137602053" % BOT, OTHER, BLOCKED, None),
    # 没 @ 不走公开指令
    ("/查询 137602053", GROUP, CHAT, None),
    # 前缀用错：截图是控制指令
    ("@%s /截图" % BOT, GROUP, IGNORED, None),
    # 普通聊天
    ("@%s 你好呀" % BOT, GROUP, CHAT, None),
    # 不认识的公开指令
    ("@%s /随便乱写" % BOT, GROUP, IGNORED, None),
]

print("脚本判定 + 与机器人一致（%d 条）" % len(TABLE))
print()
enka.query = stub_query
try:
    for text, group, want_kind, want_args in TABLE:
        res = check_command.analyze(text, cfg, group)
        got_kind = res["kind"]
        short = text.replace(BOT, "机器人")
        check("%-40s 判定 = %s" % (short, want_kind),
              got_kind == want_kind, "得到 %s" % got_kind)

        if want_kind == EC or want_kind == "control:enka":
            d = res["detail"]
            got_args = (d.get("game"), d.get("uid"), d.get("focus"))
            check("  └ 参数 = %s" % (want_args,), got_args == want_args,
                  "得到 %s" % (got_args,))
            # **分歧检查**：同一段文本真跑一遍机器人，看是不是同一个结果
            real = bot_parse(text, group)
            check("  └ 和机器人一致", real == want_args,
                  "脚本 %s / 机器人 %s" % (got_args, real))
finally:
    enka.query = _real_query

# 私有会话：免 @、免口令
if PRIVATE:
    print()
    print("私聊（可信会话 %s）" % PRIVATE)
    print()
    for text, want in (("/查询 原神 137602053 可莉", ("gi", "137602053", "可莉")),
                       ("/查询137602053可莉", (None, "137602053", "可莉")),
                       ("/uid 137602053", (None, "137602053", None))):
        res = check_command.analyze(text, cfg, PRIVATE)
        d = res["detail"]
        got = (d.get("game"), d.get("uid"), d.get("focus"))
        check("%-28s -> %s" % (text, want), got == want, "得到 %s" % (got,))

# 脚本自己的 bug 也要能测出来：控制指令传错参数会变 focus='\uid'
print()
print("反向断言：脚本必须用「前缀之后的正文」，不能是去掉 @ 的原文")
print()
res = check_command.analyze("@%s \\uid 137602053" % BOT, cfg, GROUP)
check("focus 里**不能**出现反斜杠",
      "\\" not in (res["detail"].get("focus") or ""),
      str(res["detail"]))

failed = [c for c in cases if not c[1]]
print()
if failed:
    print("不通过 %d / %d" % (len(failed), len(cases)))
    raise SystemExit(1)
print("全部通过（%d 项）" % len(cases))
