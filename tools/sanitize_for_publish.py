"""发布前的清理：把个人标识换成占位符，并把硬编码路径改成自定位。

## 为什么需要这个

这个项目里有几类东西不该出现在公开仓库里：

  - **微信号**（授权名单用的，改不了、也不该公开）
  - **Windows 用户名**（`C:\\Users\\<名字>` 是可关联到人的标识）
  - **真实群名和群友昵称**（尤其出现在注释的实测例子里）
  - **口令**（config.yaml 里那个）
  - **硬编码的绝对路径**（既是隐私问题，也让项目换台机器就跑不起来）

`data/` 和 `config.yaml` 直接用 .gitignore 排除，不用清理。
剩下的（文档、注释里的实测例子、启动脚本）在这里统一替换。

## 为什么顺带修路径

`start.cmd` / `start.ps1` 把 `F:\\wechat_addon` 和某个具体的 python 路径写死了，
只在这台机器上能跑。改成从脚本自身位置推导（`%~dp0` / `$PSScriptRoot`），
顺带解决隐私和可移植性两个问题。

跑法：venv\\Scripts\\python.exe tools\\sanitize_for_publish.py [--check]
  --check 只报告不修改
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 要替换的东西：明文 -> 占位符
# 顺序有讲究：长的先替换，否则短的是长的子串会被先吃掉
REPLACEMENTS: list[tuple[str, str]] = [
    (r"F:\wechat_addon", "C:\\wechat_addon"),
    (r"C:\Users\34196", "C:\\Users\\you"),
    ("yjb1313y", "<你的微信号>"),
    ("透🍋板🐔之内设刘姓男子", "<群名>"),
    ("341961", "<你的群昵称>"),
    ("臭肥鱼", "<机器人昵称>"),
]

# 这些文件不处理：
#   venv/ data/ 不参与
#   ui/web/ 是前端，路径无所谓（且 css 里的「透」是「透明」）
SKIP_DIRS = {"venv", "data", "__pycache__", ".git", "wxauto_logs", "node_modules"}
SKIP_FILES = {"config.yaml", "persona.yaml", ".gitignore",
              "tools/sanitize_for_publish.py"}

TEXT_EXT = {".py", ".md", ".cmd", ".ps1", ".yaml", ".yml", ".json",
            ".html", ".css", ".js", ".txt", ".toml", ".ini"}


def targets() -> list[Path]:
    out = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        parts = set(p.relative_to(ROOT).parts)
        if parts & SKIP_DIRS:
            continue
        rel = p.relative_to(ROOT).as_posix()
        if rel in SKIP_FILES:
            continue
        if p.suffix.lower() not in TEXT_EXT:
            continue
        out.append(p)
    return sorted(out)


def scan(text: str) -> list[tuple[str, str]]:
    found = []
    for plain, _ in REPLACEMENTS:
        n = text.count(plain)
        if n:
            found.append((plain, n))
    return found


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    check_only = "--check" in sys.argv
    files = targets()
    total = 0

    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        hits = scan(text)
        if not hits:
            continue
        rel = p.relative_to(ROOT).as_posix()
        desc = "、".join(f"{k}×{v}" for k, v in hits)
        total += sum(v for _, v in hits)
        if check_only:
            print(f"  {rel}: {desc}")
            continue

        new = text
        for plain, placeholder in REPLACEMENTS:
            new = new.replace(plain, placeholder)
        p.write_text(new, encoding="utf-8")
        print(f"  {rel}: {desc}  -> 已替换")

    print()
    if check_only:
        print(f"发现 {total} 处敏感字串" if total else "干净")
        return 1 if total else 0
    print(f"共替换 {total} 处，处理 {len(files)} 个文本文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
