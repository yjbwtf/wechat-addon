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


import json
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

print()
print("5) 多账号")
print()
ACCOUNTS = {"items": [
    {"id": "acc-main", "label": "主号", "userId": "u1"},
    {"id": "acc-alt", "label": "小号", "userId": "u2"},
]}

multi_calls: list = []


def stub_multi(cfg, tool, args=None, timeout=0):
    multi_calls.append((tool, dict(args or {})))
    if tool == "waves_account_list":
        return ACCOUNTS
    if tool == "waves_account_set_active":
        return {"id": (args or {}).get("accountId"), "label": "小号"}
    if tool == "waves_character_build":
        d = json.loads(json.dumps(BUILD))
        d["account"] = {"label": "账号-%s" % (args or {}).get("accountId")}
        return d
    return ALL


wuwa.call_tool = stub_multi
try:
    print("  参数解析")
    for text, want in (("", ("query", "", None)),
                       ("维里奈", ("query", "维里奈", None)),
                       ("账号", ("list", "", None)),
                       ("list", ("list", "", None)),
                       ("切换 2", ("switch", "", 2)),
                       ("@2", ("query", "", 2)),
                       ("@2 维里奈", ("query", "维里奈", 2)),
                       ("@ 3 今汐", ("query", "今汐", 3))):
        got = wuwa.parse_request(text)
        check("%-12r -> %s" % (text, want), got == want, "得到 %s" % (got,))
    check("「切换」没给序号时不乱切", wuwa.parse_request("切换")[2] is None)

    print()
    print("  账号列表")
    c8 = wuwa.Cache(TMP / "c8.json")
    multi_calls.clear()
    items = wuwa.list_accounts(Cfg(), c8)
    check("拿到 2 个账号", len(items) == 2, str(items))
    check("调的是 waves_account_list",
          multi_calls[0][0] == "waves_account_list", str(multi_calls))
    multi_calls.clear()
    wuwa.list_accounts(Cfg(), c8)
    check("**缓存住了，不再起进程**", not multi_calls, str(multi_calls))

    text = wuwa.format_accounts(items, active_id="acc-alt")
    check("列表带序号", "1. 主号" in text and "2. 小号" in text, text)
    check("标出哪个是当前", "← 当前" in text, text)
    check("当前标记落在第 2 个账号那行",
          text.splitlines()[2].endswith("← 当前"), text)
    check("给出用法提示", "切换" in text and "@2" in text, text)
    check("账号为空 -> 直接是「怎么登录」的提示",
          "wuthering-waves-platform login" in wuwa.format_accounts([]),
          wuwa.format_accounts([]))

    print()
    print("  按序号查")
    c9 = wuwa.Cache(TMP / "c9.json")
    multi_calls.clear()
    ok, text = wuwa.query(Cfg(), c9, "维里奈", account_index=2)
    tools = [t for t, _ in multi_calls]
    check("先列账号、再查角色",
          tools == ["waves_account_list", "waves_character_build"], str(tools))
    check("**accountId 用的是第 2 个账号的 id**",
          multi_calls[1][1].get("accountId") == "acc-alt", str(multi_calls))
    check("返回里有第 2 个账号的痕迹", "acc-alt" in text, text[:80])

    multi_calls.clear()
    ok, text = wuwa.query(Cfg(), c9, "维里奈", account_index=1)
    check("第 1 个账号用的是 acc-main",
          multi_calls[-1][1].get("accountId") == "acc-main", str(multi_calls))

    # 缓存不串：用**全新的缓存**走一遍标准序列，
    # 否则前一步刚查过的账号会命中缓存，测不出东西。
    c9b = wuwa.Cache(TMP / "c9b.json")
    multi_calls.clear()
    wuwa.query(Cfg(), c9b, "维里奈", account_index=1)
    first = [t for t, _ in multi_calls]
    multi_calls.clear()
    wuwa.query(Cfg(), c9b, "维里奈", account_index=2)
    second = multi_calls
    check("**两个账号的缓存不互相串**（@1 查完，@2 仍要真的去查）",
          bool(second) and second[-1][1].get("accountId") == "acc-alt",
          "第一次 %s / 第二次 %s" % (first, second))
    multi_calls.clear()
    wuwa.query(Cfg(), c9b, "维里奈", account_index=2)
    check("@2 再查一次 -> 命中自己的缓存", not multi_calls, str(multi_calls))
    multi_calls.clear()
    wuwa.query(Cfg(), c9b, "维里奈", account_index=1)
    check("@1 再查一次 -> 也命中自己的缓存", not multi_calls, str(multi_calls))

    multi_calls.clear()
    ok, text = wuwa.query(Cfg(), c9, "维里奈", account_index=9)
    check("序号超范围时给提示，不报错",
          (not ok) and "没有第 9 个账号" in text and "有 2 个" in text, text)
    check("超范围时不发起查询",
          all(t == "waves_account_list" for t, _ in multi_calls),
          str(multi_calls))

    print()
    print("  切换")
    c10 = wuwa.Cache(TMP / "c10.json")
    wuwa.list_accounts(Cfg(), c10)          # 先填缓存
    multi_calls.clear()
    ok, text = wuwa.switch_account(Cfg(), c10, 2)
    # 注意：切换会**先拉一次账号列表**（要把序号换成 id），所以看最后一次调用
    check("调的是 waves_account_set_active",
          multi_calls[-1][0] == "waves_account_set_active", str(multi_calls))
    check("传的是第 2 个账号的 id",
          multi_calls[-1][1].get("accountId") == "acc-alt", str(multi_calls))
    check("回执里带账号名", ok and "小号" in text, text)
    multi_calls.clear()
    wuwa.list_accounts(Cfg(), c10)
    check("**切换后账号列表缓存被清掉**（下次重新拉）",
          bool(multi_calls) and multi_calls[0][0] == "waves_account_list",
          str(multi_calls))

    multi_calls.clear()
    ok, text = wuwa.switch_account(Cfg(), c10, 7)
    check("切不存在的序号时给提示",
          (not ok) and "没有第 7 个账号" in text, text)
    check("切不存在的不发 set_active",
          all(t != "waves_account_set_active" for t, _ in multi_calls),
          str(multi_calls))

    print()
    print("  没登录时")
    def stub_nologin(cfg, tool, args=None, timeout=0):
        raise McpError('{"code": "CREDENTIAL_ERROR", "message": "没有找到账号凭据"}')

    wuwa.call_tool = stub_nologin
    c11 = wuwa.Cache(TMP / "c11.json")
    ok, text = wuwa.query(Cfg(), c11, "维里奈", account_index=1)
    check("@N 且没登录 -> 还是那句登录提示",
          (not ok) and "wuthering-waves-platform login" in text, text[:80])
