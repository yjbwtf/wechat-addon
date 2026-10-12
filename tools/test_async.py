"""验证耗时指令不再阻塞主循环。

用一个「睡 3 秒」的桩替换真的 agent，测三件事：
  1. handle() 立刻返回（不阻塞）—— 这是修复的核心
  2. 结果通过 _outbox 回到主线程发送
  3. 同时来第二个请求会被锁拒绝，而不是排队
"""
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
# 原来是 sys.path.insert(0, r"ROOT") —— 没被替换的占位符，测试一直跑不起来。
# 见 tools\test_dedup.py 里的同一处说明。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402
from src.main import Bot  # noqa: E402
from src.policy import make_msg_key  # noqa: E402
from src.wechat_io import IncomingMessage, build_adapter  # noqa: E402

cfg = load_config()
cfg.whitelist_groups = ["test"]
cfg.mode = "auto"
cfg.rate_limit.per_group_per_hour = 60
cfg.rate_limit.min_interval_seconds = 0
cfg.rate_limit.reply_delay = (0.05, 0.1)
cfg.split.delay = (0.1, 0.2)
cfg.db_path = cfg.data_dir / "_t.sqlite3"
stop = cfg.data_dir / "STOP"
had = stop.exists()
stop.unlink(missing_ok=True)

# 口令**从配置里读**，不要硬编码。这里原来写死了 "ccb"，而 config.yaml 里
# 后来改成了别的词 —— 于是口令校验不通过、指令被静默吞掉，测试报
# 「实际调用了 0 次」。那不是代码坏了，是测试跟不上配置。
SECRET = (cfg.commands.secret or "").strip()

# 同理，把这个测试**不关心**的闸门全部放行：它的主题是「耗时指令会不会
# 阻塞主循环」，不是鉴权。发送者 10001 不在用户的 allowed_senders 里，
# 不放开的话指令会被静默忽略，测的就成了鉴权规则。
cfg.commands.allowed_senders = ["10001"]
cfg.commands.verify_identity = False
cfg.commands.identity_mode = "any"

adapter = build_adapter(cfg, console=True)
bot = Bot(cfg, adapter)


@dataclass
class FakeResult:
    ok: bool = True
    text: str = "慢任务完成"
    tools: list = field(default_factory=lambda: ["pwsh"])
    seconds: float = 3.0
    reason: str = ""


SLEEP = 3.0
calls = []


def slow_run(task, **kw):
    calls.append((time.time(), task))
    time.sleep(SLEEP)
    return FakeResult(seconds=SLEEP)


bot.runner.run = slow_run
bot.runner.refuse_reason = lambda: ""
bot.runner.allowed_in = lambda g: True
bot.runner.allowed_now = lambda: True
bot.cfg.agent.ack = True


def feed(content):
    bot.handle(IncomingMessage(
        group="test", sender="10001", content=content,
        msg_key=make_msg_key("test", "10001", content + str(time.time()), str(time.time()))))


print("=" * 74)
print(f"1) 发一条 agent 指令，看 handle() 是否立刻返回（桩要睡 {SLEEP:.0f} 秒）")
print("=" * 74)
t0 = time.time()
feed(rf"\\agent 测试任务 {SECRET}")
dt = time.time() - t0
print(f"  handle() 耗时 {dt:.3f}s")
blocked = dt > 1.0
print(f"  {'!! 仍然阻塞' if blocked else '不阻塞 ✓'}")

print()
print("=" * 74)
print("2) 第一个还在跑时，再发一条 —— 应被锁拒绝")
print("=" * 74)
t0 = time.time()
feed(rf"\\agent 第二个任务 {SECRET}")
print(f"  handle() 耗时 {time.time()-t0:.3f}s")
time.sleep(0.3)
bot._drain_outbox()
got = [t for _g, t in adapter.sent]
print(f"  已发出的: {got}")
rejected = any("上一个还在跑" in t for t in got)
print(f"  {'被拒绝 ✓' if rejected else '!! 没拒绝'}")

print()
print("=" * 74)
print("3) 等后台跑完，排空 outbox")
print("=" * 74)
adapter.sent.clear()
deadline = time.time() + SLEEP + 6
while time.time() < deadline:
    bot._drain_outbox()
    if any("慢任务完成" in t for _g, t in adapter.sent):
        break
    time.sleep(0.3)
for _g, t in adapter.sent:
    print(f"  <- {t}")
ok = any("慢任务完成" in t for _g, t in adapter.sent)
print(f"  {'结果回来了 ✓' if ok else '!! 结果没回来'}")
print(f"  实际调用了 {len(calls)} 次（应为 1，第二个被锁挡住）")

bot.close()
if had:
    stop.write_text("恢复", encoding="utf-8")
import os
for f in os.listdir(cfg.data_dir):
    if f.startswith("_t.sqlite3"):
        os.remove(cfg.data_dir / f)

print()
print("=" * 74)
print("全部通过" if (not blocked and rejected and ok and len(calls) == 1) else "!! 有不符合预期的地方")
print("=" * 74)
