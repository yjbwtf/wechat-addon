"""极简 MCP stdio 客户端 —— 跟一个本地 MCP 服务进程说 JSON-RPC。

## 为什么自己写一个

`wuthering-waves-platform` 目前**只提供 MCP stdio**（README 里写「CLI 和 HTTP
适配器会复用同一套核心服务」，但那两个还没做）。而机器人需要一条**确定性的、
不过 LLM** 的调用路径 —— 跟查 Enka 展柜是同一个思路：确定的事不要过模型。

## MCP stdio 的线格式

**按行分隔的 JSON**：一行一条消息，消息里不能有多余换行。握手顺序是：

    -> initialize                     （带 protocolVersion / capabilities / clientInfo）
    <- result                          （服务端回它支持的版本）
    -> notifications/initialized       （通知：没有 id，不期待回复）
    -> tools/call                      （name + arguments）
    <- result

## 实现上要注意的

  · **读必须用线程**。Windows 上没法对管道做 select，所以起一个读线程把行
    塞进队列，主线程按 deadline 等 —— 否则服务端卡住时我们也会一起卡死。
  · **stderr 必须一起收走**。MCP 服务通常把日志写到 stderr；管道不读满就
    会堵住子进程。顺便这些日志是排查问题的唯一线索。
  · **用 no_window() 起进程**。机器人的父进程没有控制台，直接 spawn 一个
    node 会**弹出一个黑窗口**（这个项目踩过，见 src/procutil.py）。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from typing import Any

from .procutil import no_window

log = logging.getLogger("wechat-bot.mcp")

#: 我们支持的协议版本。服务端会回它自己的版本，不一致时只警告不报错 ——
#: 这个客户端只用到 initialize / tools/list / tools/call 三个方法，
#: 这几个在历次修订里没变过。
PROTOCOL_VERSION = "2025-06-18"

CLIENT_INFO = {"name": "wechat-addon", "version": "0.1"}


class McpError(RuntimeError):
    """MCP 层面的失败（进程起不来、超时、服务端返回 error）。"""


def resolve_command(cmd: list[str]) -> list[str]:
    """把命令列表里的第一个词解析成真实路径。

    Windows 上必须这样：`subprocess` 传列表时走 CreateProcess，
    它**不会**按 PATHEXT 找 `.cmd` —— 所以 `npx` 得先解析成 `npx.cmd`，
    否则报 FileNotFoundError（明明命令行里能用）。
    """
    if not cmd:
        return cmd
    found = shutil.which(cmd[0])
    if found:
        return [found] + list(cmd[1:])
    return cmd


class McpStdio:
    """一个 MCP 服务进程。用法：

        with McpStdio(["npx", "-y", "--package", "x", "x-mcp"]) as c:
            c.initialize()
            c.call_tool("some_tool", {"a": 1})
    """

    def __init__(self, cmd: list[str], *, cwd: str | os.PathLike | None = None,
                 env: dict[str, str] | None = None,
                 timeout: float = 60.0) -> None:
        self.cmd = resolve_command(list(cmd))
        self.cwd = str(cwd) if cwd else None
        self.env = env
        self.timeout = timeout
        self.proc: subprocess.Popen | None = None
        self._q: queue.Queue[str] = queue.Queue()
        self._err: list[str] = []
        self._id = 0
        self._lock = threading.Lock()
        self.server_info: dict[str, Any] = {}
        self.stderr_tail = ""

    # ------------------------------------------------------------ 生命周期
    def start(self) -> "McpStdio":
        if self.proc is not None:
            return self
        log.debug("起 MCP 服务：%s", self.cmd)
        try:
            self.proc = subprocess.Popen(
                self.cmd, cwd=self.cwd, env=self.env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, creationflags=no_window(),
            )
        except OSError as exc:
            raise McpError("起不来：%s（%s）" % (" ".join(self.cmd), exc)) from exc
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        return self

    def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._q.put(line)
        self._q.put("")          # 关流的哨兵

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self._err.append(line)
                del self._err[:-40]          # 只留最近 40 行
        self.stderr_tail = "\n".join(self._err[-8:])

    def close(self) -> None:
        p, self.proc = self.proc, None
        if p is None:
            return
        try:
            if p.stdin:
                p.stdin.close()
        except OSError:
            pass
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
        self.stderr_tail = "\n".join(self._err[-8:])

    def __enter__(self) -> "McpStdio":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ 收发
    def _send(self, msg: dict) -> None:
        if not self.proc or not self.proc.stdin:
            raise McpError("MCP 进程没起")
        data = json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n"
        with self._lock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise McpError("写 MCP 进程失败：%s" % exc) from exc

    def _await(self, want_id: int, deadline: float) -> dict:
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise McpError("等 MCP 回复超时（%.0fs）%s"
                               % (self.timeout,
                                  ("；服务端最后几行：" + self.stderr_tail)
                                  if self.stderr_tail else ""))
            try:
                line = self._q.get(timeout=min(left, 1.0))
            except queue.Empty:
                # 进程死了就别等了
                if self.proc and self.proc.poll() is not None:
                    raise McpError("MCP 进程退出了（码 %s）%s"
                                   % (self.proc.returncode,
                                      ("；最后几行：" + self.stderr_tail)
                                      if self.stderr_tail else ""))
                continue
            if line == "":
                raise McpError("MCP 进程关了输出流%s"
                               % (("；最后几行：" + self.stderr_tail)
                                  if self.stderr_tail else ""))
            try:
                msg = json.loads(line)
            except ValueError:
                log.debug("MCP 非 JSON 输出：%s", line[:200])
                continue
            if msg.get("id") == want_id:
                if "error" in msg:
                    err = msg["error"]
                    raise McpError("%s: %s" % (err.get("code"),
                                               err.get("message")))
                return msg.get("result") or {}
            # 别的 id / 通知，丢掉（我们只做同步调用）

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        mid = self._id
        self._send({"jsonrpc": "2.0", "id": mid, "method": method,
                    "params": params or {}})
        return self._await(mid, time.time() + self.timeout)

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # ------------------------------------------------------------ MCP 动作
    def initialize(self) -> dict:
        res = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        })
        self.server_info = res.get("serverInfo") or {}
        got = res.get("protocolVersion")
        if got and got != PROTOCOL_VERSION:
            log.info("MCP 协议版本不一致（我们 %s，服务端 %s）—— 继续用",
                     PROTOCOL_VERSION, got)
        self.notify("notifications/initialized")
        return res

    def list_tools(self) -> list[dict]:
        out: list[dict] = []
        cursor = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            res = self.request("tools/list", params)
            out.extend(res.get("tools") or [])
            cursor = res.get("nextCursor")
            if not cursor:
                return out

    def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        """调一个工具。返回**结构化结果**（没有就解析 text 里的 JSON）。

        服务端把结果放在 structuredContent 里，同时把 JSON 文本放进 content[0].text；
        两者都在时优先用结构化的。出错（isError）时抛 McpError，
        错误信息里带上服务端给的说明 —— 不然只剩一句「调用失败」没法查。
        """
        res = self.request("tools/call", {"name": name,
                                          "arguments": arguments or {}})
        if res.get("isError"):
            raise McpError(self._text_of(res) or "工具返回了错误")
        if "structuredContent" in res:
            return res["structuredContent"]
        text = self._text_of(res)
        if text:
            try:
                return json.loads(text)
            except ValueError:
                return text
        return None

    @staticmethod
    def _text_of(res: dict) -> str:
        parts = []
        for c in res.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(str(c.get("text") or ""))
        return "\n".join(parts)
