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

print()
print("课程表解析（两种写法）")
print()
from src.config import parse_courses  # noqa: E402

check("字符串写法（界面用的）",
      parse_courses(["舞台人生 | https://a/x"]) ==
      [{"name": "舞台人生", "url": "https://a/x"}])
check("对象写法（手写用的）",
      parse_courses([{"name": "高数", "url": "https://b/y"}]) ==
      [{"name": "高数", "url": "https://b/y"}])
check("全角竖线也认",
      parse_courses(["舞台人生 ｜ https://a/x"])[0]["url"] == "https://a/x")
check("两种写法可以混着写",
      len(parse_courses(["A | https://a", {"name": "B", "url": "https://b"}])) == 2)
check("只有网址时用它当名字（至少能按 url 片段命中）",
      parse_courses(["https://c/z"]) == [{"name": "https://c/z", "url": "https://c/z"}])
check("写坏的行只跳过它自己", parse_courses(["坏的 |", "", "  ", "好 | https://a"]) ==
      [{"name": "好", "url": "https://a"}])

print()
print("保存路径（界面写回去那一侧）—— 在临时副本上做，绝不碰真配置")
print()
import shutil  # noqa: E402
import tempfile  # noqa: E402
import ui_bridge as B  # noqa: E402

tmpdir = Path(tempfile.mkdtemp(prefix="brushtest-"))
try:
    tmp = tmpdir / "config.yaml"
    shutil.copyfile(CONFIG_PATH, tmp)
    doc = B.load_doc(tmp)
    # 这就是界面点保存时走的那条路：set_path 按点号路径写值
    B.set_path(doc, "brush.courses",
               ["舞台人生 | https://a.example/x", "高等数学 | https://b.example/y"])
    B.set_path(doc, "brush.enabled", True)
    tmp.write_text(B.dump_doc(doc, tmp), encoding="utf-8")
    back = load_config(tmp)
    check("存回去再读回来，课程表还在",
          [c["name"] for c in back.brush.courses] == ["舞台人生", "高等数学"],
          "得到 %r" % (back.brush.courses,))
    check("url 没被 YAML 转义搞坏",
          back.brush.courses[0]["url"] == "https://a.example/x",
          "得到 %r" % (back.brush.courses[0]["url"],))
    # 顺带确认没把别的段写坏
    check("其它配置段没被动",
          back.dsh.profile == cfg.dsh.profile
          and back.commands.secret == cfg.commands.secret)
finally:
    shutil.rmtree(tmpdir, ignore_errors=True)

failed = [c for c in cases if not c[1]]
print()
if failed:
    print("不通过 %d / %d" % (len(failed), len(cases)))
    raise SystemExit(1)
print("全部通过（%d 项）" % len(cases))
