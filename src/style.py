"""人味层：颜文字、口头语，以及「别像人机」的兜底清洗。

分工
----
- **主要**靠 persona.yaml 的提示词约束模型（改完立即生效，不用重启）。
- 这里是**兜底**：模型偶尔还是会端出一句客服腔，或者在被空 @ 的时候
  回一句「在呢在呢」。这些情况用确定性代码兜住，比反复改提示词可靠。

三件事
------
1. `load_style()`  读 persona.yaml 的 style 段（颜文字池 + 出现权重）。
2. `decorate()`    按权重挂一个颜文字。权重是**代码保证**的，
   不是让模型自己掷骰子——模型对「20% 概率」的理解完全不可靠。
3. `polish()`      削掉结尾的客服腔；空 @ 收到「在呢在呢」这类
   纯待命回复时，换成一句更像真人的口头话。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PERSONA_PATH = ROOT / "persona.yaml"

# 默认颜文字池。猫感为主，混一些通用网络颜文字。
DEFAULT_KAOMOJI = [
    "（｡･ω･｡）",
    "(=^･ω･^=)",
    "ヾ(≧▽≦*)o",
    "(๑•̀ㅂ•́)و✧",
    "_(:з」∠)_",
    "(*/ω＼*)",
    "Σ(っ°Д°;)っ",
    "(´･ω･`)",
    "( •̀ ω •́ )✧",
    "ヾ(•ω•`)o",
    "(≧∇≦)ﾉ",
    "๑乛◡乛๑",
    "(￣▽￣)",
    "(⊙_⊙)?",
    "ฅ(•ㅅ•❀)ฅ",
    "(=①ω①=)",
    "o(*￣▽￣*)ブ",
    "(๑•́ ₃ •̀๑)",
    "ヘ(￣ω￣ヘ)",
    "(๑¯ω¯๑)",
]

# 空 @ 时，如果模型只憋出一句客服腔，就用这些替代。
NUDGES = [
    "干嘛喵~",
    "嗯？咋啦",
    "欸，叫我？",
    "咋啦喵",
    "喵？说呀",
    "在的在的，说",
    "嗯哼？",
    "叫我干嘛，快说喵",
    "咋了咋了",
    "？说话喵",
]

# 「宣布换话题」的 AI 腔。
#
# 真人换话题是**直接换**，或者用「对了」「所以说」「哎」带一下；
# 不会先打个报告说「回正题」「言归正传」。实测模型会写出
#   「诶你这梗有点脏，我不接哈。回正题——伞塞包里没」
# 这个「回正题——」一眼就是 AI，所以既在 persona 里禁掉，
# 这里再兜一层：命中就换成真人用的连接词，连同后面的破折号一起吃掉。
_AI_TOPIC_SHIFT = re.compile(
    # 前缀要把「让我们」放前面，否则会被「让我」先匹配、剩下一个「们」
    r"(?:让我们|让我|我们|咱们)?(?:回(?:到|归|复)?|拉回|说回|转回)"
    # 择一顺序：长词必须写在前面，否则「话题」会先被「话」吃掉、留下一个「题」
    r"(?:话题|正题|正事|主题|原话题|刚才(?:的)?(?:那个)?(?:话题|话|事)?)"
    r"|言归正传|书归正传|话归正传"
)
_TOPIC_CONNECTORS = ["对了，", "哎对了，", "所以说，", "那什么，", "对了，"]
# 换话题说法后面常挂着的虚词尾巴（「回到正事**上**」「言归正传**了**」）
_TOPIC_TAIL_PARTICLES = "上来了啊呢吧"
_TOPIC_TAIL_PUNCT = "—－-–~～:：，, 、\t "

# 「像客服待命」的回复。只在**空 @** 时判断，所以不会误伤正常回答。
# 做法：先必须命中一个「待命核心词」，再把核心词抠掉，
# 剩下的如果全是「呢/哦/呀/我/在/好/标点」这类填充字，就认定是待命腔。
# 这样「在呢在呢」「嗯嗯，我在」「好，有什么需要随时找我」都命中，
# 而「哈哈哈哈哈」「好呀，我记住了」「在的，我看看」不会。
_STANDBY_CORE = re.compile(
    r"(?:我?在(?:呢|哦|呀|啊|的)?|在的|怎么(?:了|啦)|需要我?帮|需要帮忙|有什么需要"
    r"|可以帮|有啥需要|能帮|帮你|帮忙|帮助|请问|请讲|请说|随时|待命|恭候|听着呢"
    r"|找我|问我|叫我)"
)
_STANDBY_FILLER = re.compile(
    r"^[呢哦呀啊吗的了嗯哈我在这好是您～~，,。.、！!？?\s"
    r"请问有什么你帮可以能需要话说的]*$"
)

# 结尾的客服腔。命中就削掉；削完空了就保留原文（宁可留着也别发空消息）。
_TRAILING_BOILERPLATE = [
    re.compile(r"[，,。.;；!！~～\s]*(有(什么|啥)?(需要|问题|事)[^。！？!?]{0,8}"
               r"(随时|尽管|都)?(找我|问我|跟我说|说一声)[^。！？!?]{0,6})[。！？~～!?]*$"),
    re.compile(r"[，,。.;；!！~～\s]*(随时(找我|叫我|问我|说|喊我)[^。！？!?]{0,6})[。！？~～!?]*$"),
    re.compile(r"[，,。.;；!！~～\s]*(有事(随时)?(找我|说|喊我)[^。！？!?]{0,6})[。！？~～!?]*$"),
    re.compile(r"[，,。.;；!！~～\s]*(需要帮助[^。！？!?]{0,8})[。！？~～!?]*$"),
    re.compile(r"[，,。.;；!！~～\s]*((查(天气|资料)|搜(索|资料)|闲聊|唠嗑|聊天)"
               r"[^。！？!?]{0,12})[。！？~～!?]*$"),
]

# 判定「已经有颜文字了」的特征字符（颜文字里常见、正常聊天很少用的符号）。
_KAO_SIG = set("｡･ω･^∀≧▽≦๑з´〃・ฅㅅ①ㅂ눈╥ゝヘ彡")


@dataclass
class StyleConfig:
    enabled: bool = True
    kaomoji_rate: float = 0.2
    kaomoji: list[str] = field(default_factory=lambda: list(DEFAULT_KAOMOJI))
    nudges: list[str] = field(default_factory=lambda: list(NUDGES))
    strip_boilerplate: bool = True


def load_style(path: Path | str = PERSONA_PATH) -> StyleConfig:
    """读 persona.yaml 的 style 段。读不到就用内置默认值。"""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (FileNotFoundError, yaml.YAMLError):
        return StyleConfig()

    block = raw.get("style") or {}
    if not isinstance(block, dict):
        return StyleConfig()

    pool = block.get("kaomoji") or []
    pool = [str(k).strip() for k in pool if str(k).strip()] or list(DEFAULT_KAOMOJI)
    nudges = block.get("nudges") or []
    nudges = [str(n).strip() for n in nudges if str(n).strip()] or list(NUDGES)

    try:
        rate = float(block.get("kaomoji_rate", 0.2))
    except (TypeError, ValueError):
        rate = 0.2

    return StyleConfig(
        enabled=bool(block.get("enabled", True)),
        kaomoji_rate=min(1.0, max(0.0, rate)),
        kaomoji=pool,
        nudges=nudges,
        strip_boilerplate=bool(block.get("strip_boilerplate", True)),
    )


def _has_kaomoji(text: str) -> bool:
    return any(ch in _KAO_SIG for ch in text)


def _append_kaomoji(text: str, kao: str) -> str:
    if not text:
        return kao
    if text[-1] in "。！？!?~～":
        return f"{text}{kao}"
    return f"{text} {kao}"


def decorate(reply: str, style: StyleConfig, rng: random.Random | None = None) -> str:
    """按权重挂颜文字。权重由代码掷骰子，保证长期比例稳定。"""
    text = (reply or "").strip()
    if not text or not style.enabled or style.kaomoji_rate <= 0:
        return text
    if _has_kaomoji(text):
        return text
    # 太长的回复后面挂个颜文字很怪，跳过（这种情况本来就少）。
    if len(text) > 120:
        return text

    rng = rng or random
    if rng.random() >= style.kaomoji_rate:
        return text
    return _append_kaomoji(text, rng.choice(style.kaomoji))


def fix_topic_shift(text: str, rng: random.Random | None = None) -> str:
    """把「回正题」这类宣布换话题的说法，换成真人用的连接词。

    「诶你这梗有点脏，我不接哈。回正题——伞塞包里没」
        -> 「诶你这梗有点脏，我不接哈。对了，伞塞包里没」
    """
    if not text:
        return text
    rng = rng or random
    if not _AI_TOPIC_SHIFT.search(text):
        return text

    out: list[str] = []
    i = 0
    while True:
        m = _AI_TOPIC_SHIFT.search(text, i)
        if not m:
            out.append(text[i:])
            break
        out.append(text[i:m.start()])
        j = m.end()
        # 先吃掉虚词尾巴（回到正事**上**），再吃掉分隔符（—— / ： / ，）
        k = 0
        while j < len(text) and k < 2 and text[j] in _TOPIC_TAIL_PARTICLES:
            j += 1
            k += 1
        while j < len(text) and text[j] in _TOPIC_TAIL_PUNCT:
            j += 1
        if not re.search(r"[\w\u4e00-\u9fff]", text[j:]):
            # 这半句就在句尾，后面只剩标点了 —— 补个「对了，」会变成「对了，。」
            # 这种悬空连接词，直接删掉，顺手把前面多余的分隔符也收掉
            while out and out[-1] and out[-1][-1] in "，,、；; ":
                out[-1] = out[-1][:-1]
            i = j
            continue
        out.append(rng.choice(_TOPIC_CONNECTORS))
        i = j
    fixed = "".join(out)
    # 连接词前面如果已经紧挨着标点，避免出现「。，对了」
    fixed = re.sub(r"([。！？!?…~])[，,]\s*", r"\1", fixed)
    # 兜底：削完一个实词都不剩（原文就只有「回正题。」这种），保留原文
    if not re.search(r"[\w\u4e00-\u9fff]", fixed):
        return text
    return fixed


def _is_standby(text: str) -> bool:
    """整句都是「我在 / 请讲 / 有什么可以帮你」这类客服待命腔。"""
    text = text.strip()
    if not text or len(text) > 20:
        return False
    if not _STANDBY_CORE.search(text):
        return False
    return bool(_STANDBY_FILLER.match(_STANDBY_CORE.sub("", text)))


def polish(reply: str, style: StyleConfig, *, empty_trigger: bool = False,
           rng: random.Random | None = None) -> str:
    """削客服腔；空 @ 收到待命腔时换成一句口头话。"""
    text = (reply or "").strip()
    if not text:
        return text
    rng = rng or random

    # 空 @ 却回了个「在呢在呢」——这是最像机器人的一种，直接换掉。
    if empty_trigger and _is_standby(text):
        return rng.choice(style.nudges)

    if not style.strip_boilerplate:
        return text

    for pattern in _TRAILING_BOILERPLATE:
        match = pattern.search(text)
        if not match:
            continue
        stripped = text[:match.start()].strip()
        if not stripped:
            # 整句都是客服腔。空 @ 就换一句口头话，否则宁可原样发出去。
            if empty_trigger and style.nudges:
                return rng.choice(style.nudges)
            return text
        if match.start() == 0:
            # 从开头就开始削，说明这半句也是套话，交给别的规则处理
            continue
        return stripped.rstrip("，,、;； ")
    return text


def apply_style(reply: str, style: StyleConfig, *, empty_trigger: bool = False,
                rng: random.Random | None = None) -> str:
    """完整流程：去人机腔 -> 换掉 AI 式换话题 -> 按权重挂颜文字。"""
    text = polish(reply, style, empty_trigger=empty_trigger, rng=rng)
    if style.strip_boilerplate:
        text = fix_topic_shift(text, rng=rng)
    return decorate(text, style, rng=rng)
