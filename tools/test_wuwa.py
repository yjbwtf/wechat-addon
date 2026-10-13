"""鸣潮查询的测试：MCP 客户端、缓存、格式化、查询入口。

## 为什么用「假 MCP 服务」

`src/mcp_stdio.py` 是自己写的 JSON-RPC 客户端，出错方式很多（超时、进程自己退了、
服务端返回 error、结果太长被截断…），而这些**用真服务很难复现**。
所以这里起一个**用 Python 写的假 MCP 服务**，让它按剧本回话 ——
不依赖 node、不依赖网络、失败模式可控。

（真服务那边另有一个一次性验证：`data/_test_mcp.py` 打过真的
wuthering-waves-platform，验过 initialize / tools/list / waves_capabilities。）
"""

from __future__ import annotations


import sys
import tempfile
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.mcp_stdio import McpError, McpStdio, resolve_command  # noqa: E402
from src import wuwa  # noqa: E402

cases: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    cases.append((name, ok, detail))
    print("  %s  %s%s" % ("[OK]" if ok else "[!!]", name,
                          ("   " + detail) if detail and not ok else ""))


TMP = Path(tempfile.mkdtemp(prefix="wuwa_test_"))

# ---------------------------------------------------------------- 假服务
FAKE = TMP / "fake_mcp.py"
FAKE.write_text(textwrap.dedent('''
    """按剧本回话的假 MCP 服务。剧本用 --mode 选。"""
    import json, sys, time

    mode = "ok"
    for i, a in enumerate(sys.argv):
        if a == "--mode":
            mode = sys.argv[i + 1]

    def send(obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        m, mid = msg.get("method"), msg.get("id")
        if m == "initialize":
            if mode == "oldversion":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "fake", "version": "1"}}})
            else:
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "protocolVersion": "2025-06-18",
                    "serverInfo": {"name": "fake", "version": "1"}}})
        elif m == "notifications/initialized":
            pass
        elif m == "tools/list":
            if mode == "paged" and not (msg.get("params") or {}).get("cursor"):
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "tools": [{"name": "t1"}], "nextCursor": "c2"}})
            else:
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "tools": [{"name": "t2"}], "nextCursor": None}})
        elif m == "tools/call":
            name = (msg.get("params") or {}).get("name")
            if mode == "hang":
                time.sleep(60)
                continue
            if mode == "die":
                sys.exit(3)
            if mode == "rpc_error":
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32601, "message": "没有这个方法"}})
                continue
            if name == "errtool":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": "CREDENTIAL_ERROR: 没登录"}]}})
                continue
            if name == "onlytext":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": json.dumps({"a": 1})}]}})
                continue
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "structuredContent": {"echo": name, "args": msg.get("params")},
                "content": [{"type": "text", "text": "{}"}]}})
        else:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": "unknown"}})
'''), encoding="utf-8")


def fake(mode: str = "ok", timeout: float = 10) -> McpStdio:
    return McpStdio([sys.executable, str(FAKE), "--mode", mode], timeout=timeout)


print("1) MCP 客户端")
print()
with fake() as c:
    info = c.initialize()
    check("initialize 拿到服务端信息", info.get("serverInfo", {}).get("name") == "fake",
          str(info))

with fake() as c:
    c.initialize()
    tools = c.list_tools()
    check("tools/list 返回工具", [t["name"] for t in tools] == ["t2"], str(tools))

with fake("paged") as c:
    c.initialize()
    check("**tools/list 会跟着 nextCursor 翻页**",
          [t["name"] for t in c.list_tools()] == ["t1", "t2"],
          str([t["name"] for t in c.list_tools()]))

with fake() as c:
    c.initialize()
    got = c.call_tool("mytool", {"x": 1})
    check("call_tool 用 structuredContent",
          got.get("echo") == "mytool" and got["args"]["arguments"] == {"x": 1},
          str(got))

with fake() as c:
    c.initialize()
    got = c.call_tool("onlytext", {})
    check("没有 structuredContent 时解析 content 里的 JSON",
          got == {"a": 1}, str(got))

with fake() as c:
    c.initialize()
    try:
        c.call_tool("errtool", {})
        check("isError 要抛 McpError", False, "没抛")
    except McpError as exc:
        check("isError 要抛 McpError，且带上服务端的说明",
              "CREDENTIAL_ERROR" in str(exc), str(exc))