finally:
    wuwa.call_tool = real_call

print()
print("6) 群里每人各自登录、各查各的")
print()
print("  按微信号归属（群昵称谁都能改，不能当钥匙）")
k1, n1 = wuwa.member_key({"id": "wxid_aaa", "nickname": "小明"}, "小明")
k2, n2 = wuwa.member_key({"id": "wxid_bbb", "nickname": "小红"}, "小红")
check("两个人拿到不同的 key", k1 and k2 and k1 != k2, "%s / %s" % (k1, k2))
check("key 里不含明文微信号（要进凭据文件和日志）",
      "wxid_aaa" not in k1 and "wxid" not in k1.replace("wx-", ""), k1)
check("key 是稳定的（同一个人每次一样）",
      wuwa.member_key({"id": "wxid_aaa"}, "小明")[0] == k1)
check("**昵称变了 key 不变**（这正是要防的冒充）",
      wuwa.member_key({"id": "wxid_aaa", "nickname": "改成小红了"}, "小红")[0] == k1)
check("**拿不到微信号就拒绝**（不给 key）",
      wuwa.member_key({"nickname": "小明"}, "小明")[0] == "",
      str(wuwa.member_key({"nickname": "小明"}, "小明")))
check("显式打开 allow_nickname 才退回昵称",
      wuwa.member_key({"nickname": "小明"}, "小明",
                      allow_nickname=True)[0].startswith("nick-"))
check("显示名取昵称", n1 == "小明" and n2 == "小红", "%r/%r" % (n1, n2))

