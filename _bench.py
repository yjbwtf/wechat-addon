"""对话链路的 token 基准。改 prompt 前后各跑一次，看省了多少。

跑法：venv\\Scripts\\python.exe _bench.py
"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.brain import Brain, build_prompt
from src.config import load_config, load_persona
from src.history import StoredMessage
from src.main import format_history

cfg = load_config()
name, persona = load_persona("test")

DEMO = [
    ("张三", "今晚有人打游戏吗", False),
    ("李四", "我我我", False),
    ("景德镇到印尼", "二次元😹😹😹", False),
    ("蓝色大肥鱼", "算我一个喵", True),
    ("张三", "行，那就八点半", False),
    ("王五", "话说那个 ccb 我真服了", False),
    ("李四", "哈哈哈哈哈踩踩背", False),
    ("张三", "老爷爷我给你踩背来喽", False),
    ("蓝色大肥鱼", "笑死了，这也能火", True),
    ("王五", "北京明天天气咋样", False),
    ("李四", "不知道，问它", False),
    ("张三", "对了那个技能五子棋你们看了没", False),
    ("景德镇到印尼", "看了，抽象到我了", False),
    ("王五", "飞沙走石！", False),
    ("蓝色大肥鱼", "我先手天元，你随意", True),
    ("李四", "？", False),
    ("张三", "丝瓜汤文学懂不懂", False),
    ("王五", "你肝火太旺，喝点丝瓜汤吧", False),
    ("李四", "哈哈哈哈哈哈", False),
    ("景德镇到印尼", "回！答！我！", False),
    ("蓝色大肥鱼", "Look in my eyes!", True),
    ("张三", "法拉利老了还是法拉利", False),
    ("王五", "那必须的", False),
    ("李四", "明天降温记得穿外套", False),
    ("张三", "ok", False),
    ("王五", "话说机器人今天话有点多啊", False),
    ("蓝色大肥鱼", "哪有，我很克制的喵", True),
    ("李四", "确实", False),
    ("张三", "别贫了", False),
    ("王五", "所以明天到底下不下雨", False),
]
now = int(time.time())
history = [
    StoredMessage(sender=s, content=c, is_bot=b, created_at=now - (len(DEMO) - i) * 30)
    for i, (s, c, b) in enumerate(DEMO)
]
hist_block = format_history(history)

SCENES = [
    ("纯闲聊（不调工具）", "张三", "别贫了，你今晚来不来"),
    ("空 @", "张三", "（只 @ 了你一下，没有说别的内容）"),
    ("要查天气（会调工具）", "王五", "帮我看看北京明天天气，顺便说下要不要带伞"),
]

print(f"persona {len(persona)} 字 | history {len(hist_block)} 字（{len(history)} 条）")
print("=" * 78)

brain = Brain(cfg.dsh, cwd=cfg.data_dir / "agent-cwd")
rows = []
for label, sender, text in SCENES:
    prompt = build_prompt(persona, hist_block, sender, text, "<群名>")
    t0 = time.time()
    try:
        reply = brain.ask(prompt, user=sender, group="test")
    except Exception as exc:  # noqa: BLE001
        print(f"{label}: 失败 {exc}")
        continue
    u = brain.last_usage or {}
    rows.append((label, len(prompt), brain.last_step,
                 u.get("inputTokens"), u.get("cacheReadTokens"),
                 u.get("outputTokens"), u.get("totalTokens")))
    print(f"{label}")
    print(f"  prompt {len(prompt)} 字 | 耗时 {time.time()-t0:.1f}s")
    print(f"  回复：{reply}")
    print(f"  step={brain.last_step} in={u.get('inputTokens')} "
          f"cacheR={u.get('cacheReadTokens')} out={u.get('outputTokens')} "
          f"total={u.get('totalTokens')}")
    print("-" * 78)

print("\n汇总（total 列越小越好）")
print(f"{'场景':<22}{'prompt字':>9}{'step':>6}{'input':>9}{'cacheR':>9}{'out':>6}{'total':>9}")
tot = 0
for r in rows:
    print(f"{r[0]:<22}{r[1]:>9}{r[2]:>6}{r[3]:>9}{r[4]:>9}{r[5]:>6}{r[6]:>9}")
    tot += r[6] or 0
print(f"{'合计':<22}{'':>9}{'':>6}{'':>9}{'':>9}{'':>6}{tot:>9}")
