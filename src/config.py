"""配置与人设加载。

config.yaml  —— 安全策略、频率、历史记录保留策略（改完需重启）
persona.yaml —— 人设 / 性格 / 语气（每次请求实时读取，改完立即生效）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
PERSONA_PATH = ROOT / "persona.yaml"
DATA_DIR = ROOT / "data"


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass
class DshConfig:
    command: str
    profile: str = "wechat"
    timeout_seconds: int = 180
    #: 闲聊用哪个模型。「切换」是**手动**的，不做任何自动判断。
    #:   deepseek = profile 里配的（现在是 DSH 桌面账号的 deepseek-flash，按量计费）
    #:   zhipu    = 智谱 GLM-4.7-Flash（官方定价输入/输出都是「免费」）
    #: 实现方式见 src/model_preset.py：给 dsh 加一层 --patch 叠加层。
    model_preset: str = "deepseek"
    #: 叠加层放哪。空 = 用默认的 <项目>\dsh\ 目录。
    preset_dir: str = ""
    #: 智谱的 Key。**不进公开仓库**（config.yaml 是 gitignore 的）。
    #: 留空时回退到环境变量 ZAI_API_KEY。
    zhipu_api_key: str = ""


@dataclass
class ReadLimit:
    """组装上下文时的读取上限（独立于存储上限）。"""

    max_messages: int = 50
    max_chars: int = 6000
    max_age_days: int = 3


@dataclass
class HistoryConfig:
    enabled: bool = True
    retention: str = "weekly"          # daily | weekly | forever
    retention_days: int = 7
    per_group_max: int = 500
    purge_interval_min: int = 30
    reset_on_expiry: bool = True
    # 组装上下文时，历史里每条消息最多保留多少字（超长截断，省 token）
    per_message_chars: int = 120
    read_limit: ReadLimit = field(default_factory=ReadLimit)

    @property
    def effective_retention_days(self) -> int | None:
        """实际保留天数。None 表示永久保留。"""
        mode = (self.retention or "weekly").lower()
        if mode == "forever":
            return None
        if mode == "daily":
            return 1
        return max(1, int(self.retention_days))


@dataclass
class RateLimitConfig:
    per_group_per_hour: int = 20
    min_interval_seconds: int = 8
    reply_delay: tuple[float, float] = (3.0, 8.0)


@dataclass
class AggregateConfig:
    """连续消息聚合。

    被 @ 之后**不马上回复**，而是继续观察这个群的消息：
    对方经常是「@机器人」发一条、再把真正的问题分几条发出来。
    在 quiet_seconds 秒内没有新消息（或触到 max_wait_seconds /
    max_messages 上限）时，把这些消息当成**同一次发言**整体理解，
    只回复一条。
    """

    enabled: bool = True
    quiet_seconds: float = 6.0        # 静默多久算「说完了」
    max_wait_seconds: float = 25.0    # 最长等待（对方一直发也会回）
    max_messages: int = 8             # 一次最多聚合几条
    # 后续消息的吸收范围：
    #   any  = 群里任何人在这期间说的话都纳入（更像真人在听整段对话）
    #   same = 只有触发 @ 的那个人说的才纳入（更保守）
    followup_sender: str = "any"
    # 机器人回复完之后，多长时间内「接着聊」可以不再 @。
    # 不加这个的话，被 @ 一次之后对方再补一句（「nmsl」「那咋了」）就完全没反应，
    # 看起来像死机。0 = 关掉，每句都必须 @。
    after_reply_window: float = 120.0
    # 窗口里谁的话算接着聊：same = 只有刚才触发的那个人（推荐），any = 谁都算
    after_reply_sender: str = "same"


@dataclass
class LaunchConfig:
    """`\\开 <程序>`：远程启动需要管理员权限的程序。

    走的是预授权提权通道（tools/elevated/run.ps1 的 launch 动作），
    **不过 LLM** —— 一条指令直达，几秒返回，不花 token、也不会卡。

    程序名必须在那份脚本的 `$programs` 表里（路径写死，调用方只能选名字）。
    这里只放指令词和开关，程序列表不重复维护 —— 那边的脚本才是权威。
    """

    enabled: bool = True
    words: list[str] = field(default_factory=lambda: ["开", "打开", "开个", "开一下", "帮我开", "launch", "open"])


@dataclass
class BrushConfig:
    """`\\刷课 <课程>`：在自己的浏览器里打开课程页。

    **故意不走 LLM。** 刷课这件事是确定性的：打开对的页面，剩下交给油猴脚本。
    走 agent 的代价是每刷一次十几秒、几十步工具调用、几千 token，
    而且 agent 起的浏览器会在指令结束时被回收（这个已经实测确认过），
    它为了拿到调试口还会去杀用户的浏览器 —— 全都绕着这一个「打开页面」转。

    所以这里只做一件事：用系统默认浏览器打开一个 URL。
    浏览器是用户自己的进程，不会被回收；油猴脚本照常工作；
    不碰任何浏览器配置，也没有调试口要守。
    """

    enabled: bool = True
    words: list[str] = field(default_factory=lambda: ["刷课", "上课", "挂课"])
    #: 浏览器可执行文件。留空 = 让系统按 URL 关联自己去开（最省事，也不挑浏览器）。
    browser: str = ""
    #: [{"name": "舞台人生", "url": "https://..."}] —— 名字用 Substring 匹配，
    #: 「刷课 舞台」就能命中「舞台人生」。
    courses: list[dict] = field(default_factory=list)


@dataclass
class EnkaConfig:
    """`\\uid [游戏] <UID>`：查米哈游账号的**公开角色展柜**（Enka.Network）。

    **不走 LLM。** 确定的事一次 HTTP 就够了 —— 和 `\\开` / `\\刷课` 同一个思路：
    走 agent 要多花一次模型调用、几十秒，而且 agent 没法遵守这个 API 的规矩。

    Enka 的作者在文档里写了几条硬规矩，配置项就是照着它设的：
      · 不许枚举 UID、不许批量查 —— 所以没有「遍历」这类东西
      · 必须带自定义 User-Agent（写死在 src/enka.py 里）
      · UID 端点有动态限流 —— 所以有 min_interval_seconds / max_per_hour
      · 响应里的 ttl 期间要用本地缓存 —— 所以有 data/enka_cache.json
    """

    enabled: bool = True
    words: list[str] = field(default_factory=lambda: ["查询", "uid", "查询uid", "战绩"])
    #: 群友能不能用。True = 群里 `@机器人 /查询 原神 618285856` 直接能查，
    #: **不需要口令、也不看授权名单**。原因是这个 API 的数据本来就是公开的，
    #: 而群友根本不知道口令。关掉之后只有授权者能用 `\\uid`。
    allow_group_members: bool = True
    #: 公开指令的前缀。**刻意和群控制指令的 `\\` 分开**：
    #: `\` 开头一律要授权 + 口令，`/` 开头是「谁都能问的公开查询」。
    prefix: str = "/"
    #: `\\uid` 不带参数时查谁（填你自己的 UID 就不用每次打）
    default_uid: str = ""
    #: 默认游戏。auto = 自动识别（按 gi -> hsr -> zzz 依次试）
    default_game: str = "auto"
    timeout_seconds: int = 20
    #: 两次查询之间至少隔多久。**按「一次用户查询」算**，不是按内部尝试算
    min_interval_seconds: float = 3.0
    #: 每小时最多发几次真实请求（跨重启累计）
    max_per_hour: int = 60


@dataclass
class LogsConfig:
    """日志的定期清理。

    日志会一直涨（bot.log 每次重启都追加），加上控制台输出、
    审计日志、截图目录，data/ 几个月就能到几百 MB。
    """

    cleanup_enabled: bool = True
    #: 超过这么多天的 *.log 删掉。0 表示不删
    keep_days: float = 14.0
    #: 多久检查一次（小时）。低频就够，删日志不着急
    check_hours: float = 6.0


@dataclass
class PollConfig:
    """轮询节奏。漏消息时先动这里。"""

    #: 每几轮做一次全量读取（1 = 每轮都读，不会漏消息，但频繁抢微信窗口）
    full_scan_every: int = 6

    #: 每次读取前用 LoadMoreCache 多加载几次历史。
    #: 微信窗口默认只装最近十几条，机器人自己发的截图会把尚未处理的用户消息
    #: 挤出窗口（实测：用户连发 4 条只收到 1 条回复）。装载更多 = 根治。
    #: 代价是每次读取更慢、微信会上滚，所以别调太大。
    load_more_times: int = 2


@dataclass
class AutoUpdateConfig:
    """梗词库的定时联网更新。

    梗活得短，手工维护必然烂掉。让机器人定期用带联网搜索的 profile 去捞新的，
    写进 data/memes_user.yaml（叠加层），**不碰** memes.yaml。
    """

    enabled: bool = True
    interval_days: float = 14.0    # 多久捞一次
    max_new: int = 20              # 一次最多收几条，防灌爆提示词
    check_minutes: int = 120       # 运行时多久检查一次该不该捞


@dataclass
class CatchUpConfig:
    """开机补回：机器人停机期间错过的 @ 消息，重启后补答。

    只补「明显没被回复过」的：窗口最后一条机器人自己的发言之后、
    并且包含 @ 机器人的消息。超过上限或超出回看范围的不补，避免翻旧账刷屏。
    """

    enabled: bool = True
    lookback_messages: int = 20   # 只看窗口最后 N 条
    max_per_group: int = 3        # 每次启动每群最多补几条


@dataclass
class ThreadConfig:
    """按人分开的连续对话：被 @ 一次之后，那个人接着说话不用再 @。

    相关性判定是纯启发式的（字符二元组 + 「是不是对别人说的」信号），
    **不额外调用模型**，所以判断成本是零。
    """

    enabled: bool = True
    window_seconds: float = 600.0   # 多久没说话算这个话题结束
    max_turns: int = 6              # 一个人最多连续聊几轮就收住
    threshold: float = 1.5          # 保留字段（现在判定是"默认接、只排除明确不相关"）
    long_chars: int = 40            # 超过这么多字又和上文零重合，判为在跟别人说
    merge_window: float = 25.0      # 这么多秒内别人的话若话题相关，并到同一次回答
    # 群里最后一条消息是机器人说的 —— 那下一个开口的人几乎一定是在跟它说话。
    # 这条能救回「我 @ 完它、过一会儿才想起来再说一句」，而且一有人插话就立刻失效。
    answer_when_last: bool = True
    dangling_window: float = 900.0  # 上面那条规则的时限（秒）
    persist: bool = True            # 线程存盘，重启不丢


@dataclass
class MoodConfig:
    """心情值 / 好感度：按人持久化，影响之后的语气。

    数值变化由模型在同一次回复末尾附带 [mood:N] 决定，不额外多花一次调用。
    """

    enabled: bool = True
    initial: int = 100
    floor: int = 1
    ceiling: int = 100
    max_down: int = 15              # 单次最多掉多少，防止一句话把好感打到底
    max_up: int = 8
    neutral: int = 70               # 高于这个值不注入额外语气说明
    angry: int = 35                 # 低于这个值时语气明显变冷
    show_in_status: bool = True     # 群内 \\状态 里是否列出偏低的人


@dataclass
class AgentConfig:
    """群内 `\\agent` 指令：把一条自然语言指令交到本机 agent 手上执行。

    ⚠ 这是「微信群里的一条远程操作通道」。能在群里发这条指令的人，
    等于能在这台机器上读文件、改文件、跑命令，中间没有任何隔离层。

    所以默认关闭，而且开启时**强制**要求 commands.secret 非空：
    群昵称谁都能改，昵称那层挡不住冒用，口令是唯一的第二因素。
    """

    enabled: bool = False               # 默认关闭
    profile: str = "wechat-agent"       # 有完整工具链的 DSH profile
    words: list[str] = field(default_factory=lambda: ["agent", "执行", "run", "操作"])
    require_secret: bool = True         # 强制口令，关掉这个等于把机器交给群
    groups: list[str] = field(default_factory=list)   # 只在这些群生效；空=全部白名单群
    hours: list[int] = field(default_factory=list)    # [起, 止)，如 [8, 23]；空=不限
    timeout_seconds: int = 600
    max_reply_chars: int = 1200         # 结果太长就截断，群里没人看长报告
    cwd: str = ""                       # 留空 = 项目目录
    ack: bool = True                    # 先回一句「收到，开始弄」
    audit_file: str = "data/agent_audit.jsonl"


@dataclass
class ShotConfig:
    """群内 `\\截图` 指令。

    ⚠ 截图会把整块屏幕拍下来。发到群里 = 全群都看得到你当时开着什么窗口。
    `target: private` 可以改成私聊发给指定的人。
    """

    enabled: bool = True
    words: list[str] = field(default_factory=lambda: ["截图", "截屏", "截个图", "截张图", "截一下图", "截个屏", "截一下屏", "屏幕截图", "屏幕截屏", "看下屏幕", "看看屏幕", "screenshot", "screen", "screencap"])
    target: str = "same"        # same = 发回指令所在的聊天；private = 私聊发给 private_to
    private_to: str = ""        # target=private 时的收件人（微信里显示的昵称）
    max_width: int = 0          # 0 = 原分辨率发送（不缩放）；填数字就缩到那个宽度
    format: str = "PNG"         # PNG | JPG
    quality: int = 82           # format=JPG 时的质量
    keep_files: int = 20        # 本地最多留几张截图
    require_secret: bool = False
    timeout_seconds: int = 30


@dataclass
class SplitConfig:
    """把一段长回复拆成几条发。微信群里没人会发一整屏文字。"""

    enabled: bool = True
    min_chars: int = 50           # 短于这个长度不拆
    max_chars: int = 60           # 想控制在每条多少字以内（用来算该拆几段）
    max_parts: int = 3            # 最多拆几条
    min_part: int = 6             # 比这更短的碎片并回上一段
    delay: tuple[float, float] = (1.5, 4.0)   # 段与段之间的随机间隔


@dataclass
class MemeConfig:
    """网络热梗库。命中才注入，不命中不花 token。"""

    enabled: bool = True
    file: str = "memes.yaml"
    user_file: str = "data/memes_user.yaml"
    max_entries: int = 6          # 一次最多注入几个梗
    max_chars: int = 420          # 注入内容的字数上限
    scan_history: bool = True     # 除了当前消息，也扫最近聊天记录
    history_window: int = 8       # 只扫最近这几条（扫全部会导致每轮都注入）
    max_history_hits: int = 2     # 历史里命中的最多注入几个，当前消息里的优先
    learn_hint: bool = False      # 把疑似新梗记到日志里，方便手工补库


@dataclass
class CommandsConfig:
    """群内指令：消息里出现 prefix（默认两个反斜杠）即为指令。

    ## 身份怎么认

    微信里同一个人有好几个名字，可靠程度差很多：

    | 来源 | 群里其他人看得到吗 | 自己能改吗 | 可靠程度 |
    |---|---|---|---|
    | 群昵称（msg.sender 常见值） | 看得到 | 能改 | 最弱 |
    | 备注 display_name | **看不到**（只有机器人账号自己看得到） | 改不了 | 中 |
    | 微信昵称 nickname | 看得到 | 能改 | 弱 |
    | **微信号 id** | 基本看不到 | **改不了** | **最强** |

    机器人账号给「大号」设的备注，群成员不知道字符串是什么，所以拿它当口令
    比群昵称强得多。但冒用者只要把自己的群昵称改成同一个字符串，
    `msg.sender` 也会等于那个串 —— 所以真正防冒用要靠微信号，
    也就是 `identity_mode: id` 那个严格模式。
    """

    enabled: bool = True
    # 单个反斜杠。原来是两个（`\\agent`），改成单个更好敲，
    # 群里 `\截图` / `\agent xxx` 都能用。改这个等于改群里的指令写法，
    # 老配置里如果写着 '\\' 就还是两个，以配置为准。
    prefix: str = "\\"
    # 简写前缀：**只认开头**。
    # 用 / 是因为好敲，但也正因为 / 在正常聊天里太常见（路径、网址、分数），
    # 所以它只在可信会话里生效；群里一律走 prefix + 口令。
    short_prefix: str = "/"
    # 可信会话：这些会话里的消息**不用 @**、指令**不用口令**、也不走线程判断。
    #
    # **只该填你自己的私聊。** 私聊的发送者没法冒充，所以免口令是安全的。
    # 千万别把群名填进来：identity_mode=any 时群昵称谁都能改，
    # 群里免口令 = 把机器人的完整控制权交给任何知道你代号的人。
    # （代码里对这件事会打警告日志。）
    trusted_chats: list[str] = field(default_factory=list)
    # 授权名单。可以混写微信号、备注、微信昵称、群昵称。
    # 强烈建议填**微信号**（改不了、群里看不到）。
    allowed_senders: list[str] = field(default_factory=list)
    # 口令：留空表示不校验（只靠身份 + \\ 两重检查）。
    # 填上之后，指令正文里必须包含这个口令才会执行。
    # 注意：口令一旦在群里发过就等于公开，群消息所有成员都看得到。
    secret: str = ""
    # 身份校验方式：
    #   any = 任何别名（群昵称／备注／微信昵称／微信号）对上就放行（宽松）
    #   id  = 只认微信号，且必须真的读到才放行（严格，防冒用）
    identity_mode: str = "any"
    # 用 sender_info() 解析身份。只在指令消息上做（约 400ms，点一下头像取资料），
    # 结果按 (群, 发送者) 缓存。普通聊天不走这条路，不增加开销。
    verify_identity: bool = True
    # 重启机器人服务。**注意别和 start_words 撞**：重启是「退出再拉起」，
    # 恢复只是解除暂停，两回事。
    restart_words: list[str] = field(
        default_factory=lambda: ["restart", "重启", "重开"])
    stop_words: list[str] = field(default_factory=lambda: ["stop", "停止", "停", "暂停"])
    start_words: list[str] = field(default_factory=lambda: ["start", "恢复", "启动", "继续"])
    status_words: list[str] = field(default_factory=lambda: ["status", "状态"])
    # 教机器人认梗：  \\梗 add ccb | 踩踩背那个梗，顺着演别解释
    meme_words: list[str] = field(default_factory=lambda: ["梗", "meme", "gag"])
    reply_confirm: bool = True                 # 执行后是否在群里回一句确认


@dataclass
class Config:
    dsh: DshConfig
    mode: str
    whitelist_groups: list[str]
    bot_names: list[str]
    require_at: bool
    ignore_self: bool
    rate_limit: RateLimitConfig
    aggregate: AggregateConfig
    split: SplitConfig
    memes: MemeConfig
    memes_update: AutoUpdateConfig
    launch: LaunchConfig
    brush: BrushConfig
    enka: EnkaConfig
    poll: PollConfig
    logs: LogsConfig
    threads: ThreadConfig
    mood: MoodConfig
    agent: AgentConfig
    screenshot: ShotConfig
    catch_up: CatchUpConfig
    commands: CommandsConfig
    history: HistoryConfig
    dedup_window_seconds: int
    log_level: str
    log_dir: Path
    data_dir: Path
    db_path: Path

    @property
    def auto_send(self) -> bool:
        return self.mode == "auto"


# --------------------------------------------------------------------------
# 加载
# --------------------------------------------------------------------------
def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key) or {}
    if not isinstance(value, dict):
        raise ValueError(f"config.yaml 的 {key} 必须是映射（key: value）")
    return value


def parse_courses(raw_courses) -> list[dict]:
    """课程表。**接受两种写法**，返回统一的 [{"name":…, "url":…}]。

    为什么有两种：设置界面只有一个「一行一项」的文本域（schema 的 list 类型），
    它只能存字符串列表；而手写 config.yaml 时用对象更清楚。
    所以：

        courses:
          - 舞台人生 | https://mooc2-ans.chaoxing.com/...     # 界面用的写法
          - name: 高等数学                                    # 手写用的写法
            url: https://...

    分隔符 `|`（也认全角 `｜`）。名字在前、url 在后。
    两种写法都坏掉的行会被**跳过**，不影响其它课程 ——
    一条写错的课程不该让整条 \\刷课 指令不可用。
    """
    out: list[dict] = []
    for item in raw_courses or []:
        name = url = ""
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            url = str(item.get("url", "")).strip()
        elif isinstance(item, str):
            line = item.strip()
            if not line:
                continue
            # 全角竖线也认 —— 中文输入法下很容易打出来
            for sep in ("|", "｜"):
                if sep in line:
                    name, _, url = line.partition(sep)
                    name, url = name.strip(), url.strip()
                    break
            else:
                # 没有分隔符：整行当 url，用 url 当名字（至少 \\刷课 <url片段> 能命中）
                name = url = line
        else:
            continue
        if name and url:
            out.append({"name": name, "url": url})
    return out


def _read_delay(rl_raw: dict[str, Any]) -> tuple[float, float]:
    """回复前的随机延迟区间。

    优先读 reply_delay_min / reply_delay_max 两个标量（设置界面更好改），
    没有就回退到老写法 reply_delay: [3, 8]。
    """
    if "reply_delay_min" in rl_raw or "reply_delay_max" in rl_raw:
        return (float(rl_raw.get("reply_delay_min", 3) or 0),
                float(rl_raw.get("reply_delay_max", 8) or 0))

    delay = rl_raw.get("reply_delay") or [3, 8]
    if not isinstance(delay, (list, tuple)) or len(delay) != 2:
        raise ValueError(
            "config.yaml 的 rate_limit.reply_delay 必须是两个数字，如 [3, 8]；"
            "或改用 reply_delay_min / reply_delay_max"
        )
    return (float(delay[0]), float(delay[1]))


def load_config(path: Path | str = CONFIG_PATH) -> Config:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    dsh_raw = _section(raw, "dsh")
    if not dsh_raw.get("command"):
        raise ValueError("config.yaml 缺少 dsh.command")

    hist_raw = _section(raw, "history")
    read_raw = hist_raw.get("read_limit") or {}
    history = HistoryConfig(
        enabled=bool(hist_raw.get("enabled", True)),
        retention=str(hist_raw.get("retention", "weekly")),
        retention_days=int(hist_raw.get("retention_days", 7)),
        per_group_max=int(hist_raw.get("per_group_max", 500)),
        purge_interval_min=int(hist_raw.get("purge_interval_min", 30)),
        reset_on_expiry=bool(hist_raw.get("reset_on_expiry", True)),
        per_message_chars=int(hist_raw.get("per_message_chars", 120)),
        read_limit=ReadLimit(
            max_messages=int(read_raw.get("max_messages", 50)),
            max_chars=int(read_raw.get("max_chars", 6000)),
            max_age_days=int(read_raw.get("max_age_days", 3)),
        ),
    )

    rl_raw = _section(raw, "rate_limit")

    agg_raw = _section(raw, "aggregate")
    follow = str(agg_raw.get("followup_sender", "any")).strip().lower()
    if follow not in {"any", "same"}:
        raise ValueError("config.yaml 的 aggregate.followup_sender 只能是 any 或 same")
    aggregate = AggregateConfig(
        enabled=bool(agg_raw.get("enabled", True)),
        quiet_seconds=float(agg_raw.get("quiet_seconds", 6) or 0),
        max_wait_seconds=float(agg_raw.get("max_wait_seconds", 25) or 0),
        max_messages=max(1, int(agg_raw.get("max_messages", 8))),
        followup_sender=follow,
        after_reply_window=float(agg_raw.get("after_reply_window", 120) or 0),
        after_reply_sender=str(agg_raw.get("after_reply_sender", "same")).strip().lower(),
    )
    if aggregate.after_reply_sender not in {"any", "same"}:
        raise ValueError("config.yaml 的 aggregate.after_reply_sender 只能是 any 或 same")

    mode = str(raw.get("mode", "draft")).lower()
    if mode not in {"draft", "auto"}:
        raise ValueError("config.yaml 的 mode 只能是 draft 或 auto")

    cu_raw = _section(raw, "catch_up")
    catch_up = CatchUpConfig(
        enabled=bool(cu_raw.get("enabled", True)),
        lookback_messages=int(cu_raw.get("lookback_messages", 20)),
        max_per_group=int(cu_raw.get("max_per_group", 3)),
    )

    sp_raw = _section(raw, "split")
    sp_delay = sp_raw.get("delay") or [1.5, 4.0]
    if not isinstance(sp_delay, (list, tuple)) or len(sp_delay) != 2:
        raise ValueError("config.yaml 的 split.delay 必须是两个数字，如 [1.5, 4.0]")
    split = SplitConfig(
        enabled=bool(sp_raw.get("enabled", True)),
        min_chars=int(sp_raw.get("min_chars", 50)),
        max_chars=max(20, int(sp_raw.get("max_chars", 60))),
        max_parts=max(1, int(sp_raw.get("max_parts", 3))),
        min_part=int(sp_raw.get("min_part", 6)),
        delay=(float(sp_delay[0]), float(sp_delay[1])),
    )

    mem_raw = _section(raw, "memes")
    memes = MemeConfig(
        enabled=bool(mem_raw.get("enabled", True)),
        file=str(mem_raw.get("file", "memes.yaml")),
        user_file=str(mem_raw.get("user_file", "data/memes_user.yaml")),
        max_entries=max(1, int(mem_raw.get("max_entries", 6))),
        max_chars=max(80, int(mem_raw.get("max_chars", 420))),
        scan_history=bool(mem_raw.get("scan_history", True)),
        history_window=max(0, int(mem_raw.get("history_window", 8))),
        max_history_hits=max(0, int(mem_raw.get("max_history_hits", 2))),
        learn_hint=bool(mem_raw.get("learn_hint", False)),
    )

    logs_raw = _section(raw, "logs")
    logs = LogsConfig(
        cleanup_enabled=bool(logs_raw.get("cleanup_enabled", True)),
        keep_days=float(logs_raw.get("keep_days", 14) or 0),
        check_hours=float(logs_raw.get("check_hours", 6) or 6),
    )

    poll_raw = _section(raw, "poll")
    poll = PollConfig(
        full_scan_every=max(1, int(poll_raw.get("full_scan_every", 6))),
        load_more_times=max(0, int(poll_raw.get("load_more_times", 2))),
    )

    la_raw = _section(raw, "launch")
    launch = LaunchConfig(
        enabled=bool(la_raw.get("enabled", True)),
        words=list(la_raw.get("words") or ["开", "打开", "开个", "开一下", "帮我开", "launch", "open"]),
    )

    br_raw = _section(raw, "brush")
    brush = BrushConfig(
        enabled=bool(br_raw.get("enabled", True)),
        words=[str(w) for w in
               (br_raw.get("words") or ["刷课", "上课", "挂课"])],
        browser=str(br_raw.get("browser", "") or ""),
        # 两种写法都收（「名字 | url」字符串 / name+url 对象），见 parse_courses
        courses=parse_courses(br_raw.get("courses")),
    )

    en_raw = _section(raw, "enka")
    enka = EnkaConfig(
        enabled=bool(en_raw.get("enabled", True)),
        words=[str(w) for w in
               (en_raw.get("words") or ["查询", "uid", "查询uid", "战绩"])],
        allow_group_members=bool(en_raw.get("allow_group_members", True)),
        prefix=str(en_raw.get("prefix", "/") or "/"),
        default_uid=str(en_raw.get("default_uid", "") or "").strip(),
        default_game=str(en_raw.get("default_game", "auto") or "auto").strip().lower(),
        timeout_seconds=int(en_raw.get("timeout_seconds", 20)),
        min_interval_seconds=float(en_raw.get("min_interval_seconds", 3) or 0),
        max_per_hour=max(0, int(en_raw.get("max_per_hour", 60) or 0)),
    )

    mu_raw = _section(raw, "memes_update")
    memes_update = AutoUpdateConfig(
        enabled=bool(mu_raw.get("enabled", True)),
        interval_days=float(mu_raw.get("interval_days", 14) or 0),
        max_new=max(1, int(mu_raw.get("max_new", 20))),
        check_minutes=max(5, int(mu_raw.get("check_minutes", 120))),
    )

    th_raw = _section(raw, "threads")
    threads = ThreadConfig(
        enabled=bool(th_raw.get("enabled", True)),
        window_seconds=float(th_raw.get("window_seconds", 180) or 0),
        max_turns=max(1, int(th_raw.get("max_turns", 6))),
        threshold=float(th_raw.get("threshold", 1.5)),
        long_chars=max(10, int(th_raw.get("long_chars", 40))),
        answer_when_last=bool(th_raw.get("answer_when_last", True)),
        dangling_window=float(th_raw.get("dangling_window", 900) or 0),
        persist=bool(th_raw.get("persist", True)),
        merge_window=float(th_raw.get("merge_window", 25) or 0),
    )

    md_raw = _section(raw, "mood")
    mood = MoodConfig(
        enabled=bool(md_raw.get("enabled", True)),
        initial=int(md_raw.get("initial", 100)),
        floor=int(md_raw.get("floor", 1)),
        ceiling=int(md_raw.get("ceiling", 100)),
        max_down=max(0, int(md_raw.get("max_down", 15))),
        max_up=max(0, int(md_raw.get("max_up", 8))),
        neutral=int(md_raw.get("neutral", 70)),
        angry=int(md_raw.get("angry", 35)),
        show_in_status=bool(md_raw.get("show_in_status", True)),
    )

    ag_raw = _section(raw, "agent")
    agent = AgentConfig(
        enabled=bool(ag_raw.get("enabled", False)),
        profile=str(ag_raw.get("profile", "wechat-agent")),
        words=[str(w) for w in (ag_raw.get("words") or AgentConfig().words)],
        require_secret=bool(ag_raw.get("require_secret", True)),
        groups=[str(g) for g in (ag_raw.get("groups") or [])],
        hours=[int(h) for h in (ag_raw.get("hours") or [])],
        timeout_seconds=max(30, int(ag_raw.get("timeout_seconds", 600))),
        max_reply_chars=max(80, int(ag_raw.get("max_reply_chars", 1200))),
        cwd=str(ag_raw.get("cwd", "") or ""),
        ack=bool(ag_raw.get("ack", True)),
        audit_file=str(ag_raw.get("audit_file", "data/agent_audit.jsonl")),
    )

    sh_raw = _section(raw, "screenshot")
    screenshot = ShotConfig(
        enabled=bool(sh_raw.get("enabled", True)),
        words=[str(w) for w in (sh_raw.get("words") or ShotConfig().words)],
        target=str(sh_raw.get("target", "same")),
        private_to=str(sh_raw.get("private_to", "") or ""),
        max_width=int(sh_raw.get("max_width", 0)),
        format=str(sh_raw.get("format", "PNG")),
        quality=int(sh_raw.get("quality", 82)),
        keep_files=int(sh_raw.get("keep_files", 20)),
        require_secret=bool(sh_raw.get("require_secret", False)),
        timeout_seconds=int(sh_raw.get("timeout_seconds", 30)),
    )

    cmd_raw = _section(raw, "commands")
    commands = CommandsConfig(
        enabled=bool(cmd_raw.get("enabled", True)),
        prefix=str(cmd_raw.get("prefix", "\\")),
        short_prefix=str(cmd_raw.get("short_prefix", "/") or ""),
        trusted_chats=[str(n) for n in (cmd_raw.get("trusted_chats") or [])],
        allowed_senders=[str(n) for n in (cmd_raw.get("allowed_senders") or [])],
        secret=str(cmd_raw.get("secret", "") or ""),
        identity_mode=str(cmd_raw.get("identity_mode", "any")).strip().lower(),
        verify_identity=bool(cmd_raw.get("verify_identity", True)),
        restart_words=[str(w) for w in
                       (cmd_raw.get("restart") or CommandsConfig().restart_words)],
        stop_words=[str(w) for w in (cmd_raw.get("stop") or CommandsConfig().stop_words)],
        start_words=[str(w) for w in (cmd_raw.get("start") or CommandsConfig().start_words)],
        status_words=[str(w) for w in (cmd_raw.get("status") or CommandsConfig().status_words)],
        meme_words=[str(w) for w in (cmd_raw.get("meme") or CommandsConfig().meme_words)],
        reply_confirm=bool(cmd_raw.get("reply_confirm", True)),
    )

    data_dir = DATA_DIR
    log_dir = ROOT / str(_section(raw, "logging").get("dir", "data/logs"))
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        dsh=DshConfig(
            command=str(dsh_raw["command"]),
            profile=str(dsh_raw.get("profile", "wechat")),
            timeout_seconds=int(dsh_raw.get("timeout_seconds", 180)),
            model_preset=str(dsh_raw.get("model_preset", "deepseek")).strip().lower(),
            preset_dir=str(dsh_raw.get("preset_dir", "") or ""),
            zhipu_api_key=str(dsh_raw.get("zhipu_api_key", "") or ""),
        ),
        mode=mode,
        whitelist_groups=[str(g) for g in (raw.get("whitelist_groups") or [])],
        bot_names=[str(n) for n in (raw.get("bot_names") or [])],
        require_at=bool(raw.get("require_at", True)),
        ignore_self=bool(raw.get("ignore_self", True)),
        rate_limit=RateLimitConfig(
            per_group_per_hour=int(rl_raw.get("per_group_per_hour", 20)),
            min_interval_seconds=int(rl_raw.get("min_interval_seconds", 8)),
            reply_delay=_read_delay(rl_raw),
        ),
        aggregate=aggregate,
        split=split,
        memes=memes,
        memes_update=memes_update,
        launch=launch,
        brush=brush,
        enka=enka,
        poll=poll,
        logs=logs,
        threads=threads,
        mood=mood,
        agent=agent,
        screenshot=screenshot,
        catch_up=catch_up,
        commands=commands,
        history=history,
        dedup_window_seconds=int(_section(raw, "dedup").get("window_seconds", 300)),
        log_level=str(_section(raw, "logging").get("level", "INFO")),
        log_dir=log_dir,
        data_dir=data_dir,
        db_path=data_dir / "history.sqlite3",
    )


def load_persona(group: str | None = None, path: Path | str = PERSONA_PATH) -> tuple[str, str]:
    """返回 (机器人昵称, 人设提示词)。读取失败时回退到内置的最小人设。"""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return "小深", "你是一个微信群助手，回答简短、口语化、纯文本。"

    default = raw.get("default") or {}
    name = str(default.get("name", "小深"))
    prompt = str(default.get("prompt", "")).strip()

    groups = raw.get("groups") or {}
    if group and isinstance(groups, dict) and group in groups:
        override = groups[group] or {}
        name = str(override.get("name", name))
        prompt = str(override.get("prompt", prompt)).strip()

    if not prompt:
        prompt = "你是一个微信群助手，回答简短、口语化、纯文本。"
    return name, prompt
