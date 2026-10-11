"""诊断去重失效：连续读取两次，比较同一批消息的 id/hash 是否稳定。"""
import time

from wxauto4 import WeChat

GROUP = "test"
wx = WeChat()
time.sleep(1)


def snapshot(tag):
    try:
        wx.ChatWith(who=GROUP)
    except TypeError:
        wx.ChatWith(GROUP)
    except Exception as exc:
        print(f"[{tag}] ChatWith 失败: {exc}")
        return {}
    time.sleep(1.5)
    try:
        msgs = wx.GetAllMessage() or []
    except Exception as exc:
        print(f"[{tag}] GetAllMessage 失败: {exc}")
        return {}

    print(f"\n===== {tag}：读到 {len(msgs)} 条 =====")
    out = {}
    for i, m in enumerate(msgs):
        raw = getattr(m, "raw", None)
        raw = raw if isinstance(raw, dict) else {}
        mid = getattr(m, "id", None)
        mhash = getattr(m, "hash", None)
        content = raw.get("content") or getattr(m, "content", "")
        print(f"  [{i:02d}] id={mid}")
        print(f"       hash={mhash}  content={content!r}")
        out[mid] = content
    return out


first = snapshot("第一次读取")
time.sleep(8)
second = snapshot("第二次读取（8 秒后）")

print("\n===== 对比 =====")
only_first = set(first) - set(second)
only_second = set(second) - set(first)
print(f"第一次 {len(first)} 条，第二次 {len(second)} 条")
print(f"只在第一次出现: {len(only_first)} 条")
for mid in only_first:
    print(f"    {mid} -> {first[mid]!r}")
print(f"只在第二次出现: {len(only_second)} 条")
for mid in only_second:
    print(f"    {mid} -> {second[mid]!r}")
same = set(first) & set(second)
print(f"两次都出现: {len(same)} 条")