print()
print("  手机号打码（群里消息所有成员都看得到）")
check("中间打掉", wuwa.mask_mobile("13800138000") == "138****8000",
      wuwa.mask_mobile("13800138000"))
check("短号也不会漏", "*" in wuwa.mask_mobile("138"), wuwa.mask_mobile("138"))

print()
print("  登录两步走")
lstore = wuwa.Cache(TMP / "login.json")
ok, mobile = wuwa.start_login(lstore, k1, "138 0013 8000", "小明")
check("第一步收下手机号（去掉了空格）", ok and mobile == "13800138000",
      "%s / %s" % (ok, mobile))
check("存住了", wuwa.pending_mobile(lstore, k1)[0] == "13800138000",
      str(wuwa.pending_mobile(lstore, k1)))
check("**别人的 key 查不到他的进行中登录**",
      wuwa.pending_mobile(lstore, k2)[0] == "",
      str(wuwa.pending_mobile(lstore, k2)))
check("手机号不像手机号时拒绝",
      wuwa.start_login(lstore, k1, "abc", "小明")[0] is False)

login_calls: list = []


def stub_login(cfg, tool, args=None, timeout=0):
    login_calls.append((tool, dict(args or {})))
    if tool == "waves_account_login_sms":
        if (args or {}).get("code") == "000000":
            raise McpError('{"code":"CODE_ERROR","message":"验证码错误或已失效"}')
        return {"id": (args or {}).get("accountId"),
                "label": (args or {}).get("label")}
    if tool == "waves_account_list":
        return {"items": [{"id": k1, "label": "小明"}]}
    return ALL


