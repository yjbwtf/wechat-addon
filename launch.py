"""无窗口地把机器人起在后台。**给 start.cmd 用。**

## 为什么需要这个小脚本

`start.cmd` 原来最后一行是直接跑 `pythonw -m src.main`（阻塞），于是那个
cmd 窗口会一直开着 —— 而且**是空的**，因为机器人是 GUI 子系统的 pythonw，
一个字都不往控制台写。这就是「还是会开一个空的 cmd 窗口」。

改成 `start "" pythonw -m src.main` 也**不保险**：`start` 起的子进程会继承
cmd 的控制台，控制台要等所有挂着的进程都退出才关，于是窗口照样留着。

所以这里换成 `src/procutil.launch_bot()` —— 它用 `DETACHED_PROCESS` 起，
子进程**不挂任何控制台**，cmd 一退窗口立刻关。设置界面的「启动机器人」
走的是同一个函数，两边不会跑偏。

用法（一般不用手敲）：

    venv\\Scripts\\python.exe launch.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.procutil import launch_bot  # noqa: E402


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    pid, err = launch_bot(ROOT)
    if not pid:
        print(f"启动失败：{err}")
        return 1
    print(f"机器人已在后台运行（pid {pid}）")
    print("  日志：data\\logs\\")
    print("  界面：start.cmd -UI")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
