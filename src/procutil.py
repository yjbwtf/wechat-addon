"""进程探活工具。**别用 os.kill(pid, 0)。**

## 为什么单独一个模块

Windows 上 `os.kill(pid, sig)` 对任何非 `CTRL_C_EVENT`/`CTRL_BREAK_EVENT`
的 sig 都会调 `TerminateProcess` —— **无条件杀掉目标进程**。
`os.kill(pid, 0)` 就是「以退出码 0 杀掉它」，不是探活。

这个坑在项目里散布了 5 处，症状还很隐蔽：
  · 打开/刷新设置界面 -> 可能把机器人杀掉（界面取状态时会探活）
  · 单例守卫 -> 判断"已有一个实例"的逻辑不可信
  · 重启 -> 新实例被守卫挡回去，机器人再也起不来

集中到一个函数，免得下次又有人在别处写 os.kill。
"""

from __future__ import annotations


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