wuwa.call_tool = stub_login
try:
    print()
    print("  第二步：交验证码")
    ok, text = wuwa.finish_login(Cfg(), lstore, k1, "123456")
    check("登录成功", ok, text)
    check("调的是 waves_account_login_sms",
          login_calls[0][0] == "waves_account_login_sms", str(login_calls))
    check("**账号 ID 用的是这个人的 key**（这才叫「谁的号谁查」）",
          login_calls[0][1].get("accountId") == k1, str(login_calls))
    check("label 用群昵称", login_calls[0][1].get("label") == "小明",
          str(login_calls))
    check("传的是他留的那个手机号",
          login_calls[0][1].get("mobile") == "13800138000", str(login_calls))
    check("登录完把「进行中」清掉了", wuwa.pending_mobile(lstore, k1)[0] == "")

    ok, text = wuwa.finish_login(Cfg(), lstore, k1, "123456")
    check("没有进行中的登录时给提示（不是报错）",
          (not ok) and "没有正在进行的登录" in text, text[:60])

    wuwa.start_login(lstore, k1, "13800138000", "小明")
    login_calls.clear()
    ok, text = wuwa.finish_login(Cfg(), lstore, k1, "000000")
    check("验证码错 -> 翻成人话，不是甩 JSON",
          (not ok) and "验证码" in text and "{" not in text, text)
    check("验证码错时不清掉「进行中」（让他直接重发新码）",
          wuwa.pending_mobile(lstore, k1)[0] == "13800138000",
          str(wuwa.pending_mobile(lstore, k1)))

    print()
    print("  登录失败的三种情形要分清楚（踩过的坑：全都报成「验证码错」）")
    # 真实事件：用户交了一个 10 秒前拿到的验证码，机器人回「验证码不对或已过期」，
    # 而平台的原话是「获取日常数据失败: 暂无数据, 请漂泊者绑定角色后查看」。
    # 根因：错误体里永远有 "code" 字段，我拿它当「验证码错」的标志。
    REAL_API_ERROR = (
        '{\n  "code": "API_ERROR",\n'
        '  "message": "获取日常数据失败: -暂无数据-\\n请漂泊者绑定角色后查看。",\n'
        '  "details": {"operation": "获取日常数据", "code": 6001}\n}')

    def stub_no_role(cfg, tool, args=None, timeout=0):
        raise McpError(REAL_API_ERROR)

    wuwa.call_tool = stub_no_role
    lstore2 = wuwa.Cache(TMP / "login2.json")
    wuwa.start_login(lstore2, k1, "13800138000", "小明")
    ok, text = wuwa.finish_login(Cfg(), lstore2, k1, "123456")
    check("**「库街区没绑定角色」不能报成「验证码错」**",
          (not ok) and "验证码不对" not in text and "验证码错" not in text, text)
    check("要说清验证码其实是对的",
          "验证码是对的" in text, text)
    check("要说清真正的原因（绑定角色 / 数据展示开关）",
          "绑定" in text and "展示开关" in text, text)
    check("而且是可操作的（告诉他弄好后再发一次）",
          "登录 <手机号>" in text, text)

    def stub_code_wrong(cfg, tool, args=None, timeout=0):
        raise McpError('{"code": "CODE_ERROR", "message": "验证码错误"}')

    wuwa.call_tool = stub_code_wrong
    lstore3 = wuwa.Cache(TMP / "login3.json")
    wuwa.start_login(lstore3, k1, "13800138000", "小明")
    ok, text = wuwa.finish_login(Cfg(), lstore3, k1, "999999")
    check("真的验证码错时才说验证码错",
          (not ok) and "验证码" in text and "重新发一次" in text, text)

    def stub_weird(cfg, tool, args=None, timeout=0):
        raise McpError('{"code": "WEIRD", "message": "火星文错误"}')

    wuwa.call_tool = stub_weird
    lstore4 = wuwa.Cache(TMP / "login4.json")
    wuwa.start_login(lstore4, k1, "13800138000", "小明")
    ok, text = wuwa.finish_login(Cfg(), lstore4, k1, "123456")
    check("**认不出来就把平台原话给出来**（猜错比不给更坏）",
          (not ok) and "火星文错误" in text, text)

    print()
    print("  身份解析不稳定时，两步也要能接上（另一个真实踩到的坑）")
    # 实测：第一步只拿到群昵称（key=nick-xxx），第二步拿到微信号（key=wx-yyy），
    # 两步 key 不同 -> 回「没有正在进行的登录」，用户完全看不懂。
    lstore5 = wuwa.Cache(TMP / "login5.json")
    wuwa.call_tool = stub_login
    wuwa.start_login(lstore5, "nick-aaa", "13800138000", "小明",
                     group="G1", sender="群友甲")
    ok, text = wuwa.finish_login(Cfg(), lstore5, "wx-bbb", "123456",
                                 group="G1", sender="群友甲")
    check("**第一步用昵称 key、第二步用微信号 key，仍然接得上**", ok, text)
    check("账号归到**这一步**解析出来的 key（更准的那个）",
          isinstance(login_calls[-1][1], dict)
          and login_calls[-1][1].get("accountId") == "wx-bbb",
          str(login_calls[-1]))
    ok, text = wuwa.finish_login(Cfg(), lstore5, "wx-bbb", "123456",
                                 group="G1", sender="群友甲")
    check("用完之后两份都清掉了（不会重复登录）",
          (not ok) and "没有正在进行的登录" in text, text)
    ok, _ = wuwa.finish_login(Cfg(), lstore5, "wx-bbb", "123456",
                              group="G2", sender="群友甲")
    check("**别的群不会误用这个待登录状态**",
          (not ok) and "没有正在进行的登录" in _, _)

    wuwa.call_tool = stub_login
    print()
    print("  有没有登录过")
    c12 = wuwa.Cache(TMP / "c12.json")
    check("k1 已登录", wuwa.has_account(Cfg(), c12, k1))
    check("k2 没登录", not wuwa.has_account(Cfg(), c12, k2))

    print()
    print("  查询用的是自己的账号")
    c13 = wuwa.Cache(TMP / "c13.json")
    login_calls.clear()
    wuwa.query(Cfg(), c13, "维里奈", account_id=k1)
    check("查询带上自己的 accountId",
          login_calls[-1][0] == "waves_character_build"
          and login_calls[-1][1].get("accountId") == k1, str(login_calls[-1]))
    c14 = wuwa.Cache(TMP / "c14.json")
    login_calls.clear()
    wuwa.query(Cfg(), c14, "维里奈", account_id=k2)
    check("**另一个人的查询用另一个 accountId（不串）**",
          login_calls[-1][1].get("accountId") == k2, str(login_calls[-1]))
