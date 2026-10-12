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

failed = [c for c in cases if not c[1]]
print()
if failed:
    print("不通过 %d / %d" % (len(failed), len(cases)))
    raise SystemExit(1)
print("全部通过（%d 项）" % len(cases))
print()
print("注：这里只测参数拼装。真正的请求能不能通，看 data\\_fake_llm.py 那套：")
print("    把叠加层的 baseURL 指到本机假服务，再用 brain.ask() 跑一条消息。")
