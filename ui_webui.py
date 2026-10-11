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
sys.path.insert(0, str(ROOT))

import ui_bridge as B  # noqa: E402
from src.procutil import pid_alive

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
    bat = ROOT / "start.cmd"
    if not bat.is_file():
        return {"ok": False, "message": f"找不到 {bat}"}
    # 用 start 起一个独立控制台窗口，日志能直接看
    subprocess.Popen(["cmd.exe", "/c", "start", "", str(bat)], cwd=str(ROOT),
                     creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    for _ in range(24):
        time.sleep(0.5)
        running, pid = bot_running()
        if running:
            return {"ok": True, "message": f"已启动（pid {pid}）"}
    return {"ok": False, "message": "启动超时，去看新开的控制台窗口或 logs/bot.log"}


def bot_stop() -> dict:
    for name in ("stop.cmd", "stop.py"):
        p = ROOT / name
        if p.is_file():
            subprocess.run(["cmd.exe", "/c", str(p)], cwd=str(ROOT), timeout=60)
            time.sleep(0.8)
            running, pid = bot_running()
            return {"ok": not running,
                    "message": "已停止" if not running else f"还有进程在跑（pid {pid}）"}
    return {"ok": False, "message": "找不到 stop.cmd / stop.py"}


def log_tail(lines: int = 400) -> str:
    p = ROOT / "data" / "logs" / "bot.log"
    if not p.is_file():
        return "(还没有日志文件)"
    try:
        text = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"(读不到日志：{exc})"
    return "\n".join(text[-lines:])


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