finally:
    wuwa.call_tool = real_call

# ---------------------------------------------------------------- @ 群友
print()
print("  询问验证码时要 @ 那个群友（真 @，不是文本里塞个名字）")
print()
import src.main as main  # noqa: E402
from src.config import load_config  # noqa: E402
from src.policy import Gatekeeper  # noqa: E402
from src.wechat_io import IncomingMessage  # noqa: E402

sent: list = []


class StubStop:
    stopped = False


class FakeBot:
    def __init__(self, allow_nickname: bool = False) -> None:
        self.cfg = load_config(ROOT / "config.yaml")
        # **固定住**，别跟着本机 config.yaml 走 —— 否则用户一改设置，
        # 这两条断言就会莫名其妙地变红（真踩过）。
        self.cfg.wuwa.allow_nickname_identity = allow_nickname
        self.gate = Gatekeeper(self.cfg)
        self.paused = False
        self.stop = StubStop()
        self.wuwa_cache = wuwa.Cache(TMP / "fb_cache.json")
        self.wuwa_login = wuwa.Cache(TMP / "fb_login.json")
        self.adapter = None

    def _say(self, group, text, *, split=True, at=None):
        sent.append({"group": group, "text": text, "at": at})


FakeBot.handle_wuwa = main.Bot.handle_wuwa
FB = FakeBot()
group = FB.cfg.commands.trusted_chats[0] if FB.cfg.commands.trusted_chats \
    else FB.cfg.whitelist_groups[0]

wuwa.call_tool = stub_login
try:
    sent.clear()
    m = IncomingMessage(group=group, sender="小明", content="鸣潮 登录 13900000000",
                        msg_key="m1")
    m.identity = {"id": "wxid_aaa", "nickname": "小明"}
    FB.handle_wuwa(m, "鸣潮 登录 13900000000")
    check("回了「去拿验证码」", sent and "验证码" in sent[0]["text"],
          str(sent)[:120])
    check("**回执里 @ 了发指令的那个人**",
          sent and sent[0]["at"] == ["小明"], str(sent)[:160])
    check("手机号在群里是打码的",
          sent and "13900000000" not in sent[0]["text"]
          and "139****0000" in sent[0]["text"], str(sent)[:200])

    sent.clear()
    m2 = IncomingMessage(group=group, sender="路人甲", content="鸣潮 登录 13900000001",
                         msg_key="m2")
    m2.identity = {}          # 拿不到微信号
    FB.handle_wuwa(m2, "鸣潮 登录 13900000001")
    check("**拿不到微信号时不给登录**（防冒充）",
          sent and "拿不到你的微信号" in sent[0]["text"], str(sent)[:120])
    check("而且不 @（没必要惊动他）", sent and sent[0]["at"] is None,
          str(sent)[:120])

    # 把开关打开时（用户在本机就是这么设的）才允许退回昵称
    sent.clear()
    FB2 = FakeBot(allow_nickname=True)
    m4 = IncomingMessage(group=group, sender="路人甲", content="鸣潮 登录 13900000002",
                         msg_key="m4")
    m4.identity = {}
    FB2.handle_wuwa(m4, "鸣潮 登录 13900000002")
    check("**显式打开 allow_nickname_identity 后允许**（这是用户的选择）",
          sent and "手机号收到" in sent[0]["text"], str(sent)[:120])

    sent.clear()
    m3 = IncomingMessage(group=group, sender="小明", content="鸣潮",
                         msg_key="m3")
    m3.identity = {"id": "wxid_bbb", "nickname": "小明"}
    FB.handle_wuwa(m3, "鸣潮")
    check("没登录过时引导他去登录，并 @ 他",
          sent and "还没登录" in sent[0]["text"] and sent[0]["at"] == ["小明"],
          str(sent)[:160])
finally:
    wuwa.call_tool = real_call

failed = [c for c in cases if not c[1]]
print()
if failed:
    print("不通过 %d / %d" % (len(failed), len(cases)))
    raise SystemExit(1)
print("全部通过（%d 项）" % len(cases))
