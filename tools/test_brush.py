"""`\\刷课` 的匹配与识别逻辑：不联网、不开浏览器、不花钱。

只验两件事：
  1. 指令能被认出来（走的是和其它指令同一套 classify_command）
  2. 课程名怎么匹配（双向包含 + 去废话词 + 直接给网址）

**故意不测真的打开浏览器** —— 那会在别人机器上弹一个窗口出来。
真正的打开动作只有一句 os.startfile / Popen，不值得为它写需要副作用的测试。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import CONFIG_PATH, load_config  # noqa: E402
from src.main import match_course              # noqa: E402
from src.policy import Gatekeeper              # noqa: E402

cases: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    cases.append((name, ok, detail))
    print("  %s  %s%s" % ("[OK]" if ok else "[!!]", name,
                          ("   " + detail) if detail and not ok else ""))


COURSES = [
    {"name": "舞台人生：走进戏剧艺术", "url": "https://example.invalid/a"},
    {"name": "高等数学", "url": "https://example.invalid/b"},
]

print("课程匹配")
print()
check("写子串能命中", (match_course(COURSES, "舞台") or {}).get("name")
      == "舞台人生：走进戏剧艺术")
check("写全名能命中", (match_course(COURSES, "高等数学") or {}).get("url")
      == "https://example.invalid/b")
check("输入比课名长也能命中（双向包含）",
      (match_course(COURSES, "舞台人生：走进戏剧艺术 2026春") or {}).get("name")
      == "舞台人生：走进戏剧艺术")
check("夹废话词也能命中",
      (match_course(COURSES, "帮我开一下舞台") or {}).get("name")
      == "舞台人生：走进戏剧艺术")
check("大小写无关（英文课名）",
      (match_course([{"name": "CS50", "url": "u"}], "cs50") or {}).get("url") == "u")
check("匹配不到返回 None", match_course(COURSES, "量子力学") is None)
check("空输入返回 None", match_course(COURSES, "   ") is None)
check("课程表为空返回 None", match_course([], "舞台") is None)
check("全是废话词时返回 None", match_course(COURSES, "帮我一下那个") is None)
check("缺 name 的条目不会崩",
      match_course([{"url": "u"}], "舞台") is None)

print()
print("指令识别（用真实 config.yaml，不改任何东西）")
print()
cfg = load_config(CONFIG_PATH)
gate = Gatekeeper(cfg)
words = {"agent": cfg.agent.words, "shot": cfg.screenshot.words,
         "launch": cfg.launch.words, "brush": cfg.brush.words}
for body, want in [("刷课 舞台", "brush"),
                   ("刷课", "brush"),
                   ("上课 高等数学", "brush"),
                   ("挂课", "brush")]:
    got = gate.classify_command(gate.strip_secret(body), words)
    check("%-14r -> %s" % (body, want), got == want, "得到 %r" % (got,))

failed = [c for c in cases if not c[1]]
print()
if failed:
    print("不通过 %d / %d" % (len(failed), len(cases)))
    raise SystemExit(1)
print("全部通过（%d 项）" % len(cases))
