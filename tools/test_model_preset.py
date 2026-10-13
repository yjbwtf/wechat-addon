"""闲聊模型档位的切换逻辑：不联网、不需要 Key。

只验证「档位 -> dsh 参数」这段映射对不对，以及几种坏输入下的行为。
真正的模型调用能不能通，用假服务单独验（见本文件末尾的说明）。

跑法：
    venv\\Scripts\\python.exe tools\\test_model_preset.py
退出码 0 = 全过。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import model_preset as mp  # noqa: E402


class Cfg:
    """够用的假配置对象。"""

    def __init__(self, preset="deepseek", preset_dir="", key=""):
        self.model_preset = preset
        self.preset_dir = preset_dir
        self.zhipu_api_key = key


cases: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    cases.append((name, ok, detail))
    print("  %s  %s%s" % ("[OK]" if ok else "[!!]", name,
                          ("   " + detail) if detail and not ok else ""))


print("档位 -> 参数")
print()

# 1) 默认档 = 不加 --patch，回到 profile 自带配置
args = mp.launch_args(Cfg("deepseek"))
check("deepseek 不加 --patch", args == [], "得到 %r" % (args,))
check("deepseek 不带 Key", mp.env_overrides(Cfg("deepseek")) == {})

# 2) zhipu 档 = 加 --patch，且指向仓库里的叠加层
cfg = Cfg("zhipu")
args = mp.launch_args(cfg)
want = ROOT / "dsh" / "model-zhipu.yml"
check("zhipu 加了 --patch", args[:1] == ["--patch"], "得到 %r" % (args,))
check("zhipu 指向 dsh\\model-zhipu.yml",
      len(args) == 2 and Path(args[1]) == want, "得到 %r" % (args,))
check("叠加层文件真的在", want.is_file(), str(want))

# 3) Key：配置里有就用配置的；配置留空回退环境变量；都没有就不塞
check("zhipu 用配置里的 Key",
      mp.env_overrides(Cfg("zhipu", key="  abc123  ")) == {"ZAI_API_KEY": "abc123"})
check("zhipu 配置留空 -> 环境变量",
      mp.env_overrides(Cfg("zhipu", key="")) == {})
check("deepseek 不塞 Key",
      mp.env_overrides(Cfg("deepseek", key="abc")) == {})

# 4) 坏输入：不认识的档位退回默认，而不是崩
check("不认识的档位退回 deepseek", mp.normalize("gpt5") == "deepseek")
check("大小写/空格无所谓", mp.normalize("  ZHIPU ") == "zhipu")
check("空值退回默认", mp.normalize(None) == "deepseek")

# 4b) 「改完下一条消息生效」靠的是这个：Brain 存的是**引用**，
#     所以 main.py 原地改 cfg.dsh 的字段，Brain 立刻就能看到。
#     这条一旦被人改成 Brain(copy(cfg.dsh))，热切换就会静默失效 ——
#     界面上切了档、消息还是旧模型回的，很难查。所以钉死它。
try:
    from src.brain import Brain  # noqa: PLC0415
    from src.config import DshConfig  # noqa: PLC0415
    dsh = DshConfig(command="dsh", profile="wechat")
    b = Brain(dsh)
    dsh.model_preset = "zhipu"
    check("Brain 和 cfg 共享同一个 DshConfig（热切换的前提）",
          b.cfg is dsh and b.cfg.model_preset == "zhipu")
except Exception as exc:  # noqa: BLE001
    check("Brain 和 cfg 共享同一个 DshConfig（热切换的前提）", False, repr(exc))

# 4c) **\agent 绝不能被闲聊档位带着走。**
#     这条链路上跑的是完整工具链（读写文件、跑命令），模型不该因为
#     界面上切了个下拉框就变。用人话讲：闲聊换模型，agent 照旧。
try:
    from src.agent import AgentRunner  # noqa: PLC0415
    from src.config import AgentConfig  # noqa: PLC0415

    chat = DshConfig(command="dsh", profile="wechat", model_preset="zhipu",
                     zhipu_api_key="dummy")
    runner = AgentRunner(AgentConfig(enabled=True), chat, ROOT)
    check("agent 不吃闲聊的叠加层",
          mp.launch_args(runner.brain.cfg) == [],
          "得到 %r" % (mp.launch_args(runner.brain.cfg),))
    check("agent 不会被塞智谱的 Key",
          mp.env_overrides(runner.brain.cfg) == {})
    check("agent 走的是自己的 profile",
          runner.brain.cfg.profile == "wechat-agent")
except Exception as exc:  # noqa: BLE001
    check("agent 不吃闲聊的叠加层", False, repr(exc))

# 5) 叠加层文件不存在时：不加参数、不抛异常（宁可退回默认模型，也别不回复）
missing = Cfg("zhipu", preset_dir=str(ROOT / "dsh" / "不存在的目录"))
try:
    args = mp.launch_args(missing)
    check("叠加层缺失时不抛异常且不加参数", args == [], "得到 %r" % (args,))
except Exception as exc:  # noqa: BLE001
    check("叠加层缺失时不抛异常且不加参数", False, repr(exc))

# 6) preset_dir 生效
custom = Cfg("zhipu", preset_dir=str(ROOT / "dsh"))
check("preset_dir 生效",
      Path(mp.launch_args(custom)[1]).parent == ROOT / "dsh")

# 7) **失败兜底**：免费档挂了要退到 DSH 账号重试，而不是石沉大海。
#    起因是实测：智谱返回 429/1305，DSH 内置重试跑满 5 次仍失败，
#    然后那条消息一个字都没回。
print()
print("失败兜底")
print()
check("zhipu 档可以退", mp.fallback_preset(Cfg("zhipu")) == "deepseek")
check("已经在默认档就不退（没得退）", mp.fallback_preset(Cfg("deepseek")) is None)

try:
    from src.brain import BrainError          # noqa: PLC0415
    from src.main import Bot                  # noqa: PLC0415

    class StubBrain:
        """第一次调失败，第二次成功；顺便记下每次调用时看到的档位。"""

        def __init__(self, failures: int = 1):
            self.failures = failures
            self.seen: list[str] = []

        def ask(self, prompt, **kw):
            self.seen.append(self.cfg.model_preset)
            if self.failures > 0:
                self.failures -= 1
                raise BrainError('429: {"code":"1305","message":"访问量过大"}')
            return "兜底成功"

    def make_bot(preset: str):
        # 跳过 __init__（那要建数据库、适配器），只装 _ask 需要的东西
        bot = object.__new__(Bot)
        bot.cfg = type("C", (), {})()
        bot.cfg.dsh = DshConfig(command="dsh", profile="wechat",
                                model_preset=preset)
        bot.brain = StubBrain()
        bot.brain.cfg = bot.cfg.dsh      # 和真 Brain 一样：存的是引用
        return bot

    b = make_bot("zhipu")
    out = Bot._ask(b, "在吗")
    check("第一次失败后用 deepseek 重试成功", out == "兜底成功", "得到 %r" % (out,))
    check("两次调用看到的档位是 zhipu -> deepseek",
          b.brain.seen == ["zhipu", "deepseek"], "得到 %r" % (b.brain.seen,))
    check("**兜底之后档位要改回去**（不能把用户的选择改掉）",
          b.cfg.dsh.model_preset == "zhipu",
          "得到 %r" % (b.cfg.dsh.model_preset,))

    b2 = make_bot("deepseek")
    b2.brain.failures = 1
    try:
        Bot._ask(b2, "在吗")
        check("默认档失败时不重试（没得退）", False, "居然没抛异常")
    except BrainError:
        check("默认档失败时不重试（没得退）", b2.brain.seen == ["deepseek"],
              "调用序列 %r" % (b2.brain.seen,))

    b3 = make_bot("zhipu")
    b3.brain.failures = 0
    check("第一次就成功时不重试", Bot._ask(b3, "在吗") == "兜底成功"
          and b3.brain.seen == ["zhipu"], "得到 %r" % (b3.brain.seen,))
except Exception as exc:  # noqa: BLE001
    check("失败兜底", False, repr(exc))

print()
print("  超时换档（brain._ask_plans）")
# 用户实测：档位是 zhipu（免费档），群里 @ 之后最长干等 3 分钟、日志一行不动。
# 现在给主模型一个"耐心"上限，超时就换兜底档。
from src import brain as _brain          # noqa: E402
from src.config import DshConfig         # noqa: E402


def _plans(preset, after, fb="", total=180):
    cfg = DshConfig(command="dsh", model_preset=preset,
                    timeout_seconds=total, fallback_after_seconds=after,
                    fallback_preset=fb)
    return _brain.Brain(cfg)._ask_plans()


check("zhipu 档 -> 先给 45s，再换 deepseek",
      _plans("zhipu", 45) == [("zhipu", 45), ("deepseek", 135)],
      str(_plans("zhipu", 45)))
check("已经是默认档 -> 没得退，只试一次",
      _plans("deepseek", 45) == [("deepseek", 180)],
      str(_plans("deepseek", 45)))
check("耐心设 0 -> 不换（一直等总超时）",
      _plans("zhipu", 0) == [("zhipu", 180)], str(_plans("zhipu", 0)))
check("耐心 >= 总超时 -> 不换（第二项来不及跑）",
      _plans("zhipu", 200) == [("zhipu", 180)], str(_plans("zhipu", 200)))
check("可以写死换回哪个档",
      _plans("deepseek", 30, fb="zhipu") == [("deepseek", 30), ("zhipu", 150)],
      str(_plans("deepseek", 30, fb="zhipu")))
check("两段加起来不超过总超时",
      sum(x[1] for x in _plans("zhipu", 60)) == 180,
      str(_plans("zhipu", 60)))
check("target 耐心要明显小于总超时（留 5s 余量）",
      _plans("zhipu", 176) == [("zhipu", 180)], str(_plans("zhipu", 176)))

failed = [c for c in cases if not c[1]]
print()
if failed:
    print("不通过 %d / %d" % (len(failed), len(cases)))
    raise SystemExit(1)
print("全部通过（%d 项）" % len(cases))
print()
print("注：这里只测参数拼装。真正的请求能不能通，看 data\\_fake_llm.py 那套：")
print("    把叠加层的 baseURL 指到本机假服务，再用 brain.ask() 跑一条消息。")
