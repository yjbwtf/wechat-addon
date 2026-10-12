#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""极简 CDP 客户端：驱动一个已经用 --remote-debugging-port 打开的 Edge。

## 先读这段，否则很容易把用户浏览器搞没

**这个脚本本身不启动、也不关闭浏览器**，它只挂到已经开着调试口的 Edge 上。
浏览器得**你自己**先用 `tools\\edge_cdp.cmd` 起好（那个脚本会检查 Edge 是不是
已经在跑 —— 已经在跑的话 `--remote-debugging-port` 会被静默忽略）。

为什么强调这点：群里的 `\\agent` 曾经为了拿到调试口，直接
`Stop-Process -Name msedge -Force` 把用户正在用的浏览器杀了 —— 而那条指令里
明确写着「别把浏览器关了」。根因就是「调试口只能在启动时加」，它没有别的办法。

还有一件事让 `\\agent` 根本做不到「留着浏览器」：**agent 用 Start-Process 起的
任何进程，都会在它那条指令结束的瞬间被回收**（实测：起的 cmd 在指令结束后就
查不到了）。所以「浏览器要一直开着」必须由 agent 之外的进程来起 —— 也就是
你在电脑上跑 `tools\\edge_cdp.cmd`。

## 用法

    python tools/cdp.py tabs
    python tools/cdp.py open  <url>            # 新开一个标签页，返回 targetId
    python tools/cdp.py goto  <targetId> <url>
    python tools/cdp.py eval  <targetId> <js>  # 在页面里跑 JS，打印 JSON
    python tools/cdp.py text  <targetId>       # 打印页面可见文字（前 4000 字）
    python tools/cdp.py click <targetId> <js>  # 跑 JS 并返回结果（同 eval，语义化）
    python tools/cdp.py shot  <targetId> <png> # 截图存文件
    python tools/cdp.py frames <targetId>      # 列出 iframe 树

依赖 `websocket-client`（已在 requirements.txt 里；它是后补进去的，
之前这个包只是恰好躺在环境里，重建 venv 就会 ModuleNotFoundError）。

2026-10-12 由群里的 `\\agent` 写出，起因是它没有可用的浏览器工具
（DSH 的浏览器工具只存在于交互式客户端里，无界面的 dsh 进程根本没有 ——
见 README「浏览器工具」那节）。
"""

import json
import sys
import time
import urllib.parse
import urllib.request

import websocket

PORT = 9222
BASE = f"http://127.0.0.1:{PORT}"


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def list_tabs():
    return [t for t in _get("/json/list") if t.get("type") == "page"]


def new_tab(url):
    req = urllib.request.Request(
        BASE + "/json/new?" + urllib.parse.quote(url, safe=""), method="PUT"
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def ws_for(target_id):
    for t in list_tabs():
        if t["id"] == target_id:
            return t["webSocketDebuggerUrl"]
    raise SystemExit(f"找不到 target {target_id}")


class Session:
    def __init__(self, target_id):
        self.ws = websocket.create_connection(
            ws_for(target_id), timeout=30, suppress_origin=True
        )
        self.n = 0

    def send(self, method, **params):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise SystemExit(json.dumps(msg["error"], ensure_ascii=False))
                return msg.get("result", {})

    def eval(self, expr):
        r = self.send(
            "Runtime.evaluate",
            expression=expr,
            returnByValue=True,
            awaitPromise=True,
            userGesture=True,
        )
        if "exceptionDetails" in r:
            return {"__error__": str(r["exceptionDetails"])[:800]}
        return r.get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "tabs":
        for t in list_tabs():
            print(f"{t['id']}  {t['title'][:60]}  {t['url'][:120]}")
    elif cmd == "open":
        t = new_tab(sys.argv[2])
        print(t["id"])
    elif cmd == "goto":
        s = Session(sys.argv[2])
        s.send("Page.enable")
        s.send("Page.navigate", url=sys.argv[3])
        time.sleep(0.5)
        print("ok")
        s.close()
    elif cmd in ("eval", "click"):
        s = Session(sys.argv[2])
        print(json.dumps(s.eval(sys.argv[3]), ensure_ascii=False, default=str))
        s.close()
    elif cmd == "text":
        s = Session(sys.argv[2])
        v = s.eval("document.body ? document.body.innerText.slice(0,4000) : '(no body)'")
        print(v)
        s.close()
    elif cmd == "shot":
        import base64

        s = Session(sys.argv[2])
        r = s.send("Page.captureScreenshot", format="png")
        data = base64.b64decode(r["data"])
        out = sys.argv[3] if len(sys.argv) > 3 else "shot.png"
        with open(out, "wb") as f:
            f.write(data)
        print(f"{out}  {len(data)} bytes")
        s.close()
    elif cmd == "frames":
        s = Session(sys.argv[2])
        def walk(node, depth=0):
            fr = node["frame"]
            print("  " * depth + fr["id"] + "  " + fr.get("url", "")[:140])
            for c in node.get("childFrames", []) or []:
                walk(c, depth + 1)

        walk(s.send("Page.getFrameTree")["frameTree"])
        s.close()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
