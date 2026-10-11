"""群内 `\\agent` 指令：把一条自然语言指令交给本机 agent 去执行。

这是什么
--------
和群里闲聊那条链路完全是两回事。闲聊走 `wechat` profile，那个 profile
**刻意关掉了文件系统和命令行**，只留联网搜索。这里走 `wechat-agent` profile，
fs / pwsh / fs-search 全开着，agent 能真的在本机上读文件、改文件、跑命令。

所以它是「微信群里的一条远程操作通道」，风险必须说清楚
--------------------------------------------------------
- 能在群里发这条指令的人，等于能在这台机器上做任何事。**没有任何中间层**。
- 群昵称谁都能改，`commands.allowed_senders` 挡不住冒用；
  `commands.secret` 是唯一的第二因素，而它在群里发过就等于公开。
- 所以默认**关闭**，而且一旦开启就强制要求口令（不留空就拒绝启动）。

闸门
----
1. `agent.enabled` 默认 false；
2. `commands.enabled` + `allowed_senders`（昵称）必须过；
3. `commands.secret` 必须非空且出现在指令里 —— 这条对 agent 是**强制**的，
   不像别的指令可以留空；
4. 只有 `agent.groups` 里列的群（留空 = 全部白名单群）能用；
5. 可选的允许时段（`agent.hours`），比如只允许白天用；
6. 每次调用写 `data/agent_audit.jsonl`：谁、哪个群、什么指令、用了哪些工具、结果摘要。

权限怎么开
----------
被拉起的 dsh 子进程需要 `DSH_PERMISSION_MODE=danger-full-access`。
这一个开关同时决定沙箱模式和审批策略（profile 里的 approval 插件：
`danger-full-access` -> `policy: 'never'`，否则 `'ask'`，
而群里没有人能点「批准」，`ask` 就等于卡死）。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .brain import Brain, BrainError, clean_reply
from .config import AgentConfig, DshConfig


@dataclass
class AgentResult:
    ok: bool
    text: str
    tools: list[str]
    seconds: float
    steps: int
    reason: str = ""


class AgentRunner:
    def __init__(self, cfg: AgentConfig, dsh: DshConfig, root: Path,
                 *, secret_present: bool = False) -> None:
        self.cfg = cfg
        self.root = Path(root)
        self.secret_present = bool(secret_present)
        self.audit_path = self.root / (cfg.audit_file or "data/agent_audit.jsonl")
        # 复用 Brain：只是换 profile、换 cwd、多塞一个环境变量
        agent_dsh = DshConfig(
            command=dsh.command,
            profile=cfg.profile,
            timeout_seconds=max(30, int(cfg.timeout_seconds)),
        )
        cwd = Path(cfg.cwd) if cfg.cwd else self.root
        self.brain = Brain(
            agent_dsh,
            cwd=cwd,
            extra_env={"DSH_PERMISSION_MODE": "danger-full-access"},
        )
        self.cwd = cwd

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    # ---------------------------------------------------------------- 闸门
    def refuse_reason(self) -> str:
        """返回不能启用的原因；空字符串表示可以启用。"""
        if not self.cfg.enabled:
            return "agent 指令未开启（config.yaml 里 agent.enabled）"
        if self.cfg.require_secret and not self.secret_present:
            return ("agent 指令必须配 commands.secret 才能开启 —— "
                    "否则任何把群昵称改成授权昵称的人都能操作这台机器")
        return ""

    def allowed_in(self, group: str) -> bool:
        if not self.cfg.groups:
            return True
        return group in self.cfg.groups

    def allowed_now(self, when: time.struct_time | None = None) -> bool:
        """可选的时段限制，例如只允许 08:00-23:00。"""
        if not self.cfg.hours:
            return True
        try:
            lo, hi = self.cfg.hours
        except (TypeError, ValueError):
            return True
        hour = (when or time.localtime()).tm_hour
        if lo <= hi:
            return lo <= hour < hi
        return hour >= lo or hour < hi      # 跨零点，比如 22-6

    # ---------------------------------------------------------------- 执行
    def run(self, task: str, *, group: str, sender: str) -> AgentResult:
        task = (task or "").strip()
        if not task:
            return AgentResult(False, "", [], 0.0, 0, "空指令")

        prompt = (
            f"【本机任务】\n{task}\n\n"
            f"（工作目录：{self.cwd}）\n"
            "直接动手做完，然后用一两句话回报结果。"
        )

        started = time.time()
        try:
            text = self.brain.ask(prompt, user=sender, group=group)
            result = AgentResult(
                ok=True, text=clean_reply(text)[: self.cfg.max_reply_chars],
                tools=list(self.brain.last_tools),
                seconds=time.time() - started, steps=self.brain.last_step,
            )
        except BrainError as exc:
            result = AgentResult(
                ok=False, text=str(exc)[: self.cfg.max_reply_chars],
                tools=list(self.brain.last_tools),
                seconds=time.time() - started, steps=self.brain.last_step,
                reason=str(exc),
            )

        self.audit(group=group, sender=sender, task=task, result=result)
        return result

    # ---------------------------------------------------------------- 审计
    def audit(self, *, group: str, sender: str, task: str,
              result: AgentResult) -> None:
        """每次调用都留痕。事后至少能查出「谁在什么时候让机器干了什么」。"""
        entry = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "group": group,
            "sender": sender,
            "task": task[:500],
            "ok": result.ok,
            "tools": result.tools,
            "steps": result.steps,
            "seconds": round(result.seconds, 1),
            "result": result.text[:400],
        }
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            logging.warning("审计日志写入失败：%s", exc)
        logging.warning(
            "agent 指令执行：[%s] %s -> %r | %s | %.1fs | 工具=%s",
            group, sender, task[:60], "成功" if result.ok else "失败",
            result.seconds, ",".join(result.tools) or "无",
        )

    def tail(self, count: int = 5) -> list[dict]:
        """最近几次调用，供群内 \\agent log 查看。"""
        if not self.audit_path.exists():
            return []
        try:
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines[-count:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out
