"""大脑层：调用 DSH headless 拿回复。

实测确认的调用形态（注意：**不要**在 --profile 后面再写 headless，
否则那个词会被当成任务正文的一部分，模型会以为用户在叫它 "headless"）：

    dsh --profile wechat --json -
任务文本通过 stdin 传入，避免 Windows 命令行引号/长度/换行问题。
stdout 是 NDJSON 事件流，其中：
    {"type":"session","sessionId":"...","cwd":"..."}
    {"type":"text","text":"..."}
    {"type":"final","text":"..."}
    {"type":"error","message":"..."}
stderr 是诊断信息（含 reasoning），与最终答案分离。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .config import DshConfig
from .procutil import no_window

# 兜底长度上限。人设里已经要求 80 字，这里只是防止模型跑飞。
MAX_REPLY_CHARS = 600


class BrainError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# 输出清洗：即便模型违规带了 Markdown，也在发送前抹掉
# --------------------------------------------------------------------------
_CODE_FENCE = re.compile(r"```.*?```", re.S)
_MD_LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
_INLINE_CODE = re.compile(r"`([^`]*)`")
_BOLD = re.compile(r"(\*\*|__)(.*?)\1")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_BULLET = re.compile(r"^\s{0,3}([-*+]|\d+[.)])\s+", re.M)
_HRULE = re.compile(r"^\s*([-*_]\s*){3,}$", re.M)


def clean_reply(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    text = _CODE_FENCE.sub(lambda m: m.group(0).strip("`").strip(), text)
    text = _MD_LINK.sub(lambda m: f"{m.group(1)} {m.group(2)}".strip(), text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _BOLD.sub(r"\2", text)
    text = _HEADING.sub("", text)
    text = _BULLET.sub("", text)
    text = _HRULE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    if len(text) > MAX_REPLY_CHARS:
        text = text[:MAX_REPLY_CHARS].rstrip() + "…"
    return text


def build_prompt(persona: str, history_block: str, sender: str, text: str,
                 group: str, *, multi: bool = False, empty: bool = False,
                 memes_block: str = "", mood_line: str = "",
                 turn_no: int = 0, quote_nick: str = "",
                 may_decline: bool = False) -> str:
    """拼提示词。

    ## 为什么全用英文短标签

    每一节原先写的是「【本群最近的聊天记录（仅供参考，不要逐条回应）】」
    这种中文长标签，一个字大约一个 token，还每次调用都发一遍。
    实测一次查天气要跑 3 步 agent 循环，每步都重发整段上下文，
    所以标签上的一个 token 实际要乘 3 倍花出去。

    换成 [CTX] / [MSG] / [RULES] 这类英文标签之后，
    模型理解不受影响（就是几个分区标记），但同样的语义只花几分之一的 token。
    正文、人设、规则仍然是中文——那部分直接决定回复质量，不动。

    ## 为什么昵称必须单列

    实测踩过坑：写成「景德镇到印尼：二次元😹😹😹」时，模型把群友昵称
    「景德镇到印尼」当成消息正文的一部分，还去"纠正"这个错别字。
    所以名字永远单独放在 u= 后面，正文另起一行，并用 RULES 明确说明。

    ## 新增的几行都很短

    [MOOD] / [THREAD] / [QUOTE] 都只有十几个字，但换来的是「记得住人」
    「分得清话题」「认得出引用」。这几行是按需注入的，没有就不占 token。
    """
    parts = ["[ROLE]", persona, ""]

    if history_block:
        parts += ["[CTX]", history_block, ""]

    if memes_block:
        parts += ["[GAG]", memes_block, ""]

    if mood_line:
        parts += ["[MOOD]", mood_line, ""]

    header = "[MSG]"
    meta = []
    if group:
        meta.append(f"g={group}")
    meta.append(f"u={sender}")
    # 当前时间。**必须给**，否则问「几点了」模型只能编 —— 它没有时钟，
    # 也没有能查时间的工具。放在 [MSG] 的 meta 里而不是缓存前缀里，
    # 因为它每分钟都在变，放进前缀会让缓存整个失效。
    # %w 是 0=周日..6=周六，所以直接用「日一二三四五六」取字符，别绕。
    meta.append(f"now={time.strftime('%m-%d %H:%M')} 周{'日一二三四五六'[int(time.strftime('%w'))]}")
    if turn_no:
        meta.append(f"turn={turn_no}")
    if quote_nick:
        meta.append(f"quote={quote_nick}")
    if may_decline:
        # 见 profile 里的约定：带这个标记时，模型可以只回一个 "-" 表示
        # "这条不是跟我说的"。比多问一次模型便宜得多。
        meta.append("mayignore")

    parts += [header]
    parts += meta

    if multi:
        parts += [
            "[NOTE] 以下几行是同一个人（或群里几个人）在 @ 你之后连着发的，"
            "属于同一次发言，整体理解，只回一条，不要逐条回应。",
            text,
        ]
    else:
        parts.append(text)

    parts += ["[RULES]"]
    parts.append("u= 后面是发消息那个人的群昵称，只是名字，不是消息内容。"
                 "不要复述、评论、纠正或调侃它。")
    # 这条很关键：只要模型决定调一次工具，agent 循环就要多跑两步，
    # 每一步都把整段上下文重发一遍——实测空 @ 时它自作主张去搜天气，
    # 一次就从 2100 token 涨到 13000。闲聊根本不需要联网。
    parts.append("只有确实需要最新信息或天气时才用联网搜索；闲聊、接梗、吐槽、"
                 "被 @ 了就应一声，都直接回，不要调用任何工具。")

    if mood_line:
        parts.append("按 [MOOD] 的数值调语气。")
        parts.append("[MSG] 里有 now=，那就是当前时间。问几点、星期几、今天几号都照它答，"
                     "不要说「我不知道时间」—— 这是你唯一该照搬而不是发挥的信息。")

    if empty:
        parts.append("这次只 @ 了你一下、没有正文，像真人被点名那样随口应一声即可"
                     "（如「干嘛喵~」「嗯？咋啦」）。不要调用任何工具。"
                     "禁止「在呢在呢」「我在」「有什么可以帮你的吗」这类客服腔，"
                     "也不要介绍自己能做什么。")
    else:
        parts.append("只针对 [MSG] 的正文回复，用 [ROLE] 的身份和语气。")

    parts.append("输出纯文本，就是发到群里的那句话本身：不要前后缀、署名、引号、解释。")
    parts.append("想分几条发，就用空行隔开。")
    return "\n".join(p for p in parts if p)


class Brain:
    def __init__(self, cfg: DshConfig, cwd: Path | str | None = None,
                 extra_env: dict[str, str] | None = None) -> None:
        self.cfg = cfg
        # 给 agent 一个干净的 cwd，避免意外读取到工作目录里的 AGENTS.md
        self.cwd = Path(cwd) if cwd else None
        if self.cwd:
            self.cwd.mkdir(parents=True, exist_ok=True)
        # 额外塞给子进程的环境变量。agent 那条链路靠它开权限：
        #   DSH_PERMISSION_MODE=danger-full-access
        # 一个开关同时决定沙箱模式和审批策略（见 profile 里的 approval 插件：
        # danger-full-access -> policy: 'never'，否则 'ask'，而群里没人能审批）。
        self.extra_env = dict(extra_env or {})
        # 最近一次调用的 token 用量，dsh 会在 status/stepend 事件里报
        self.last_usage: dict = {}
        # agent 循环走了几步。查天气/搜资料这类会用工具，一步就是一整轮上下文，
        # 所以这个数字直接决定真实开销。
        self.last_step: int = 0
        # 最近一次调用用过的工具名（agent 指令要记进审计日志）
        self.last_tools: list[str] = []

    def _launcher_prefix(self) -> list[str]:
        """只返回启动器前缀，不含 --profile/--json 等应用参数。

        优先直接调用 Electron 可执行文件（免去 cmd.exe 的引号转义），
        否则回退到 dsh.cmd 包装脚本。
        """
        cmd = Path(str(self.cfg.command))
        direct = self._direct_argv(cmd)
        if direct:
            return direct
        prefix = [str(cmd)]
        if str(cmd).lower().endswith((".cmd", ".bat")):
            prefix = ["cmd.exe", "/c"] + prefix
        return prefix

    @staticmethod
    def _direct_argv(cmd: Path) -> list[str] | None:
        """从 dsh.cmd 的位置反推 Electron 宿主与 cli.js。

        注意 app.asar 是单个归档文件，cli.js 在归档内部，
        所以这里只能探测宿主 exe；宿主不存在时返回 None 走包装脚本。
        """
        if not str(cmd).lower().endswith((".cmd", ".bat")):
            return None
        try:
            install = cmd.parent.parent.parent.parent
            exe = install / "DeepSeek Harness.exe"
            cli = (install / "resources" / "app.asar" / "dsh" / "node_modules"
                   / "@deepseek-ai" / "dsh-desktop-host" / "lib" / "cli.js")
            if exe.is_file() and cli.is_file():
                return [str(exe), "--expose-internals", str(cli)]
        except OSError:
            return None
        return None

    def ask(self, prompt: str, *, user: str = "", group: str = "") -> str:
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env["PYTHONUTF8"] = "1"
        env.update(self.extra_env)
        argv = self._launcher_prefix()
        if argv and argv[0].lower().endswith("harness.exe"):
            env["ELECTRON_RUN_AS_NODE"] = "1"
        argv += ["--profile", self.cfg.profile, "--json", "-"]

        started = time.time()
        try:
            proc = subprocess.run(
                argv,
                input=prompt.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.cwd) if self.cwd else None,
                env=env,
                timeout=self.cfg.timeout_seconds,
                # 只有回退到 dsh.cmd（= cmd.exe）时才会有窗口问题：
                # 机器人没有控制台，起 cmd.exe 就会新建一个空黑框。
                # 直接调 Electron 宿主时不弹窗，这个标志也无害。
                creationflags=no_window(),
            )
        except subprocess.TimeoutExpired:
            raise BrainError(f"思考超时（>{self.cfg.timeout_seconds}s）")
        except FileNotFoundError as exc:
            raise BrainError(f"找不到 dsh 启动器：{self.cfg.command}") from exc

        stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace")

        final_text, streamed = "", []
        turn_error = ""
        self.last_tools = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "final" and event.get("text"):
                final_text = str(event["text"])
            elif etype == "text" and event.get("text"):
                streamed.append(str(event["text"]))
            elif etype == "tool_call" and event.get("tool"):
                name = str(event["tool"])
                if name not in self.last_tools:
                    self.last_tools.append(name)
            elif etype == "status" and isinstance(event.get("usage"), dict):
                self.last_usage = event["usage"]
                self.last_step = int(event.get("step") or self.last_step)
            elif etype == "status" and event.get("phase") == "turn_end":
                reason = event.get("reason") or {}
                if isinstance(reason, dict) and reason.get("kind") == "error":
                    err = reason.get("error") or {}
                    turn_error = str(err.get("message") or err or "未知错误")
            elif etype == "error":
                raise BrainError(str(event.get("message") or event))

        reply = clean_reply(final_text or "".join(streamed))
        if not reply:
            # turn_end 里带 error 的情况也要报出来，不然只会看到一句
            # 「模型没有返回内容」，完全不知道是权限、模型还是网络的问题。
            if turn_error:
                raise BrainError(turn_error)
            tail = (stderr or stdout).strip().splitlines()[-3:]
            raise BrainError("模型没有返回内容" + (f"：{' / '.join(tail)}" if tail else ""))

        elapsed = time.time() - started
        u = self.last_usage or {}
        if u:
            print(f"[brain] {elapsed:.1f}s | prompt={len(prompt)}字 step={self.last_step} "
                  f"in={u.get('inputTokens', '?')} out={u.get('outputTokens', '?')} "
                  f"cache_r={u.get('cacheReadTokens', '?')} "
                  f"total={u.get('totalTokens', '?')} -> {reply[:40]!r}", file=sys.stderr)
        else:
            print(f"[brain] {elapsed:.1f}s | prompt={len(prompt)}字 -> {reply[:40]!r}",
                  file=sys.stderr)
        return reply
