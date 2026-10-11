"""以管理员权限执行**预先批准**的操作（走计划任务，不弹 UAC）。

## 用法

    venv\\Scripts\\python.exe tools\\elevated_request.py whoami
    venv\\Scripts\\python.exe tools\\elevated_request.py flush-dns
    venv\\Scripts\\python.exe tools\\elevated_request.py restart-service '{"service":"Spooler"}'
    venv\\Scripts\\python.exe tools\\elevated_request.py --list

## 它是怎么工作的

    写 data\\request.json  ->  schtasks /run /tn WeChatElevated  ->  轮询 result.txt

计划任务以完整管理员令牌运行 `%ProgramData%\\WeChatElevated\\run.ps1`，
那份脚本只认白名单里的动作。

## 为什么不能想跑什么就跑什么

**`action` 必须在 `run.ps1` 的白名单里**，这是设计如此，不是限制没做全：

  - 机器人（和 agent）跑在 Medium 完整性下，本来碰不到管理操作；
  - 把整个机器人提权 = agent 拿到完整管理员权限，一条错命令就能搞坏系统；
  - 所以只开一条窄通道：**你预先批准几个动作，agent 只能选，不能塞命令**。

要加动作就改 `tools/elevated/run.ps1` 的白名单，然后**重跑一次
`tools/setup_elevated.ps1`（管理员）** —— 因为计划任务跑的是 ProgramData
里的副本，不重跑就还是旧的。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PROGRAM_DATA = os.environ.get("ProgramData", r"C:\ProgramData")
BASE = Path(PROGRAM_DATA) / "WeChatElevated"
DATA_DIR = BASE / "data"
REQUEST = DATA_DIR / "request.json"
RESULT = DATA_DIR / "result.txt"
TASK_NAME = "WeChatElevated"


def die(msg: str, code: int = 2) -> None:
    print(msg)
    raise SystemExit(code)


def list_actions() -> None:
    """从 run.ps1 里把白名单动作名抠出来，省得用户去翻脚本。

    只看形如 `'动作名' {` 的行（动作名是小写字母/数字/连字符），
    不试着解析大括号结构 —— 那样会被动作体内部的 `}` 带偏，
    实测第一版就是这么只读出一个动作的。
    """
    src = Path(__file__).resolve().parent / "elevated" / "run.ps1"
    names: list[str] = []
    if src.exists():
        text = src.read_text(encoding="utf-8-sig")
        for m in re.finditer(r"^\s*'([a-z][a-z0-9_-]*)'\s*\{\s*$", text, re.M):
            name = m.group(1)
            if name != "default" and name not in names:
                names.append(name)
    print("已安装的提权动作：")
    if names:
        for n in names:
            print(f"  {n}")
    else:
        print("  （读不到 run.ps1，或还没装）")


def check_installed() -> None:
    if not BASE.exists() or not (BASE / "run.ps1").exists():
        die(f"提权通道还没安装（找不到 {BASE / 'run.ps1'}）。\n"
            f"先以管理员身份跑一次：\n"
            f"    powershell -ExecutionPolicy Bypass -File tools\\setup_elevated.ps1")
    r = subprocess.run(["schtasks", "/query", "/tn", TASK_NAME],
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        die(f"计划任务 {TASK_NAME} 不存在。\n"
            f"重新跑一次安装脚本：tools\\setup_elevated.ps1（管理员）")


def run_action(action: str, args: dict | None = None, timeout: float = 120.0) -> int:
    check_installed()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    RESULT.unlink(missing_ok=True)
    payload = {"action": action}
    if args:
        payload["args"] = args
    REQUEST.write_text(json.dumps(payload, ensure_ascii=False),
                       encoding="utf-8")

    r = subprocess.run(["schtasks", "/run", "/tn", TASK_NAME],
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        REQUEST.unlink(missing_ok=True)
        die(f"触发计划任务失败：{(r.stdout + r.stderr).strip()}")

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.4)
        if RESULT.exists():
            # 计划任务刚写完文件，给它一点时间落盘
            time.sleep(0.15)
            try:
                data = json.loads(RESULT.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out = str(data.get("output") or "").strip()
            if data.get("ok"):
                print(out or "（动作完成，没有输出）")
                return 0
            print(f"失败：{out}")
            return 1

    REQUEST.unlink(missing_ok=True)
    die(f"等 {timeout:.0f} 秒没等到结果。任务可能没跑起来（看任务计划程序里的 "
        f"{TASK_NAME}），或者动作卡住了。")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] in ("-l", "--list"):
        list_actions()
        return 0

    action = argv[0]
    args = None
    if len(argv) > 1:
        try:
            args = json.loads(argv[1])
        except ValueError as exc:
            die(f"参数不是合法 JSON：{exc}")
        if not isinstance(args, dict):
            die("参数必须是一个 JSON 对象，例如 '{\"service\":\"Spooler\"}'")
    return run_action(action, args)


if __name__ == "__main__":
    raise SystemExit(main())