with fake("rpc_error") as c:
    c.initialize()
    try:
        c.call_tool("x", {})
        check("JSON-RPC error 要抛 McpError", False, "没抛")
    except McpError as exc:
        check("JSON-RPC error 要抛 McpError", "没有这个方法" in str(exc), str(exc))

with fake("oldversion") as c:
    try:
        c.initialize()
        check("协议版本不一致时只警告、不报错", True)
    except McpError as exc:
        check("协议版本不一致时只警告、不报错", False, str(exc))

t0 = time.time()
with fake("hang", timeout=2) as c:
    c.initialize()
    try:
        c.call_tool("x", {})
        check("**服务端卡住时会超时**（不能把机器人一起卡死）", False, "没超时")
    except McpError as exc:
        check("**服务端卡住时会超时**", "超时" in str(exc), str(exc))
        check("超时耗时应接近设定值（2 秒）", time.time() - t0 < 6,
              "%.1fs" % (time.time() - t0))

with fake("die") as c:
    c.initialize()
    try:
        c.call_tool("x", {})
        check("服务进程死掉时报错，不是一直等", False, "没报错")
    except McpError as exc:
        check("服务进程死掉时报错，不是一直等",
              "退出" in str(exc) or "输出流" in str(exc), str(exc))

check("起不来的命令要报 McpError（不是 FileNotFoundError）",
      isinstance(resolve_command(["definitely-not-a-real-cmd-xyz"]),
                 list))

resolved = resolve_command(["npx"])
check("Windows 上 npx 会被解析成真实路径（否则 Popen 找不到 .cmd）",
      resolved[0].lower().endswith(("npx.cmd", "npx.exe", "npx")) and
      Path(resolved[0]).exists(), str(resolved))

print()
print("2) 结果缓存")
print()
cp = TMP / "cache.json"
cache = wuwa.Cache(cp)
check("没写过 -> None", cache.get("k", 60) is None)
cache.put("k", "值")
check("写过就能取到", cache.get("k", 60) == "值")
check("落盘了", cp.exists() and "值" in cp.read_text(encoding="utf-8"))
c2 = wuwa.Cache(cp)
c2.load()
check("重新加载还在（跨重启）", c2.get("k", 60) == "值")
check("ttl 过期就取不到", c2.get("k", -1) is None)

print()
print("3) 格式化")
print()
BUILD = {
    "account": {"label": "测试账号", "serverName": "国服"},
    "character": {"roleName": "维里奈", "level": 90, "starLevel": 5},
    "build": {
        "role": {"name": "维里奈", "level": 90, "starLevel": 5,
                 "attributeName": "衍射"},
        "chain": {"unlocked": 2, "total": 6},
        "weapon": {"name": "千古洑流", "level": 90, "starLevel": 5,
                   "resonanceLevel": 1},
        "skills": [{"name": "普攻", "level": 8}, {"name": "共鸣技能", "level": 6}],
        "echoes": [{"name": "呜咔咔", "cost": 4,
                    "mainProps": [{"attributeName": "暴击率"}]}],
        "echoScore": {"available": True, "totalScore": 210.5,
                      "averageScore": 42.1, "rank": "S"},
        "recommendations": {"lowSkillCount": 1,
                            "lowSkills": [{"name": "共鸣技能", "level": 6}],
                            "missingEchoSlots": 0},
    },
}
text = wuwa.format_build(BUILD)
check("单个角色：带名字", "维里奈" in text, text)
check("单个角色：带等级/星级/属性",
      "等级 90" in text and "5★" in text and "衍射" in text, text)
check("单个角色：带共鸣链", "共鸣链 2/6" in text, text)
check("单个角色：带武器和精炼", "千古洑流" in text and "精1" in text, text)
check("单个角色：带声骸评分", "210.5" in text and "S" in text, text)
check("单个角色：低技能等级会点出来", "共鸣技能" in text, text)

ALL = {
    "account": {"label": "测试账号"},
    "characters": [
        {"ok": True, "character": {"roleName": "维里奈", "level": 90,
                                   "starLevel": 5},
         "build": {"weapon": {"name": "千古洑流", "resonanceLevel": 1},
                   "chain": {"unlocked": 2, "total": 6},
                   "echoScore": {"available": True, "totalScore": 210,
                                 "averageScore": 42, "rank": "S"}}},
        {"ok": False, "character": {"roleName": "坏数据"}, "error": "接口炸了"},
    ],
    "summary": {"total": 2, "averageScore": 42.0},
}
text2 = wuwa.format_all(ALL)
check("全角色：带数量", "共 2 个角色" in text2, text2)
check("全角色：一行一个角色", "维里奈" in text2, text2)
check("全角色：取数据失败的角色也要写出来（不能静默少一个）",
      "坏数据" in text2 and "接口炸了" in text2, text2)
