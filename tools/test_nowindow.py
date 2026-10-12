"""验证「没有控制台的进程去起子进程，会不会弹出黑框」。

## 为什么需要这个测试

机器人是用 pythonw.exe 起的（GUI 子系统，**没有控制台**）。
这种进程去起一个控制台程序（cmd.exe、schtasks、ping…）时，Windows 会给
子进程**新建一个控制台窗口**。症状是屏幕上闪一下、或者杵着一个空黑框，
而且极难联想到是哪一行代码干的 —— 看起来像"启动脚本的问题"。

修法是给 subprocess 传 CREATE_NO_WINDOW。本测试同时验证两件事：

  1. **不加**标志时确实会弹窗  —— 否则就是枚举没抓到窗口，
     那"加了标志没弹窗"这个结论毫无意义（测试自己骗自己）。
  2. **加了** `procutil.no_window()` 时不弹。

第 1 条是重点。之前查"截图的判断词"和"LoadMoreCache"时都在这里翻过车：
只验证"现在没有坏现象"，而没验证"测试有能力看到坏现象"。

## 怎么跑

必须用 pythonw（用 python 的话父进程自己有控制台，子进程会继承它、
根本不新建窗口，什么都测不出来 —— 脚本会检测到这一点并拒绝给结论）：

    venv\\Scripts\\pythonw.exe tools\\test_nowindow.py
    结果在 data\\logs\\test-nowindow.txt

退出码：0 = 通过，1 = 不通过，2 = 环境不对（结论不可信）。
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.procutil import no_window  # noqa: E402

OUT = ROOT / "data" / "logs" / "test-nowindow.txt"

#: 用 ping 而不是 timeout —— timeout.exe 要求 stdin 是控制台，重定向后会报错。
HOLD = ["cmd.exe", "/c", "ping -n 6 127.0.0.1 >nul"]
SETTLE = 1.5          # 等窗口真的建出来

#: 「控制台宿主」进程名。**不能只认 ConsoleWindowClass**：
#: Win11 把 Windows Terminal 设为默认终端后，console 应用的窗口类名是
#: CASCADIA_HOSTING_WINDOW_CLASS、宿主是 WindowsTerminal.exe。
#: 第一版测试就栽在这上面 —— 它一个窗口都没抓到，于是「加了标志也没弹窗」
#: 变成了毫无意义的通过（好在它自己检测到「不加标志时也没看到窗口」并拒绝
#: 给结论，这正是那个反向断言存在的意义）。
HOSTS = {"windowsterminal.exe", "openconsole.exe", "conhost.exe"}

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def has_console() -> bool:
    return bool(_k32.GetConsoleWindow())


def _proc_name(pid: int) -> str:
    handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if _k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name.lower()
        return ""
    finally:
        _k32.CloseHandle(handle)


def console_windows() -> set[int]:
    """屏幕上可见的控制台窗口（句柄集合）。

    认「窗口所属进程是不是控制台宿主」，而不是认窗口类名 —— 见 HOSTS 的注释。
    """
    found: set[int] = set()

    def cb(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _proc_name(pid.value) in HOSTS:
            found.add(int(hwnd))
        return True

    _user32.EnumWindows(_WNDENUMPROC(cb), 0)
    return found


def probe(flags: int, argv: list[str] | None = None) -> set[int]:
    """起一个持续几秒的控制台程序，返回它新开出来的窗口。

    argv 默认是 cmd.exe 包一层；传 argv 可以绕过包装直接起目标程序。
    这两种情况的结果**不一样**（cmd.exe 会用它自己的默认标志再起子进程，
    于是孙进程照样弹窗），所以两处都要能测。
    """
    before = console_windows()
    proc = subprocess.Popen(
        argv or HOLD,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    try:
        time.sleep(SETTLE)
        return console_windows() - before
    finally:
        proc.kill()
        proc.wait()
        time.sleep(0.4)          # 让它开的窗口来得及消失，别污染下一轮


def main() -> int:
    lines: list[str] = []

    def say(text: str = "") -> None:
        lines.append(text)

    say("无窗口启动测试  %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    say("父进程有控制台：%s" % has_console())

    if has_console():
        say("")
        say("环境不对：本进程自己有控制台，子进程会继承它，不会新建窗口。")
        say("用 venv\\Scripts\\pythonw.exe 跑本脚本。")
        _write(lines)
        return 2

    baseline = console_windows()
    say("起始时屏幕上的控制台窗口：%d 个" % len(baseline))
    say("")

    say("[1/3] 不加标志起 cmd.exe（期望：弹出一个新窗口）")
    plain = probe(0)
    say("      新窗口数 = %d" % len(plain))
    say("")

    say("[2/3] 加 no_window() 起 cmd.exe（期望：一个都不弹）")
    quiet = probe(no_window())
    say("      新窗口数 = %d" % len(quiet))
    say("")

    # [3] 这一条是把踩过的坑固化下来。初始诊断时我看到
    # 「DETACHED_PROCESS 也弹窗」，差点得出「DETACHED 没用」的错误结论 ——
    # 真相是 cmd.exe 自己确实没有控制台了，**但它起 ping 时 ping 又新建了一个**。
    # 也就是说：DETACHED 只对直接子进程有效，孙进程照样弹。
    # 所以「起一个会再起子进程的控制台程序」必须用 CREATE_NO_WINDOW。
    direct = ["ping", "-n", "6", "127.0.0.1"]
    say("[3/3] 直接起 ping.exe（不经 cmd.exe 包装）")
    det = probe(getattr(subprocess, "DETACHED_PROCESS", 0x00000008), direct)
    say("      DETACHED_PROCESS    -> %d 个窗口" % len(det))
    nw = probe(no_window(), direct)
    say("      CREATE_NO_WINDOW    -> %d 个窗口" % len(nw))
    say("      会话小结：DETACHED_PROCESS 对「直接子进程」有效，")
    say("      但子进程再起孙进程时孙进程会新建控制台；CREATE_NO_WINDOW 没有这个问题。")
    say("")

    ok_bad = len(plain) > 0        # 不加标志时能看见 -> 测试有感知能力
    ok_fix = len(quiet) == 0       # 加了标志时不弹    -> 修复有效
    ok_direct = len(nw) == 0       # 直接子进程上同样不弹

    if not ok_bad:
        say("不通过：不加标志时也没看到窗口。")
        say("  要么枚举方式不对，要么这台机器上控制台窗口另有来源。")
        say("  在这种情况下「加了标志也没弹窗」不能作为修复有效的证据。")
    if not ok_fix:
        say("不通过：加了 CREATE_NO_WINDOW 还是弹了 %d 个窗口。" % len(quiet))
    if not ok_direct:
        say("不通过：直接起 ping.exe 时 CREATE_NO_WINDOW 没挡住（%d 个窗口）。"
            % len(nw))
    if ok_bad and ok_fix and ok_direct:
        say("通过：不加标志弹了 %d 个；加 no_window() 后 0 个（cmd 包装和直接起都是）。"
            % len(plain))

    _write(lines)
    return 0 if (ok_bad and ok_fix and ok_direct) else 1


def _write(lines: list[str]) -> None:
    text = "\n".join(lines) + "\n"
    try:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with OUT.open("a", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass
    try:
        if sys.stdout is not None:
            sys.stdout.write(text)
            sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    raise SystemExit(main())
