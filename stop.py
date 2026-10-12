"""急停：写停止标记 + 结束机器人进程 + 中文提示。

为什么杀进程的逻辑放在这里而不是 stop.cmd 里
--------------------------------------------
原先 stop.cmd 里用 PowerShell 按**可执行文件路径**过滤：
    Get-Process python | Where-Object { $_.Path -like 'F:\\wechat_addon\\*' }
实际踩到的坑：有一个实例是用 DSH 自带的 python
（C:\\Users\\you\\.dsh\\...\\python.exe）起来的，路径不在 F:\\wechat_addon 下，
这条过滤完全看不到它 —— 急停之后它还在继续抢微信窗口。

所以这里改成三路一起杀，只要命中一路就结束：
  1. data/bot.pid 里记的那个 pid（机器人自己写的，最准）
  2. 可执行文件路径在本目录下的 python（正常情况）
  3. 命令行里带 `-m src.main` 且是 python 的进程（换了解释器也能认出来）

第 3 条限定 python.exe/pythonw.exe，且必须同时是 `-m src.main`，
不会误伤 DSH 自己用的 python 运行时（它不跑 src.main）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from src.procutil import pid_alive

ROOT = Path(__file__).resolve().parent
PID_PATH = ROOT / "data" / "bot.pid"
STOP_PATH = ROOT / "data" / "STOP"


def write_stop_flag() -> None:
    STOP_PATH.parent.mkdir(parents=True, exist_ok=True)
    STOP_PATH.write_text(
        f"stopped by stop.py at {time.strftime('%Y-%m-%d %H:%M:%S')}\n",
        encoding="utf-8",
    )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        SYNCHRONIZE = 0x00100000
        k = ctypes.windll.kernel32
        h = k.OpenProcess(SYNCHRONIZE, False, pid)
        if not h:
            return False
        try:
            return k.WaitForSingleObject(h, 0) != 0
        finally:
            k.CloseHandle(h)
    try:
        pid_alive(pid)
        return True
    except OSError:
        return False


def _pid_from_file() -> int:
    try:
        return int(PID_PATH.read_text(encoding="ascii").strip() or 0)
    except (OSError, ValueError):
        return 0


PS_SWEEP = r"""
$root = 'ROOTPLACEHOLDER'
$me = MEPLACEHOLDER
$killed = @()
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
  ForEach-Object {
    $p = $_
    if ($p.ProcessId -eq $me) { return }
    $hit = $false
    if ($p.CommandLine -and $p.CommandLine -match '-m\s+src\.main') { $hit = $true }
    if (-not $hit -and $p.ExecutablePath -and $p.ExecutablePath.StartsWith($root, 'OrdinalIgnoreCase')) {
      if (-not ($p.CommandLine -and $p.CommandLine -match 'ui_app\.py')) { $hit = $true }
    }
    if ($hit) {
      $killed += $p.ProcessId
      Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
  }
$killed -join ','
"""


def kill_bots() -> list[int]:
    """结束所有机器人进程，返回被结束的 pid 列表。"""
    killed: list[int] = []

    # 1) pid 文件里那个（无论它用什么解释器起的）
    pid = _pid_from_file()
    if pid and pid != os.getpid() and _pid_alive(pid):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=10)
            else:
                os.kill(pid, 15)
            killed.append(pid)
        except Exception:  # noqa: BLE001
            pass

    # 2)+3) 按路径 / 按命令行兜底扫一遍
    if os.name == "nt":
        script = (PS_SWEEP
                  .replace("ROOTPLACEHOLDER", str(ROOT))
                  .replace("MEPLACEHOLDER", str(os.getpid())))
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-Command", script],
                capture_output=True, text=True, timeout=40,
            ).stdout.strip()
            for part in out.split(","):
                part = part.strip()
                if part.isdigit():
                    n = int(part)
                    if n not in killed:
                        killed.append(n)
        except Exception:  # noqa: BLE001
            pass

    return killed


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    write_stop_flag()
    killed = kill_bots()

    print()
    print("=" * 52)
    print("   微信机器人已紧急停止")
    print("=" * 52)
    print("  · 已写入停止标记 data\\STOP（机器人不会再回复）")
    if killed:
        print(f"  · 已结束 {len(killed)} 个机器人进程：{', '.join(map(str, killed))}")
    else:
        print("  · 没有发现正在运行的机器人进程")
    print("  · 微信窗口已交还，不会再自动切换")
    print()
    print("  重新启动：F:\\wechat_addon\\start.cmd")
    print("=" * 52)
    time.sleep(3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
