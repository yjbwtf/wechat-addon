"""设置界面的启动器：本地服务 + WebView2 窗口。

## 为什么这么拆

  ui_webui.py   后端：状态读取、保存（走 ui_bridge 那套备份/校验/回滚）、机器人启停
  ui/web/       前端：一个页面，完全由 schema 生成
  本文件        宿主：起服务，然后开一个无浏览器边框的窗口指向它

好处是同一个页面既能被 WebView2 窗口加载，也能直接拿浏览器打开调界面：
    venv\\Scripts\\python.exe ui_app.py --browser
调前端时不用反复开窗口。

## 依赖

pywebview（约 500KB）。Windows 上它用系统自带的 WebView2 运行时（Win11 内置，
这台机器已确认装了 155.x）。没装 pywebview 时自动退回系统浏览器，
功能一样，只是多一圈浏览器边框。

## 端口

默认 8722，被占用就往上找。只监听 127.0.0.1，并且校验 Host 头，
局域网里别的机器访问不到。
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

LOG_PATH = ROOT / "data" / "logs" / "ui.log"
_WINDOWLESS = False


def setup_output() -> None:
    """把输出接住，保证在 pythonw.exe 下也能跑。

    快捷方式指向 python.exe 时，双击会带一个黑框控制台。改用 pythonw.exe
    （无窗口变体）就没窗口了 —— 但代价是 **sys.stdout 可能是 None 或者是坏的**：
    既没有控制台也没有重定向时写它会抛异常，一个 print() 就能让程序起不来。
    从 PowerShell 手动跑时看不出这个问题（输出被重定向了，句柄是好的），
    只有真从快捷方式启动才暴露 —— 所以这里主动兜住，不赌。
    """
    usable = False
    try:
        if sys.stdout is not None:
            sys.stdout.write("")
            sys.stdout.flush()
            usable = True
    except Exception:                   # noqa: BLE001
        usable = False

    log = None
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        log = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 =====\n")
    except OSError:
        log = None

    global _WINDOWLESS
    _WINDOWLESS = not usable
    if not usable:
        if log is not None:
            sys.stdout = log
            sys.stderr = log
        else:
            class _Null:
                def write(self, *_a): return 0
                def flush(self): pass
            sys.stdout = _Null()
            sys.stderr = _Null()
    elif log is not None:
        class _Tee:
            def __init__(self, *streams): self.streams = streams
            def write(self, s):
                for st in self.streams:
                    try: st.write(s)
                    except Exception: pass
                return len(s)
            def flush(self):
                for st in self.streams:
                    try: st.flush()
                    except Exception: pass
        sys.stdout = _Tee(sys.__stdout__, log)
        sys.stderr = _Tee(sys.__stderr__, log)


def fatal(title: str, message: str) -> None:
    """没有控制台时错误也必须被人看见 —— 弹原生对话框。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:                   # noqa: BLE001
        pass
    try:
        print(f"{title}: {message}")
    except Exception:                   # noqa: BLE001
        pass


import ui_webui as W  # noqa: E402

TITLE = "微信群助手 · 设置"
PREFERRED = [8722, 8723, 8724, 8725, 8730]


def pick_port() -> int:
    for p in PREFERRED:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    # 全被占了就随机要一个
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="微信群助手设置界面")
    ap.add_argument("--browser", action="store_true",
                    help="用系统浏览器打开，不开 WebView2 窗口（调界面用）")
    ap.add_argument("--port", type=int, default=0, help="指定端口")
    args = ap.parse_args()

    port = args.port or pick_port()
    try:
        httpd, url = W.serve(port, background=True)
    except OSError as exc:
        fatal(TITLE, f"起不了本地服务（端口 {port}）：{exc}\n"
                     f"可能是被别的程序占了，或防火墙拦了回环端口。")
        return 1
    print(f"设置界面后端已启动：{url}")

    if args.browser:
        webbrowser.open(url)
        print("已用默认浏览器打开。按 Ctrl+C 结束。")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return 0

    try:
        import webview  # type: ignore
    except ImportError:
        msg = ("没装 pywebview，开不了原生窗口。\n\n"
               "安装：venv\\Scripts\\python.exe -m pip install pywebview\n"
               "（requirements.txt 里已经列了，跑一次 start.cmd -Setup 也会装上）\n\n"
               "先用系统浏览器打开一份：")
        print(msg)
        # 从快捷方式启动时没有控制台，不弹框的话用户什么都看不到，
        # 只会觉得「点了没反应」。
        if _WINDOWLESS:
            fatal(TITLE, msg)
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return 0

    window = webview.create_window(
        TITLE, url,
        width=1140, height=820,
        min_size=(880, 620),
        background_color="#f3f3f3",
        text_select=True,
    )
    # 关窗时把服务一起收掉，别留后台线程
    def _shutdown():
        window.events.closed.wait()
        httpd.shutdown()

    threading.Thread(target=_shutdown, daemon=True).start()
    webview.start()          # 阻塞到窗口关闭
    return 0


if __name__ == "__main__":
    setup_output()                 # 必须在任何 print 之前
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:   # noqa: BLE001
        # 从快捷方式启动时没有控制台，异常如果只打到 stderr 就等于石沉大海。
        import traceback
        fatal(TITLE, "设置界面启动失败：\n\n"
                     + "".join(traceback.format_exception(exc))[-1500:])
        raise SystemExit(1)
