"""重启机器人服务。

## 为什么必须是独立进程

机器人**没法重启自己** —— 它一旦退出，就没人负责拉起新实例了。
所以流程是：

    机器人收到 \\restart
      -> 启动本脚本（DETACHED，脱离父进程，父进程死了它还在）
      -> 回一句「重启中」
      -> 自己硬退出
    本脚本
      -> 等旧进程真的死透（否则新实例会被单例守卫挡回去）
      -> 清掉 data\\STOP（否则新实例起来也不会回话）
      -> 拉起 start.cmd
      -> 记一行日志

## 为什么要等「死透」

`Bot.run()` 开头有个单例守卫：发现已有实例就直接退出（exit 3）。
旧进程没死干净就拉新的，新实例会立刻退出，结果就是**机器人彻底没了**。
所以这里必须等到 pid 真的不存在，而不是发个 kill 就往下走。

## 为什么单独清 STOP

`start.cmd` 自己会清 STOP，但**只在正常启动路径上**。
这里显式再清一遍，免得将来有人改了 start.cmd 的顺序就踩坑。

用法（一般不用手敲，`\\restart` 指令会调它）：

    venv\\Scripts\\python.exe restart.py [旧进程pid]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from src.procutil import launch_bot, pid_alive

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "data" / "logs" / "restart.log"
PID = ROOT / "data" / "bot.pid"
STOP = ROOT / "data" / "STOP"

#: 等旧进程退出的上限。给足余量：bot 退出时还要收尾（关 store、落盘线程）。
WAIT_SECONDS = 45.0


def log(text: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {text}"
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    print(line)


def alive(pid: int) -> bool:
    return pid_alive(pid)


def read_pid() -> int:
    try:
        return int(PID.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    # **优先用命令行参数**，不要读 bot.pid。
    # 原因：Bot.close() 会删掉 data/bot.pid，而本进程是并发启动的 ——
    # 启动稍慢就会读到「文件已删」，拿不到 pid 就不等，直接拉起新实例；
    # 旧进程还活着时新实例会被单例守卫挡回去，结果是机器人彻底没了。
    # 调用方（handle_restart）手里有确切的 pid，直接传进来就没有这个竞态。
    target = 0
    if len(sys.argv) > 1:
        try:
            target = int(sys.argv[1])
        except ValueError:
            target = 0
    if not target:
        target = read_pid()
        if target:
            log("警告：pid 是从 bot.pid 读的（调用方没传）。有竞态风险。")
    log(f"重启开始 | 旧 pid={target or '（读不到）'}")

    # 1) 等旧进程死透。**这一步不能省**：没死干净就拉新的，
    #    新实例会被单例守卫挡回去，结果是机器人彻底没了。
    if target:
        deadline = time.time() + WAIT_SECONDS
        while time.time() < deadline and alive(target):
            time.sleep(0.5)
        if alive(target):
            log(f"等 {WAIT_SECONDS:.0f} 秒旧进程 {target} 还没退，继续往下走（可能有两个实例）")
        else:
            log(f"旧进程 {target} 已退出")

    # 2) 清 STOP：留着的话新实例起来也不会回话
    if STOP.exists():
        STOP.unlink(missing_ok=True)
        log("已清掉 data\\STOP")

    # 3) 拉起新实例。**必须脱离**本进程，否则新实例会跟着一起被回收。
    #
    # **不走 start.cmd。** 它是靠 cmd 的内建命令 `start` 起进程的，而 `start`
    # 需要控制台来创建新窗口；我们这里刻意不要控制台，那一步会**静默失败**
    # —— 实测 restart.log 里每步都写「成功」，机器人却再也没起来。
    # 重启场景也不需要 start.cmd 的首次运行逻辑（建 venv、装依赖）。
    #
    # 具体怎么起交给 src/procutil.launch_bot()：设置界面的「启动机器人」
    # 走的是同一个函数，两边不会各写一套再慢慢跑偏。
    pid, err = launch_bot(ROOT)
    if not pid:
        log(f"拉起机器人失败：{err}")
        return 1

    log(f"已拉起新实例（pid {pid}），重启流程结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
