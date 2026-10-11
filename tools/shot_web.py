"""给 WebView2 窗口截图，用来验证网页界面真的渲染出来了。

用 PrintWindow 而不是抓屏幕：滚动/动画中的窗口抓屏幕容易抓到重绘中间态。
"""
import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "ui_shots"
OUT.mkdir(parents=True, exist_ok=True)

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
user32.SetProcessDPIAware()


def find(title_part="设置", timeout=40.0):
    deadline = time.time() + timeout
    found = None

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        nonlocal found
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if title_part in buf.value:
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            if r.right - r.left > 400:
                found = (hwnd, buf.value)
                return False
        return True

    while time.time() < deadline:
        user32.EnumWindows(cb, 0)
        if found:
            return found
        time.sleep(0.5)
    return None


def shot(hwnd, name):
    """抓窗口所在的屏幕区域。

    注意不能用 PrintWindow：WebView2 的内容走 DirectComposition 合成，
    PrintWindow 拿到的是一张空白表面 —— 这个坑很费时间，会让人以为页面没渲染。
    静态页面上抓屏没有中间态问题，把窗口置前再抓即可。
    """
    from PIL import ImageGrab

    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    time.sleep(1.3)                    # 等合成器出新帧
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    img = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), all_screens=True)
    p = OUT / f"web_{name}.png"
    img.save(p)
    print(f"  {name:10s} {img.width}x{img.height}  {p.name}")
    return p



proc = subprocess.Popen([str(ROOT / "venv" / "Scripts" / "python.exe"),
                         str(ROOT / "ui_app.py")], cwd=str(ROOT))
try:
    got = find()
    if not got:
        print("没找到窗口")
        raise SystemExit(1)
    hwnd, title = got
    print(f"窗口: {title!r} hwnd={hwnd}")
    time.sleep(4)                      # 等页面加载完
    shot(hwnd, "状态")
    # 逐个页签点一下（导航项在左侧，步长约 34px，从 y≈150 开始）
    names = ["群聊", "连续消息", "频率与安全", "人设与语气", "权限", "高级"]
    for i, nm in enumerate(names):
        y = 150 + i * 34
        lp = (y << 16) | 120
        user32.SetForegroundWindow(hwnd)
        user32.PostMessageW(hwnd, 0x0201, 1, lp)
        user32.PostMessageW(hwnd, 0x0202, 0, lp)
        time.sleep(1.2)
        shot(hwnd, nm)
finally:
    time.sleep(0.5)
    proc.terminate()
