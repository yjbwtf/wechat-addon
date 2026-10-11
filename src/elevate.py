"""调用提权通道（%ProgramData%\\WeChatElevated）的公共实现。

`tools/elevated_request.py` 是这个模块的命令行外壳，机器人的 `\\开` 指令也用它 ——
两边共用一份逻辑，免得"命令行能用、机器人不能用"这种分歧。

通道的原理见 tools/elevated/run.ps1 的注释。要点：写请求 → 触发计划任务 →
等结果。计划任务以完整管理员令牌运行，所以**不弹 UAC**。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

PROGRAM_DATA = os.environ.get("ProgramData", r"C:\ProgramData")
BASE = Path(PROGRAM_DATA) / "WeChatElevated"
DATA_DIR = BASE / "data"
REQUEST = DATA_DIR / "request.json"
RESULT = DATA_DIR / "result.txt"
TASK_NAME = "WeChatElevated"

#: 计划任务启动 + 脚本执行的开销实测在 1~2 秒；给足余量
DEFAULT_TIMEOUT = 60.0


def installed() -> bool:
    """通道装好了没。没装时上层要给一句人话，而不是抛异常。"""
    return (BASE / "run.ps1").exists()


def run(action: str, args: dict | None = None,
        timeout: float = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """跑一个白名单动作。返回 (成功?, 输出文本)。不抛异常。

    调用方拿到的是能直接发到群里的一句话，所以失败也返回人话。
    """
    if not installed():
        return False, ("提权通道还没装。管理员跑一次 "
                       "tools\\setup_elevated.ps1 就有了。")

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        RESULT.unlink(missing_ok=True)
        payload: dict = {"action": action}
        if args:
            payload["args"] = args
        REQUEST.write_text(json.dumps(payload, ensure_ascii=False),
                           encoding="utf-8")
    except OSError as exc:
        return False, f"写请求失败：{exc}"

    proc = subprocess.run(["schtasks", "/run", "/tn", TASK_NAME],
                          capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        REQUEST.unlink(missing_ok=True)
        detail = (proc.stdout + proc.stderr).strip().splitlines()
        return False, ("触发计划任务失败："
                       + (detail[0] if detail else "未知原因")
                       + "（任务可能没注册，重跑一次 setup_elevated.ps1）")

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.4)
        if not RESULT.exists():
            continue
        time.sleep(0.15)          # 等它落盘
        try:
            data = json.loads(RESULT.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out = str(data.get("output") or "").strip()
        if data.get("ok"):
            return True, out or "（完成，没有输出）"
        return False, out or "失败了，但脚本没给出原因"

    REQUEST.unlink(missing_ok=True)
    return False, (f"等了 {timeout:.0f} 秒没结果。"
                   f"八成是计划任务没跑起来，或者动作卡住了。")