check("全角色：带汇总", "平均声骸分 42" in text2, text2)

long_all = {"account": {}, "characters": [
    {"ok": True, "character": {"roleName": "角色%02d" % i, "level": 90},
     "build": {}} for i in range(200)]}
text3 = wuwa.format_all(long_all, max_chars=300)
check("**太长会截断**（群里发一大坨没人看）",
      len(text3) < 900 and "还有" in text3, "长度 %d" % len(text3))

print()
print("4) 查询入口")
print()
CFG = wuwa_config = None
from dataclasses import dataclass, field as _f  # noqa: E402


@dataclass
class Cfg:
    enabled: bool = True
    timeout_seconds: int = 5
    cache_seconds: int = 60
    default_account: str = ""
    max_chars: int = 1200
    command: list = _f(default_factory=lambda: ["x"])


real_call = wuwa.call_tool
calls: list = []


def stub_ok(cfg, tool, args=None, timeout=0):
    calls.append((tool, dict(args or {})))
    if tool == "waves_character_build":
        return BUILD
    return ALL


wuwa.call_tool = stub_ok
try:
    c3 = wuwa.Cache(TMP / "c3.json")
    ok, text = wuwa.query(Cfg(), c3, "维里奈")
    check("有角色名 -> 调 waves_character_build",
          calls and calls[0][0] == "waves_character_build", str(calls))
    check("角色名传给了 selector", calls[0][1].get("selector") == "维里奈",
          str(calls))
    check("返回单个角色的面板", ok and "维里奈" in text, text[:60])

    calls.clear()
    ok, text = wuwa.query(Cfg(), c3, "")
    check("没角色名 -> 调 waves_all_character_builds",
          calls and calls[0][0] == "waves_all_character_builds", str(calls))
    check("返回全角色汇总", ok and "全角色" in text, text[:60])

    calls.clear()
    ok, _ = wuwa.query(Cfg(), c3, "维里奈")
    check("**第二次命中缓存，不再调用**", not calls, str(calls))

    c4 = wuwa.Cache(TMP / "c4.json")
    calls.clear()
    ok, _ = wuwa.query(Cfg(default_account="acc-9"), c4, "维里奈")
    check("配了 default_account 就带上 accountId",
          calls[0][1].get("accountId") == "acc-9", str(calls))

    def stub_cred(cfg, tool, args=None, timeout=0):
        raise McpError('{"code": "CREDENTIAL_ERROR", "message": "没有找到账号凭据"}')

    wuwa.call_tool = stub_cred
    c5 = wuwa.Cache(TMP / "c5.json")
    ok, text = wuwa.query(Cfg(), c5, "")
    check("**没登录时给的是「怎么登录」的提示，不是 JSON 报错**",
          (not ok) and "wuthering-waves-platform login" in text, text[:80])

    def stub_other(cfg, tool, args=None, timeout=0):
        raise McpError("ECONNREFUSED 连不上")

    wuwa.call_tool = stub_other
    c6 = wuwa.Cache(TMP / "c6.json")
    ok, text = wuwa.query(Cfg(), c6, "")
    check("其它错误照实回报", (not ok) and "ECONNREFUSED" in text, text[:80])

    wuwa.call_tool = lambda *a, **k: "不是字典"
    c7 = wuwa.Cache(TMP / "c7.json")
    ok, text = wuwa.query(Cfg(), c7, "")
    check("返回看不懂的东西时不崩", not ok and "看不懂" in text, text[:80])

    wuwa.call_tool = real_call
    ok, text = wuwa.query(Cfg(enabled=False), None, "")
    check("关了就不查", not ok and "没开" in text, text[:60])
finally:
    wuwa.call_tool = real_call

failed = [c for c in cases if not c[1]]
print()
if failed:
    print("不通过 %d / %d" % (len(failed), len(cases)))
    raise SystemExit(1)
print("全部通过（%d 项）" % len(cases))
