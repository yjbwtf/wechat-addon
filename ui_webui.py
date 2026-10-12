"""网页版设置界面的后端。

## 设计原则：不重写已经对的东西

`ui_bridge.py` 里已经有一整套经过验证的读写逻辑：
  - ruamel 往返读写，**保住 config.yaml / persona.yaml 里的中文注释**
  - 写入前备份、写坏了整体回滚
  - 写完立刻用 `load_config` / `load_persona` 验证，不合法就回滚

网页端**直接复用 `cmd_apply`**（把改动写成它认识的文本格式再调它），
而不是另起一套写文件的代码。新界面只是换了个壳，落盘那条路一个字没动。

## 和 C 版的关系

字段元数据来自 `ui/schema.json`（由 tools/extract_ui_schema.py 从旧的 ui.c 提取，
已核对过每页字段数一致）。所以这次重写不涉及任何"重新定义配置项"，
只是换个渲染方式。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "data" / "logs"
sys.path.insert(0, str(ROOT))

import ui_bridge as B  # noqa: E402
from src.procutil import (bot_interpreter, launch_bot, no_window,  # noqa: E402
                          pid_alive)

SCHEMA_PATH = ROOT / "ui" / "schema.json"
IN_PATH = ROOT / "data" / "_ui_in.txt"


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _kind_map() -> dict[str, str]:
    """key -> 类型。类型来自实际解析出来的 YAML 值，比 schema 里写死的更可信。"""
    out: dict[str, str] = {}
    for prefix, path in B.FILES.items():
        try:
            doc = B.load_doc(path)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(doc, dict):
            continue
        for key, kind, _ in B._flatten(prefix, doc):
            out[key] = kind
    return out


def current_values() -> dict[str, str]:
    """当前所有值（不含状态项），key -> 字符串形式。"""
    out: dict[str, str] = {}
    for prefix, path in B.FILES.items():
        try:
            doc = B.load_doc(path)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(doc, dict):
            continue
        for key, _kind, value in B._flatten(prefix, doc):
            out[key] = value
    return out


def state() -> dict:
    """一次把所有东西给前端：结构 + 当前值 + 运行状态。"""
    schema = load_schema()
    values = current_values()
    kinds = _kind_map()

    # status_lines() 返回的是 (key, kind, value) —— 没有中文标签。
    # 标签在 schema 的 stats 里（原来 C 界面那张 g_stats 表）。
    # 另外 bridge 报的布尔是 "1"/"0"，这里换成给人看的文字。
    labels = {s["key"]: s["label"] for s in schema.get("stats", [])}
    # bridge 报的布尔是 "1"/"0"，换成给人看的文字。逐个 key 明确映射，
    # 别用一个「1 就是运行中」的通式糊过去 —— @status.stopped 是 1 时
    # 表示急停标记「存在」，不是「运行中」。
    BOOL_TEXT = {
        "@status.running": ("运行中", "未运行"),
        "@status.stopped": ("已触发", "无"),
    }
    status = []
    for key, _kind, value in B.status_lines():
        label = labels.get(key)
        if not label:
            continue
        if key in BOOL_TEXT:
            value = BOOL_TEXT[key][0 if value == "1" else 1]
        if key == "@status.pid" and not value:
            value = "—"
        status.append({"key": key, "label": label, "value": value})

    running = any(s["key"] == "@status.running" and s["value"] == "运行中"
                  for s in status)
    stopped = any(s["key"] == "@status.stopped" and s["value"] != "无"
                  for s in status)
    return {
        "schema": schema,
        "values": values,
        "kinds": kinds,
        "status": status,
        "running": running,
        "stopped": stopped,
        "root": str(ROOT),
        "files": {k: str(v) for k, v in B.FILES.items()},
    }


def save(changes: dict[str, str]) -> dict:
    """保存改动。

    复用 ui_bridge.cmd_apply —— 它负责备份、ruamel 往返、校验、回滚。
    这里只把 JSON 翻译成它认识的 `key\tkind\tvalue` 文本格式。
    """
    changes = {k: v for k, v in (changes or {}).items()
               if not k.startswith("@") and ":" in k}
    if not changes:
        return {"ok": False, "message": "没有改动"}

    kinds = _kind_map()
    schema_kinds = {f["key"]: f["kind"] for f in load_schema()["fields"]}
    lines = []
    for key, raw in changes.items():
        kind = kinds.get(key) or schema_kinds.get(key) or "str"
        # schema 里的 kind 是界面语义（bool/choice/int/...），
        # 而 apply 需要的是 YAML 语义（bool/int/float/str/list/text）。
        # 两者大多同名，只有 choice 例外 —— 它落盘是字符串。
        if kind == "choice":
            kind = "str"
        lines.append(f"{key}\t{kind}\t{B.esc(str(raw))}")

    IN_PATH.parent.mkdir(parents=True, exist_ok=True)
    IN_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = B.cmd_apply([str(IN_PATH)])
    out = buf.getvalue().strip()

    if code == 0:
        return {"ok": True, "message": "已保存", "detail": out}
    detail = out.replace("ERROR\t", "").strip()
    return {"ok": False, "message": "保存失败，配置未被改动（已回滚）",
            "detail": detail}


# ------------------------------------------------------------------ 运行控制
def _pid_path() -> Path:
    return ROOT / "data" / "bot.pid"


def bot_running() -> tuple[bool, int]:
    try:
        pid = int(_pid_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False, 0
    # **不能用 pid_alive(pid)** —— Windows 上它是 TerminateProcess，
    # 等于「检查机器人有没有在跑」的同时把它杀掉。
    # 结果就是：每打开/刷新一次设置界面，机器人就没了。
    # （原来这里的注释还写着「Windows 上也能用来探活」，是错的。）
    if not pid_alive(pid):
        return False, 0
    return True, pid


def bot_start() -> dict:
    running, pid = bot_running()
    if running:
        return {"ok": False, "message": f"已经在跑了（pid {pid}）"}
    if bot_interpreter(ROOT) is None:
        return {"ok": False, "message": "虚拟环境还没建好，先跑一次 start.cmd -Setup"}

    # start.cmd 在正常启动路径上会清 STOP；这里绕过它，就得自己清。
    # 留着 STOP 的话机器人起来了也不会回话，看起来像"启动失败"。
    (ROOT / "data" / "STOP").unlink(missing_ok=True)

    # **别用 cmd.exe /c start。** 原来这里是 `["cmd.exe","/c","start","",bat]`
    # 配 CREATE_NEW_CONSOLE，本意是"起个窗口好看日志"。但机器人已经改用
    # pythonw 起了（无输出），那个窗口就永远是**空的**，还一直杵到机器人退出。
    # 日志在设置界面的日志区里，不需要靠控制台。
    pid, err = launch_bot(ROOT)
    if not pid:
        return {"ok": False, "message": err}

    # 等 bot.pid 出现。它由机器人自己写，是"真的起来了"的唯一可靠证据 ——
    # 进程存在不代表初始化成功（比如被单例守卫挡回去的那种）。
    for _ in range(24):
        time.sleep(0.5)
        running, got = bot_running()
        if running:
            return {"ok": True, "message": f"已启动（pid {got}）"}
    return {"ok": False,
            "message": "启动超时，去看日志区（logs/bot.log 和 bot-console.log）"}


def bot_stop() -> dict:
    # 同样别让 cmd.exe 弹窗。stop.py 直接用当前解释器跑；
    # 万一只有 stop.cmd（旧版），才退回到 cmd.exe，并且不给它窗口。
    stop_py = ROOT / "stop.py"
    if stop_py.is_file():
        argv = [sys.executable, str(stop_py)]
        flags = no_window()
    else:
        stop_cmd = ROOT / "stop.cmd"
        if not stop_cmd.is_file():
            return {"ok": False, "message": "找不到 stop.py / stop.cmd"}
        argv = ["cmd.exe", "/c", str(stop_cmd)]
        flags = no_window()
    try:
        subprocess.run(argv, cwd=str(ROOT), timeout=60, creationflags=flags)
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "停止超时（60 秒）"}
    except OSError as exc:
        return {"ok": False, "message": f"调用停止脚本失败：{exc}"}
    time.sleep(0.8)
    running, pid = bot_running()
    return {"ok": not running,
            "message": "已停止" if not running else f"还有进程在跑（pid {pid}）"}


def log_tail(lines: int = 200) -> str:
    """日志区的内容：结构化日志 + 被隐藏的控制台输出。

    机器人现在用 pythonw 起（无窗口），它的 stdout/stderr 被重定向到
    data/logs/bot-console.log —— wxauto4 的横幅、崩溃栈都在那里。
    隐藏了窗口却看不到那些输出，排查就没法做了，所以这里一并带出来。

    加个分隔标题是为了让「这段不是 logging 写的」一目了然。
    """
    out = []
    for name, title in (("bot.log", "运行日志"),
                        ("bot-console.log", "控制台输出（进程的输出，含崩溃栈）")):
        f = LOG_DIR / name
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        tail = "\\n".join(text.splitlines()[-lines:]) if text else ""
        if not tail.strip():
            continue
        out.append(f"===== {title}（{name}）=====\\n{tail}")
    return "\\n\\n".join(out) if out else "（还没有日志）"

def open_path(which: str) -> dict:
    targets = {
        "config": B.CONFIG_PATH,
        "persona": B.PERSONA_PATH,
        "memes": ROOT / "memes.yaml",
        "log": ROOT / "data" / "logs" / "bot.log",
        "dir": ROOT,
        "schema": SCHEMA_PATH,
    }
    p = targets.get(which)
    if not p or not Path(p).exists():
        return {"ok": False, "message": f"找不到 {which}"}
    try:
        os.startfile(str(p))     # noqa: S606
        return {"ok": True, "message": f"已用默认程序打开 {Path(p).name}"}
    except OSError as exc:
        return {"ok": False, "message": str(exc)}


# ------------------------------------------------------------------ HTTP 服务
#
# 为什么用本地 HTTP 而不是 pywebview 的 JS 桥：
#   1. 同一个页面既能在 WebView2 窗口里跑，也能直接拿浏览器打开调试 ——
#      调界面的效率差很多；
#   2. 静态文件、JSON API、日志流全都走一套路由，不用为桥写第二套代码；
#   3. 标准库自带，零依赖。
# 只监听 127.0.0.1，并且校验 Host，避免被局域网里别的机器访问到。

import json as _json          # noqa: E402
import threading              # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

WEB_DIR = ROOT / "ui" / "web"
_MIME = {".html": "text/html; charset=utf-8",
         ".css": "text/css; charset=utf-8",
         ".js": "application/javascript; charset=utf-8",
         ".json": "application/json; charset=utf-8",
         ".svg": "image/svg+xml",
         ".png": "image/png"}


class Handler(BaseHTTPRequestHandler):
    server_version = "wechat-addon-ui"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):   # 静音，别把控制台刷满
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, _json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _local(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost", "[::1]")

    def do_GET(self):                                   # noqa: N802
        if not self._local():
            self._json({"error": "forbidden"}, 403)
            return
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            path = "/index.html"
        if path.startswith("/api/"):
            if path == "/api/state":
                self._json(state())
            elif path == "/api/log":
                self._json({"text": log_tail()})
            else:
                self._json({"error": "unknown"}, 404)
            return
        target = (WEB_DIR / path.lstrip("/")).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        self._send(200, target.read_bytes(),
                   _MIME.get(target.suffix, "application/octet-stream"))

    def do_POST(self):                                  # noqa: N802
        if not self._local():
            self._json({"error": "forbidden"}, 403)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        try:
            payload = _json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, OSError):
            payload = {}
        path = self.path.split("?")[0]
        if path == "/api/save":
            self._json(save(payload.get("changes") or {}))
        elif path == "/api/bot":
            act = payload.get("action")
            if act == "start":
                self._json(bot_start())
            elif act == "stop":
                self._json(bot_stop())
            else:
                self._json({"ok": False, "message": "未知动作"})
        elif path == "/api/open":
            self._json(open_path(str(payload.get("which") or "")))
        else:
            self._json({"error": "unknown"}, 404)


def serve(port: int = 8722, background: bool = True):
    """起服务。返回 (server, url)。"""
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    if background:
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
    return httpd, url
