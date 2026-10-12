"""进程工具：探活（别用 os.kill）＋ 无窗口地起机器人。

## 为什么单独一个模块

Windows 上 `os.kill(pid, sig)` 对任何非 `CTRL_C_EVENT`/`CTRL_BREAK_EVENT`
的 sig 都会调 `TerminateProcess` —— **无条件杀掉目标进程**。
`os.kill(pid, 0)` 就是「以退出码 0 杀掉它」，不是探活。

这个坑在项目里散布了 5 处，症状还很隐蔽：
  · 打开/刷新设置界面 -> 可能把机器人杀掉（界面取状态时会探活）
  · 单例守卫 -> 判断"已有一个实例"的逻辑不可信
  · 重启 -> 新实例被守卫挡回去，机器人再也起不来

集中到一个函数，免得下次又有人在别处写 os.kill。

## 控制台窗口

同一个模块里还放 `no_window()` 和 `launch_bot()`：机器人自己是用
pythonw.exe 起的（没有控制台），而 **没有控制台的进程去起一个控制台程序，
Windows 会给那个子进程新建一个控制台窗口**。于是本来已经"没有黑框"的机器人
会在这些时刻又弹一个空窗口出来：

    \\开 原神      -> elevate.py 调 schtasks
    界面「启动机器人」 -> 调 cmd.exe
    界面「关闭机器人」 -> 调 cmd.exe
    回退到 dsh.cmd 思考 -> brain.py 调 cmd.exe

症状特别迷惑：窗口是**空的**，而且一闪而过（schtasks）或者一直杵在那
（cmd.exe /c start 那个会活到机器人退出）。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def no_window() -> int:
    """传给 `subprocess` 的 creationflags：别给控制台程序弹窗口。

    非 Windows 上返回 0（`subprocess.CREATE_NO_WINDOW` 只有 Windows 有，
    直接取属性会 AttributeError）。
    """
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def detached_flags() -> int:
    """脱离父进程 + 不给窗口。用于起那种"父进程死了它还要活"的进程。"""
    if os.name != "nt":
        return 0
    return (getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))


def bot_interpreter(root) -> Path | None:
    """机器人该用哪个解释器。虚拟环境没建好时返回 None。

    pythonw.exe 优先（无窗口）；它其实就是解释器，只是子系统是 GUI。
    """
    root = Path(root)
    for name in ("pythonw.exe", "python.exe"):
        exe = root / "venv" / "Scripts" / name
        if exe.exists():
            return exe
    return None


def launch_bot(root, *, log_name: str = "bot-console.log") -> tuple[int, str]:
    """无窗口、脱离本进程地拉起机器人。返回 (pid, 错误说明)。

    成功时 pid > 0 且错误说明为空；失败时 pid == 0，错误说明是给人看的。

    **不走 start.cmd。** 它是靠 cmd 的内建命令 `start` 起进程的，而 `start`
    需要控制台才能创建窗口 —— 我们这里刻意不要控制台，那一步会静默失败，
    结果就是"日志写着启动成功，机器人却没起来"（restart.py 上踩过）。
    start.cmd 独有的首次运行逻辑（建 venv、装依赖、清 STOP）由调用方负责。

    子进程输出接到 data/logs/<log_name>，也就是设置界面日志区读的那个文件 ——
    否则隐藏窗口之后，wxauto4 的横幅和崩溃栈就再也看不到了。
    """
    root = Path(root)
    exe = bot_interpreter(root)
    if exe is None:
        return 0, "找不到虚拟环境解释器，先跑一次 start.cmd -Setup"

    out_path = root / "data" / "logs" / log_name
    fh = None
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(out_path, "a", encoding="utf-8")
    except OSError:
        fh = None                      # 连日志都开不了，也别因此起不来

    try:
        proc = subprocess.Popen(
            [str(exe), "-m", "src.main"],
            cwd=str(root),
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=fh or subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            creationflags=detached_flags(),
        )
    except OSError as exc:
        return 0, f"启动失败：{exc}"
    finally:
        # 关掉父进程这一份句柄即可：子进程继承的句柄仍然有效。
        if fh is not None:
            fh.close()
    return proc.pid, ""


def pid_alive(pid: int) -> bool:
    """进程还活着吗。**只查询，绝不修改。**

    OpenProcess(SYNCHRONIZE) 拿到句柄说明进程存在（哪怕正在退出）；
    拿不到说明不存在。再用 WaitForSingleObject 确认它还没结束，
    因为句柄可能在进程已退出后仍然有效（僵尸句柄）。

    拿不到句柄也可能是权限不足 —— 那种情况返回 False，宁可当成"没在跑"，
    也不要因为探活把别人的进程杀掉。
    """
    if not pid or int(pid) <= 0:
        return False
    pid = int(pid)
    try:
        import ctypes
        from ctypes import wintypes

        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x00000102

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

        handle = k32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            return k32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            k32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return False
