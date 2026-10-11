"""主循环。

用法：
    python -m src.main --check            配置与大脑自检（不接微信）
    python -m src.main --demo             控制台演示，不碰微信
    python -m src.main --groups           列出微信会话，确认群名准确写法
    python -m src.main --selftest         轮询链路自测（只读，不发送）
    python -m src.main --watch [秒数]     观察新消息，不回复不发送
    python -m src.main --probe            打印 wxauto4 真实 API 结构
    python -m src.main --console          用控制台代替微信接入
    python -m src.main --purge-now        立刻执行一次历史清理
    python -m src.main                    正式运行
"""

from __future__ import annotations

import argparse
import re
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from .aggregate import EMPTY_BODY, Aggregator, Turn
from .agent import AgentRunner
from .brain import Brain, BrainError, build_prompt
from .config import CONFIG_PATH, ROOT, Config, load_config, load_persona
from .drafts import DraftStore
from .history import HistoryStore, StoredMessage
from . import elevate, meme_feed
from .memes import MemeBook
from .mood import MoodStore, parse_mood_tag
from .policy import Gatekeeper, StopSwitch, make_msg_key
from .screen import ScreenError, capture, cleanup
from .splitter import split_reply
from .style import apply_style, load_style
from .threads import ThreadBook
from .wechat_io import BaseAdapter, IncomingMessage, build_adapter
from .procutil import pid_alive


def setup_logging(cfg: Config) -> None:
    """配置日志。

    force=True 是必须的，不是保险起见：
    `import wxauto4` 会在 wxauto4/logger.py 里执行
        root_logger.handlers.clear()
    把 root 上已有的 handler 全部删掉，再塞一个只写 stderr 的彩色 handler。
    于是 bot.log 从此收不到本项目任何日志，只剩 comtypes 的噪音——
    出问题时完全没法查（这个坑实际踩到了）。
    所以构造完适配器之后要再调一次本函数把配置抢回来。
    """
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(cfg.log_dir / "bot.log", encoding="utf-8"),
        ],
        force=True,
    )
    # 这几个第三方话很多，压到 WARNING，免得把 bot.log 冲得没法看
    for noisy in ("comtypes", "comtypes.client", "urllib3", "PIL", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def make_adapter(cfg: Config, console: bool = False) -> BaseAdapter:
    """构造接入层，并把被 wxauto4 抢走的日志配置抢回来。"""
    adapter = build_adapter(cfg, console=console)
    setup_logging(cfg)
    logging.getLogger(__name__).debug("日志配置已在适配器构造后重新生效")
    return adapter


def format_history(messages: list[StoredMessage], per_message_chars: int = 120) -> str:
    """把最近记录压成几行发给模型。

    格式从「群友「张三」说：xxx / 我（机器人自己）说：xxx」改成「- 张三: xxx」：
    30 条记录光这一项就少 200 字左右，而一次查天气要跑 3 步 agent 循环，
    省下来的是 3 倍。太长单条也截断。

    昵称仍然单独成段、和正文之间用冒号隔开——只是不再用「群友「」说：」这种
    啰嗦写法。实测踩过的坑（模型把昵称当成正文去"纠正"）由 [RULES] 那段兜着。
    """
    lines = []
    for msg in messages:
        who = "我" if msg.is_bot else (msg.sender or "?")
        text = (msg.content or "").replace("\n", " ").strip()
        if per_message_chars and len(text) > per_message_chars:
            text = text[:per_message_chars] + "…"
        lines.append(f"- {who}: {text}")
    return "\n".join(lines)


class Bot:
    def __init__(self, cfg: Config, adapter: BaseAdapter, console: bool = False) -> None:
        self.cfg = cfg
        self.adapter = adapter
        self.console = console
        self.gate = Gatekeeper(cfg)
        self.store = HistoryStore(cfg.db_path, cfg.history)
        self.drafts = DraftStore(cfg.data_dir / "drafts" / "pending.jsonl")
        self.stop = StopSwitch(cfg.data_dir)
        self.brain = Brain(cfg.dsh, cwd=cfg.data_dir / "agent-cwd")
        self.agg = Aggregator(cfg.aggregate)
        self.memes = MemeBook.load(cfg.memes, ROOT)
        # 按人分开的连续对话线程（被 @ 一次之后就不用再 @）
        self.book = ThreadBook(cfg.threads, cfg.data_dir / "threads.json")
        restored = self.book.load()
        if restored:
            logging.info("恢复了 %d 条未结束的对话线程（重启不丢）", restored)
        # 心情值：按人持久化，影响之后的语气
        self.mood = MoodStore(cfg.db_path, cfg.mood)
        # 群内 \\agent：权限最大的一条指令，闸门都在 AgentRunner 里
        self.runner = AgentRunner(
            cfg.agent, cfg.dsh, ROOT,
            secret_present=bool((cfg.commands.secret or "").strip()),
        )
        self._next_purge = 0.0
        self._next_meme_reload = 0.0
        self._next_meme_update = 0.0

        # 耗时指令（agent / 开程序）跑在后台线程，结果通过这个队列交回
        # 主线程发送。**发送只能由主线程做** —— wxauto4 走 UIA，不是
        # 线程安全的，两个线程同时发会互相踩。
        self._outbox: queue.Queue = queue.Queue()
        # 全局一把锁：同一时间只允许一个耗时任务在跑
        self._busy = threading.Lock()
        self._next_cfg_check = 0.0
        try:
            self._cfg_mtime = CONFIG_PATH.stat().st_mtime
        except OSError:
            self._cfg_mtime = 0.0
        self._last_probe_warn = 0.0
        # 群内 \\stop 指令设置的内存暂停位。和 data/STOP 文件的区别：
        # 这个是可逆的，收到 \\start 就恢复。
        self.paused = False
        self.pid_path = cfg.data_dir / "bot.pid"
        # 每群最近一次回复的时间和触发人，用来判断「接着聊」窗口
        self._last_reply: dict[str, tuple[float, str]] = {}

    # ------------------------------------------------------------------ 上下文
    def hist_block(self, history: list[StoredMessage]) -> str:
        return format_history(history, self.cfg.history.per_message_chars)

    def meme_block(self, text: str, history: list[StoredMessage]) -> str:
        """找出这段话里出现的梗。命中才注入，没命中就一个 token 都不花。"""
        if not self.cfg.memes.enabled:
            return ""
        ctx = ""
        if self.cfg.memes.scan_history and self.cfg.memes.history_window > 0:
            recent = history[-self.cfg.memes.history_window:]
            ctx = " ".join(m.content for m in recent if m.content)
        hits = self.memes.match(text or "", ctx,
                                limit=self.cfg.memes.max_entries,
                                max_chars=self.cfg.memes.max_chars,
                                max_history_hits=self.cfg.memes.max_history_hits)
        if hits:
            logging.info("梗库命中：%s", "、".join(e.term for e in hits))
        return MemeBook.format_block(hits)

    # ------------------------------------------------------------------ 指令
    def handle_command(self, msg: IncomingMessage, body: str) -> bool:
        """处理群内指令。返回 True 表示这条消息已被消费掉。

        身份只认群昵称（见 config.yaml 的说明），非授权者静默忽略，
        连"你没权限"都不回——免得暴露控制机制，也免得被试探。
        """
        action = self.gate.classify_command(
            self.gate.strip_secret(body),
            {"agent": self.cfg.agent.words, "shot": self.cfg.screenshot.words,
             "launch": self.cfg.launch.words},
        )

        if not self.gate.authorize_command(msg.sender, msg.identity):
            # 安全阀：急停/恢复绝不能被严格身份校验挡住。
            # 否则 sender_info() 哪次读不到微信号，主人就被自己锁在门外 ——
            # 机器人既停不下来也起不来，比被人冒用严重得多。
            if action in {"start", "stop"} and self.gate.authorize_command(
                msg.sender, msg.identity, loose=True
            ):
                logging.warning(
                    "急停/恢复降级为宽松身份匹配放行：[%s] %s -> %r",
                    msg.group, msg.sender, body[:30],
                )
            else:
                logging.warning(
                    "有人尝试使用控制指令但不在授权名单里：[%s] %s -> %r（身份=%s）",
                    msg.group, msg.sender, body[:30],
                    {k: v for k, v in (msg.identity or {}).items() if k != "region"} or "未解析",
                )
                return True
        # 可信会话（自己的私聊）免口令。私聊没法冒充发送者，所以是安全的；
        # 群里**不能**这么做 —— identity_mode=any 时群昵称谁都能改。
        trusted = self.gate.is_trusted_chat(msg.group)
        if not self.gate.check_secret(body, trusted=trusted):
            # 口令不对：静默吞掉，不回复也不留任何"这是命令"的痕迹，
            # 免得被人试探出机制。
            logging.warning(
                "控制指令口令不正确，已忽略：[%s] %s -> %r",
                msg.group, msg.sender, body[:30],
            )
            return True
        if action is None:
            # 走到这里说明身份和口令都过了 —— 就是自己人把指令词写错了。
            # 原来这里是静默 return，用户看到的现象是「发了指令没反应」，
            # 完全猜不到是哪个字不对（实测踩过：'桌面截图给我 ccb'）。
            # 所以回一句提示。因为已经过了鉴权，这不泄露任何机制。
            logging.info("收到无法识别的指令：[%s] %s -> %r", msg.group, msg.sender, body[:30])
            words = self.gate.command_words(
                {"agent": self.cfg.agent.words, "shot": self.cfg.screenshot.words})
            try:
                self.adapter.send(
                    msg.group,
                    f"认不出这条指令。开头那个词得是下面之一：\n{words}\n"
                    f"比如：\\\\截图 ccb")
            except Exception as exc:  # noqa: BLE001
                logging.warning("提示发送失败：%s", exc)
            return True

        if action == "stop":
            self.paused = True
            self.stop.engage(f"群内指令，来自 {msg.sender}")
            logging.warning("已被群内指令暂停：[%s] by %s", msg.group, msg.sender)
            confirm = "好，我闭嘴了。"
        elif action == "start":
            self.paused = False
            self.stop.release()
            logging.warning("已被群内指令恢复：[%s] by %s", msg.group, msg.sender)
            confirm = "回来了，继续聊喵"
        elif action == "restart":
            # 这条不回执文案，自己负责回复并退出
            self.handle_restart(msg)
            return True
        elif action == "meme":
            confirm = self.handle_meme(body)
        elif action == "agent":
            # 这两条不是「回一句话」而是「干一件事」，自己负责回复，直接返回。
            # 都走后台线程：agent 一次几十秒、开程序要等计划任务，
            # 期间主循环必须继续轮询，否则漏消息（实测漏过 \\截图）。
            self._run_async("agent", msg, self.handle_agent, body)
            return True
        elif action == "shot":
            self.handle_shot(msg, body)
            return True
        elif action == "launch":
            self._run_async("开程序", msg, self.handle_launch, body)
            return True
        else:
            state = "暂停中" if (self.paused or self.stop.stopped) else "运行中"
            lines = [f"当前状态：{state}"]
            if self.mood.enabled and self.cfg.mood.show_in_status:
                snap = self.mood.snapshot(msg.group)
                low = [f"{who} {val}" for who, val in snap if val < self.cfg.mood.neutral]
                if low:
                    lines.append("心情偏低：" + "、".join(low[:6]))
            confirm = " ／ ".join(lines)

        if self.cfg.commands.reply_confirm:
            try:
                time.sleep(self.gate.limiter.reply_delay())
                self.adapter.send(msg.group, confirm)
                self.gate.limiter.commit(msg.group)
                self.store.add(
                    msg.group, "系统", confirm,
                    make_msg_key(msg.group, "__bot__", confirm, str(time.time())),
                    is_bot=True,
                )
            except Exception as exc:  # noqa: BLE001
                logging.error("指令确认发送失败：%s", exc)
        return True

    # ------------------------------------------------------------------ 直发
    def _emit_async(self, group: str, text: str, *, split: bool = True) -> None:
        """后台线程用的"发送"：只塞队列，真正的发送留给主线程。"""
        self._outbox.put((group, text, split))

    def _run_async(self, label: str, msg: IncomingMessage, fn, *args) -> None:
        """把耗时指令丢到后台线程，主循环继续轮询。

        为什么要这样：主循环是单线程的，`\\agent` 一次跑 54 秒，这期间
        **完全不轮询微信** —— 接着发的 `\\截图` 就收不到，只能等"补答"
        事后捞回来，看起来像"没识别"。实测踩过。

        线程里只跑不碰微信的部分（dsh 子进程、提权通道），结果经 _outbox
        交回主线程发送 —— wxauto4 走 UIA，不是线程安全的。

        并发：全局一把锁，同一时间只跑一个。第二个请求直接回一句，不排队
        （排队会让群里等更久，而且重启后队列就没了）。
        """
        if not self._busy.acquire(blocking=False):
            logging.info("%s 已有任务在跑，本次拒绝：[%s] %s", label, msg.group, msg.sender)
            self._outbox.put((msg.group, "上一个还在跑，等它完了再来", False))
            return

        def worker() -> None:
            try:
                fn(msg, *args, emit=self._emit_async)
            except Exception as exc:  # noqa: BLE001
                logging.exception("%s 后台任务出错：%s", label, exc)
                self._outbox.put((msg.group, f"{label} 出错了：{exc}", False))
            finally:
                self._busy.release()

        threading.Thread(target=worker, daemon=True, name=f"bg-{label}").start()
        logging.info("%s 转入后台线程，主循环继续轮询：[%s] %s",
                     label, msg.group, msg.sender)

    def _drain_outbox(self) -> None:
        """主循环每轮调用：把后台线程的产出发出去。"""
        while True:
            try:
                group, text, split = self._outbox.get_nowait()
            except queue.Empty:
                return
            try:
                self._say(group, text, split=split)
            except Exception as exc:  # noqa: BLE001
                logging.error("发送后台结果失败：%s", exc)

    def _say(self, group: str, text: str, *, split: bool = True) -> None:
        """绕过聚合/线程，直接把一段话发到群里（指令回执、agent 结果、截图说明）。"""
        text = (text or "").strip()
        if not text:
            return
        parts = split_reply(text, self.cfg.split) if split else [text]
        for index, part in enumerate(parts):
            if index:
                time.sleep(self.gate.limiter.reply_delay_range(self.cfg.split.delay))
            try:
                self.adapter.send(group, part)
            except Exception as exc:  # noqa: BLE001
                logging.error("发送失败：%s", exc)
                return
            self.gate.limiter.commit(group)
            self.store.add(
                group, "系统", part,
                make_msg_key(group, "__bot__", part, str(time.time())),
                is_bot=True,
            )

    # ------------------------------------------------------------------ agent
    def handle_restart(self, msg: IncomingMessage) -> None:
        """`\\restart`：重启机器人服务。

        机器人没法重启自己，所以启动一个**脱离的**辅助进程（restart.py），
        由它等本进程死透、清掉 STOP、再拉起 start.cmd。详见那个文件的注释。

        只用在这两种场景：改了需要重启才生效的配置（config.yaml / 代码）。
        persona.yaml 和 memes.yaml 是实时读的，不用重启。
        """
        if not self.cfg.commands.enabled:
            return

        helper = ROOT / "restart.py"
        if not helper.exists():
            logging.error("找不到 %s，无法重启", helper)
            self._say(msg.group, f"重启失败：找不到 {helper.name}", split=False)
            return

        logging.warning("收到重启指令 [%s] %s，启动辅助进程后退出", msg.group, msg.sender)
        self._say(msg.group, "重启中，约十几秒后回来", split=False)

        try:
            subprocess.Popen(
                # 把当前 pid 传进去：close() 会删 bot.pid，靠读文件有竞态
                [sys.executable, str(helper), str(os.getpid())],
                cwd=str(ROOT),
                close_fds=True,
                creationflags=(
                    getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                ),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            logging.exception("启动 restart.py 失败：%s", exc)
            self._say(msg.group, f"重启失败：{exc}", split=False)
            return

        # 给上面那条消息一点时间真的发出去，然后收尾并硬退出。
        # 硬退出是刻意的：正常退出会在 finally 里耗一会儿，那期间主循环
        # 还可能再处理消息、再发回复，而我们刚说了「重启中」。
        time.sleep(2.0)
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass
        os._exit(0)

    def handle_agent(self, msg: IncomingMessage, body: str,
                     emit=None) -> None:
        """`\\\\agent <任务>`：把一条自然语言指令交给本机 agent 执行。

        这是整个项目里权限最大的一条指令 —— 它能真的读改本机文件、跑命令。
        所以闸门比别处多：默认关闭、强制口令、可限定群和时段、全程留痕。
        """
        if emit is None:
            emit = self._say
        reason = self.runner.refuse_reason()
        if reason:
            logging.warning("agent 指令被拒：[%s] %s -> %s", msg.group, msg.sender, reason)
            emit(msg.group, f"用不了：{reason}")
            return
        if not self.runner.allowed_in(msg.group):
            logging.warning("agent 指令在本群未开放：[%s] %s", msg.group, msg.sender)
            return
        if not self.runner.allowed_now():
            emit(msg.group, "现在不在允许的时段内")
            return

        task = self.gate.strip_secret(self.gate.command_args(body)).strip()

        # \\agent log —— 看看最近谁让机器干了什么
        if not task or task.lower() in {"log", "日志", "记录"}:
            rows = self.runner.tail(5)
            if not rows:
                emit(msg.group, "还没有 agent 调用记录")
                return
            lines = [f"{r.get('ts','?')[-8:]} {r.get('sender','?')}：{str(r.get('task',''))[:24]}"
                     f" -> {'成功' if r.get('ok') else '失败'}"
                     for r in rows]
            emit(msg.group, "最近的 agent 调用：\n" + "\n".join(lines))
            return

        if self.cfg.agent.ack:
            emit(msg.group, f"收到，开始弄：{task[:40]}")
        logging.warning("agent 指令下达：[%s] %s -> %r", msg.group, msg.sender, task[:80])

        result = self.runner.run(task, group=msg.group, sender=msg.sender)
        if result.ok:
            text = result.text or "做完了，但没给结果"
            if result.tools:
                text += f"\n（用了 {'、'.join(result.tools[:6])}，{result.seconds:.0f}s）"
        else:
            text = f"没做成：{result.text or result.reason}"
        emit(msg.group, text)

    # ------------------------------------------------------------------ 截图
    def handle_launch(self, msg: IncomingMessage, body: str,
                      emit=None) -> None:
        """`\\开 <程序>`：启动一个预授权的程序（需要管理员权限的那些）。

        为什么不做成 `\\agent` 的一部分：走 agent 要一次 LLM 调用、十几秒、
        还可能因为要管理员权限而卡住。而"开个游戏"是确定性操作 ——
        一条指令直达提权通道，几秒返回，不花 token。

        程序名必须在 run.ps1 的 `$programs` 表里。路径写死在那份脚本里
        （它被 ACL 锁成仅管理员可写），所以调用方**只能选名字、不能传路径**。
        """
        if emit is None:
            emit = self._say
        cfg = self.cfg.launch
        if not cfg.enabled:
            logging.info("启动指令未开启")
            return

        name = self.gate.strip_secret(self.gate.command_args(body)).strip()
        if not name:
            # 指令词和程序名连在一起写的（「开个原神」「帮我开星铁」）：
            # command_args 取的是第一个空格之后的内容，这里要拿到空，所以
            # 反过来从**第一个词**里把指令词剥掉。按词长从长到短试，
            # 免得「打开原神」被短的「开」先剥掉、剩下「打原神」。
            first = re.split(r"[\s,，。!！?？|]+",
                             self.gate.strip_secret(body).strip())[0]
            for w in sorted(cfg.words, key=len, reverse=True):
                if first.lower().startswith(str(w).lower()):
                    name = first[len(str(w)):].strip()
                    break
            else:
                name = first
        if not name:
            ok, out = elevate.run("list-programs", timeout=40)
            tip = out if ok else "读不到程序列表"
            emit(msg.group, f"要开哪个？可用的：\n{tip}", split=False)
            return

        # 名字做一次规范化：去掉常见的多余字，让「开个原神」「帮我开原神」也能用
        raw = name
        for junk in ("帮我", "给我", "一下", "个", "把"):
            name = name.replace(junk, "")
        name = name.strip()

        # 一条龙不是「程序」，是 BetterGI 的动作 —— 走它自己的命令行接口，
        # 而不是去点 UI 上那六下（点 UI 会抢鼠标、依赖窗口位置、失败后状态不明）。
        #   BetterGI.exe --startOneDragon            跑页面上选定的配置
        #   bettergi://startOneDragon                URL 形式，任意目录可用
        # 参数见 BetterGI 官方文档「命令行启动」。
        if name.lower() in ("一条龙", "onedragon", "onekey", "一键"):
            ok, out = elevate.run("onekey", {}, timeout=90)
            if ok:
                logging.warning("已触发 BetterGI 一条龙 [%s] %s", msg.group, msg.sender)
            else:
                logging.warning("触发一条龙失败 [%s] %s：%s", msg.group, msg.sender, out)
            emit(msg.group, out, split=False)
            return

        ok, out = elevate.run("launch", {"name": name}, timeout=60)
        if ok:
            logging.warning("已启动程序 [%s] %s -> %s（原始指令 %r）",
                            msg.group, msg.sender, name, raw)
        else:
            logging.warning("启动程序失败 [%s] %s -> %s：%s",
                            msg.group, msg.sender, name, out)
        emit(msg.group, out, split=False)

    def handle_shot(self, msg: IncomingMessage, body: str) -> None:
        """`\\\\截图`：抓一张全屏截图发出去。"""
        cfg = self.cfg.screenshot
        if not cfg.enabled:
            logging.info("截图指令未开启")
            return
        if cfg.require_secret and not (self.cfg.commands.secret or "").strip():
            logging.warning("截图指令要求口令，但 commands.secret 是空的")
            self._say(msg.group, "用不了：截图指令要求先配 commands.secret")
            return

        # 发到哪里：发回指令所在的聊天，还是私聊给指定的人
        target = msg.group
        if cfg.target == "private" and cfg.private_to:
            target = cfg.private_to

        try:
            path, (w, h) = capture(cfg, self.cfg.data_dir / "shots")
        except ScreenError as exc:
            logging.error("截图失败：%s", exc)
            self._say(msg.group, f"截图失败：{exc}", split=False)
            return

        try:
            self.adapter.send_file(target, str(path))
        except Exception as exc:  # noqa: BLE001
            logging.error("发送截图失败：%s", exc)
            self._say(msg.group, f"截图拍好了但发不出去：{exc}", split=False)
            return

        self.gate.limiter.commit(msg.group)
        removed = cleanup(self.cfg.data_dir / "shots", cfg.keep_files)
        logging.warning("已发截图到「%s」：%s（%dx%d，清理 %d 张旧图）",
                        target, path.name, w, h, removed)
        # 只有发到别处时才回一句说明，发回本群的话图片本身就是回复
        if target != msg.group:
            self._say(msg.group, f"截图已私聊发给「{target}」（{w}x{h}）", split=False)

    # ------------------------------------------------------------------ 梗
    def handle_meme(self, body: str) -> str:
        """处理「\\\\梗 …」。返回给群里看的回执（受 reply_confirm 控制）。"""
        args = self.gate.command_args(body)
        if not args:
            return "用法：\\\\梗 add 词 | 说明 ／ \\\\梗 del 词 ／ \\\\梗 list"

        head, _, rest = args.partition(" ")
        head = head.strip().lower()
        rest = rest.strip()

        if head in {"list", "列表", "ls"}:
            return self.memes.listing()
        if head in {"add", "加", "教"}:
            term, sep, note = rest.partition("|")
            if not sep:
                term, sep, note = rest.partition("：")
            term, note = term.strip(), note.strip()
            if not term:
                return "用法：\\\\梗 add 词 | 说明"
            result = self.memes.add(term, note)
            logging.warning("群内教梗：%s", result)
            return result
        if head in {"del", "delete", "rm", "删"}:
            term = rest.strip()
            if not term:
                return "用法：\\\\梗 del 词"
            return self.memes.remove(term)

        # 没写子命令时按「词 | 说明」容错
        if "|" in args or "：" in args:
            term, sep, note = (args.partition("|") if "|" in args else args.partition("："))
            return self.memes.add(term.strip(), note.strip())
        return self.memes.listing()

    # ------------------------------------------------------------------ 处理
    def handle(self, msg: IncomingMessage) -> None:
        # 指令优先于一切过滤：不需要 @，也不受"是否暂停"影响，
        # 否则一旦暂停就再也收不到 \\start 了。
        if not msg.is_bot:
            trusted = self.gate.is_trusted_chat(msg.group)
            body = self.gate.as_command(msg.content, trusted=trusted)
            if body is not None:
                # 指令的身份要单独解析（微信号/备注）。只在指令上做，
                # 普通聊天不走这条路，不增加开销。
                if self.cfg.commands.verify_identity:
                    try:
                        msg.identity = self.adapter.resolve_identity(msg)
                    except Exception as exc:  # noqa: BLE001
                        logging.warning("身份解析失败，按昵称匹配：%s", exc)
                dropped = self.agg.drop(msg.group)
                if dropped:
                    logging.info(
                        "控制指令到达，丢弃 [%s] 正在聚合的 %d 条消息",
                        msg.group, len(dropped.items),
                    )
                self.handle_command(msg, body)
                return

        if self.agg.enabled:
            self.handle_aggregated(msg)
            return

        ok, text, reason = self.gate.accept(
            msg.group, msg.sender, msg.content, msg.msg_key, is_bot=msg.is_bot
        )
        if not ok:
            logging.debug("丢弃 [%s/%s]：%s", msg.group, msg.sender, reason)
            return

        logging.info("收到 [%s] %s：%s", msg.group, msg.sender, text or "（只 @ 了机器人，没有内容）")

        if self.paused or self.stop.stopped:
            logging.warning(
                "已暂停（%s），不生成回复",
                "群内指令" if self.paused else "data/STOP 文件",
            )
            return

        # 只 @ 一下、没说别的：给模型一句说明，让它自然接一句，
        # 而不是收到一个空消息不知所措。
        prompt_text = text or EMPTY_BODY

        # 先取历史（此时还不含当前这条），再把当前消息落库
        persona_name, persona_prompt = load_persona(msg.group)
        history = self.store.recent(msg.group)
        if text:
            self.store.add(msg.group, msg.sender, text, msg.msg_key)

        prompt = build_prompt(
            persona_prompt, self.hist_block(history), msg.sender, prompt_text, msg.group,
            empty=not text, memes_block=self.meme_block(prompt_text, history),
        )

        try:
            reply = self.brain.ask(prompt, user=msg.sender, group=msg.group)
        except BrainError as exc:
            logging.error("生成失败：%s", exc)
            return

        self.deliver(msg.group, msg.sender, text, reply, empty_trigger=not text)

    # ------------------------------------------------------- 聚合模式（新）
    def _in_after_reply_window(self, group: str, sender: str) -> bool:
        """老办法：每群只记一个「最近回复时间」。

        线程模式打开时用 ThreadBook，这里是关掉线程后的退路。
        """
        window = self.cfg.aggregate.after_reply_window
        if window <= 0:
            return False
        last = self._last_reply.get(group)
        if not last:
            return False
        when, who = last
        if time.time() - when > window:
            return False
        if self.cfg.aggregate.after_reply_sender == "same" and sender != who:
            return False
        return True

    def _members(self, group: str, history: list[StoredMessage]) -> list[str]:
        """从最近记录里凑出群友昵称列表。

        拿不到真正的群成员名单（wxauto4 只给 group_member_count），
        但最近说过话的人基本就够用了——相关性判定靠它认「提到的是别人」。
        """
        return sorted({m.sender for m in history if m.sender and not m.is_bot})

    def _bot_spoke_last(self, history: list[StoredMessage]) -> tuple[bool, float]:
        """群里最后一条是不是机器人说的？距现在多少秒？

        这是比"有没有线程"更强也更便宜的信号：机器人刚说完话，
        下一个开口的人几乎一定是在跟它说话——哪怕隔了十几分钟、
        哪怕换了个昵称来说。而且只要有别人插一句，这个条件立刻不成立。
        """
        if not history:
            return False, 0.0
        last = history[-1]
        if not last.is_bot:
            return False, 0.0
        return True, max(0.0, time.time() - last.created_at)

    def handle_aggregated(self, msg: IncomingMessage) -> None:
        """被 @ 之后先收集，不立刻回。详见 src/aggregate.py 的说明。"""
        ok, reason = self.gate.prefilter(msg.group, msg.msg_key, is_bot=msg.is_bot)
        if not ok:
            logging.debug("丢弃 [%s/%s]：%s", msg.group, msg.sender, reason)
            return

        text, mentioned = self.gate.mention(msg.content)
        # 可信会话（自己的私聊）不用 @：那本来就是你在跟自己说话
        trusted = self.gate.is_trusted_chat(msg.group)
        trigger = mentioned or not self.cfg.require_at or trusted
        how = "被 @ 了"

        # 引用是群里唯一能明确指向某条消息的动作，优先拿它做路由
        quote_nick = (msg.quote_nickname or "").strip()
        quoted_bot = bool(quote_nick) and quote_nick in self.cfg.bot_names
        quoted_other = bool(quote_nick) and not quoted_bot

        if not trigger:
            # 1) 正在收集的会话：把后续并进去（这就是「对 @ 之后的消息做整合」）
            if self.agg.is_collecting(msg.group):
                if quoted_other:
                    logging.info("「%s」引用的是别人（%s），不并入本次发言",
                                 msg.sender, quote_nick)
                    return
                if not self.agg.followup(msg.group, msg.sender, msg.content.strip()):
                    logging.debug("丢弃 [%s/%s]：该会话只吸收发起者本人的后续消息",
                                  msg.group, msg.sender)
                    return
                self.store.add(msg.group, msg.sender, msg.content.strip(), msg.msg_key)
                logging.info("追加到本次发言 [%s] %s：%r | %s",
                             msg.group, msg.sender, msg.content.strip()[:40],
                             self.agg.wait_hint(msg.group))
                return

            # 2) 没 @：判断算不算在跟机器人接着聊
            if quoted_bot:
                trigger, how = True, f"引用了机器人（{quote_nick}）"
                text = msg.content.strip()
            elif self.book.enabled:
                history = self.store.recent(msg.group)
                verdict = self.book.judge(
                    msg.group, msg.sender, msg.content,
                    bot_names=self.cfg.bot_names,
                    members=self._members(msg.group, history),
                    quote_nickname=quote_nick,
                    bot_spoke_last=self._bot_spoke_last(history)[0],
                    silent_seconds=self._bot_spoke_last(history)[1],
                )
                if not verdict.ok:
                    logging.info("不接 [%s] %s：%r（%.1f 分 | %s）",
                                 msg.group, msg.sender, msg.content.strip()[:30],
                                 verdict.score, verdict.why)
                    return
                trigger, how = True, f"接着聊（{verdict.why}）"
                text = msg.content.strip()
            elif self._in_after_reply_window(msg.group, msg.sender):
                trigger, how = True, "接着刚才的话"
                text = msg.content.strip()
            else:
                logging.debug("丢弃 [%s/%s]：没有 @ 机器人，也没有正在进行的对话",
                              msg.group, msg.sender)
                return

        if self.paused or self.stop.stopped:
            logging.warning(
                "已暂停（%s），不收集消息",
                "群内指令" if self.paused else "data/STOP 文件",
            )
            return
        ok, reason = self.gate.limiter.check(msg.group)
        if not ok:
            logging.warning("丢弃 [%s]：%s", msg.group, reason)
            return

        # 历史必须在把这条落库之前取，否则同一个群友的同一句话
        # 会在 prompt 里既出现在上下文、又出现在当前消息里。
        history = self.store.recent(msg.group)
        turn = self.agg.trigger(msg.group, msg.sender, text, history)
        # 不是明确 @ 进来的（靠启发式判定接上的），允许模型自己说「不是跟我说的」
        turn.may_decline = not mentioned
        if quoted_bot:
            turn.quote_nickname = quote_nick
            turn.quote_content = msg.quote_content
        if text:
            self.store.add(msg.group, msg.sender, text, msg.msg_key)

        logging.info(
            "%s [%s] %s：%r | 开始观察，静默 %.0fs 后整合回复（当前 %d 条）",
            how, msg.group, msg.sender, text or "（空）",
            self.cfg.aggregate.quiet_seconds, len(turn.items),
        )

    def flush_turns(self) -> None:
        """把「对方已经说完」的会话整合成一次回复。"""
        for turn in self.agg.due():
            try:
                self.flush_turn(turn)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001
                logging.exception("整合回复失败：%s", exc)

    def flush_turn(self, turn: Turn) -> None:
        if self.paused or self.stop.stopped:
            logging.warning("已暂停，丢弃 [%s] 的 %d 条待整合消息",
                            turn.group, len(turn.items))
            return

        ok, reason = self.gate.limiter.check(turn.group)
        if not ok:
            logging.warning("丢弃 [%s] 的待整合消息：%s", turn.group, reason)
            return

        empty = not turn.has_content()
        body = turn.body()
        logging.info("观察结束 [%s]：整合 %d 条（跨度 %.1fs）-> 开始思考",
                     turn.group, len(turn.items), turn.elapsed())

        persona_name, persona_prompt = load_persona(turn.group)

        # 连续对话第几轮（让模型知道这是接着说的，不是新话题）
        thread = self.book.get(turn.group, turn.trigger_sender)
        turn_no = (thread.turns + 1) if thread else 0

        prompt = build_prompt(
            persona_prompt, self.hist_block(turn.history),
            turn.trigger_sender, body, turn.group,
            multi=turn.is_multi(), empty=empty,
            memes_block=self.meme_block(body, turn.history),
            mood_line=self.mood.hint(turn.group, turn.trigger_sender),
            turn_no=turn_no,
            quote_nick=turn.quote_nickname,
            may_decline=turn.may_decline,
        )

        try:
            reply = self.brain.ask(prompt, user=turn.trigger_sender, group=turn.group)
        except BrainError as exc:
            logging.error("生成失败：%s", exc)
            return

        # 靠启发式接上的消息，允许模型自己说「这条不是跟我说的」
        if turn.may_decline and reply.strip() in {"-", "－", "—", "–"}:
            logging.info("模型判断这条不是在跟它说（%r），跳过回复",
                         body[:30])
            self.book.close(turn.group, turn.trigger_sender)
            return

        self.deliver(turn.group, turn.trigger_sender, body, reply, empty_trigger=empty)

    # ------------------------------------------------------------------ 发送
    def deliver(self, group: str, sender: str, text: str, reply: str,
                *, empty_trigger: bool = False) -> None:
        """统一出口：解析心情值 -> 按人味润色 -> 发送或存草稿 -> 更新线程。"""
        # 1) 心情值。模型在同一次回复末尾附了 [mood:N] 就按它调整，
        #    没有就不动——这样不额外多花一次调用。
        if self.mood.enabled:
            reply, delta, note = parse_mood_tag(reply)
            if delta or note:
                self.mood.adjust(group, sender, delta, note)

        # 2) 人味润色
        try:
            style = load_style()
            reply = apply_style(reply, style, empty_trigger=empty_trigger)
        except Exception as exc:  # noqa: BLE001
            logging.warning("人味润色失败，按原样发送：%s", exc)
        reply = (reply or "").strip()
        if not reply:
            logging.warning("润色后回复为空，放弃发送")
            return

        persona_name, _ = load_persona(group)

        if not self.cfg.auto_send:
            draft = self.drafts.add(group, sender, text, reply)
            print("\n" + "=" * 68)
            print(f" 草稿 #{draft.id}   群：{group}   来自：{sender}")
            print("-" * 68)
            print(f" 对方：{text}")
            print(f" 拟回复：{reply}")
            print("=" * 68)
            print(f" 确认发送：python approve.py send {draft.id}")
            print(f" 丢弃    ：python approve.py reject {draft.id}\n")
            return

        # 随机延迟（像真人）+ 补足最小间隔。
        # 开机补回时可能连着好几条，只靠随机延迟会撞上 min_interval，
        # 所以这里取两者较大值，保证不会被静默丢掉。
        delay = max(
            self.gate.limiter.reply_delay(),
            self.gate.limiter.seconds_until_allowed(group),
        )

        # 长回复拆成几条发。微信群里没人会发一整屏文字。
        parts = split_reply(reply, self.cfg.split)
        if len(parts) > 1:
            logging.info("回复 %d 字，拆成 %d 条发", len(reply), len(parts))

        logging.info("等待 %.1fs 后发送（随机节奏 + 最小间隔）", delay)
        time.sleep(delay)

        for index, part in enumerate(parts):
            if index:
                gap = self.gate.limiter.reply_delay_range(self.cfg.split.delay)
                logging.info("第 %d/%d 条，间隔 %.1fs", index + 1, len(parts), gap)
                time.sleep(gap)
            try:
                self.adapter.send(group, part)
            except Exception as exc:  # noqa: BLE001
                logging.error("发送失败（第 %d/%d 条）：%s", index + 1, len(parts), exc)
                return
            # 每条都记一次数：一小时上限算的是**实际发出的消息条数**，
            # 一条长回复拆成三条就是三条，这样风控口径才和真实观感一致。
            self.gate.limiter.commit(group)
            self.store.add(
                group, persona_name, part,
                make_msg_key(group, "__bot__", part, str(time.time())),
                is_bot=True,
            )
        logging.info("已回复 [%s]：%s", group, " ⏎ ".join(parts))
        # 记下这次回复：之后一段时间里这个人接着说话就不用再 @ 了
        self._last_reply[group] = (time.time(), sender)
        # 线程模式：把这次来回并进这个人的对话线程
        if self.book.enabled:
            t = self.book.open(group, sender, text or "", reply)
            logging.debug("线程 [%s] %s：第 %d 轮", group, sender, t.turns)
            self.book.save()          # 落盘，重启不丢

    # ------------------------------------------------------------------ 维护
    def maintain(self) -> None:
        now = time.time()

        # config.yaml 是启动时读一次的；改完不重启，机器人还在用旧配置，
        # 但界面上看起来「已经改好了」。这里主动提醒一句，省得猜。
        if now >= self._next_cfg_check:
            self._next_cfg_check = now + 60
            try:
                stamp = CONFIG_PATH.stat().st_mtime
                if self._cfg_mtime and abs(stamp - self._cfg_mtime) > 1:
                    logging.warning(
                        "config.yaml 改过了，但配置只在启动时读一次 —— "
                        "要生效得停一次再启动（persona.yaml / memes.yaml 是实时读的，不用重启）"
                    )
                self._cfg_mtime = stamp
            except OSError:
                pass

        # 梗库是给人随时改的，改了不用重启
        if now >= self._next_meme_reload:
            self._next_meme_reload = now + 60
            try:
                if self.memes.changed_on_disk():
                    self.memes.reload()
                    logging.info("梗库已重新加载：%d 条", len(self.memes.entries))
            except Exception as exc:  # noqa: BLE001
                logging.warning("重载梗库失败：%s", exc)

        # 梗词库的定时联网更新。默认 14 天一次，所以这里大部分时候只是读一下
        # 状态文件就返回。放在维护循环里而不是单独开线程：它本来就是低频、
        # 可以等的事，而机器人主循环每轮都有空闲时间。
        if now >= self._next_meme_update:
            self._next_meme_update = now + max(5, self.cfg.memes_update.check_minutes) * 60
            try:
                if meme_feed.due(self.cfg.memes_update, ROOT):
                    logging.warning("梗库该更新了（距上次超过 %.0f 天），开始联网抓取",
                                    self.cfg.memes_update.interval_days)
                    summary = meme_feed.update(self.memes, self.brain,
                                               self.cfg.memes_update, ROOT)
                    logging.warning("梗库更新结束：%s", summary)
            except Exception as exc:  # noqa: BLE001
                logging.warning("梗库自动更新出错（不影响聊天）：%s", exc)

        if now < self._next_purge:
            return
        self._next_purge = now + max(1, self.cfg.history.purge_interval_min) * 60
        deleted = self.store.purge()
        removed = self.drafts.purge_old(30)
        if deleted or removed:
            logging.info("清理完成：历史消息 %d 条，旧草稿 %d 条", deleted, removed)

    # ------------------------------------------------------------------ 循环
    def run(self) -> None:
        logging.info("机器人启动 | 模式=%s | 白名单群=%s | 每群每小时上限=%d",
                     self.cfg.mode, self.cfg.whitelist_groups or "（空，不会回复任何群）",
                     self.cfg.rate_limit.per_group_per_hour)
        if self.agg.enabled:
            logging.info(
                "连续消息聚合：开 | 静默 %.0fs 后回复 | 最长等 %.0fs | 最多 %d 条 | 吸收范围=%s",
                self.cfg.aggregate.quiet_seconds, self.cfg.aggregate.max_wait_seconds,
                self.cfg.aggregate.max_messages, self.cfg.aggregate.followup_sender,
            )
        else:
            logging.info("连续消息聚合：关（收到 @ 立即回复）")
        if self.cfg.split.enabled:
            logging.info("分段发送：开 | 超过 %d 字才拆 | 最多 %d 条 | 间隔 %.1f~%.1fs",
                         self.cfg.split.min_chars, self.cfg.split.max_parts,
                         self.cfg.split.delay[0], self.cfg.split.delay[1])
        else:
            logging.info("分段发送：关（一次发完）")
        logging.info("梗词库：%s | %d 条",
                     "开" if self.cfg.memes.enabled else "关", len(self.memes.entries))
        if not self.cfg.whitelist_groups:
            logging.warning("白名单为空：机器人不会回复任何群。请在 config.yaml 里配置。")

        # 急停标记还在的话，机器人会跑但**一句话都不回** —— 这在外面完全看不出来，
        # 所以必须显式喊一声，并且给出解法。实测因为没喊这一句，白排查了一轮。
        if self.stop.stopped:
            logging.error(
                "！！急停标记还在（%s）—— 机器人会正常轮询，但**不会回复任何消息**。\n"
                "    这是上次急停/点「停止机器人」留下的。两种解法：\n"
                "      1) 删掉 %s 然后重启（用 start.cmd 启动会自动删）\n"
                "      2) 在群里发 \\start 恢复",
                self.stop.path, self.stop.path,
            )

        other = self._other_instance()
        if other:
            logging.error(
                "已经有一个机器人在跑了（pid %d）。同一个微信只能有一个实例，"
                "否则两边会互相抢窗口、消息被其中一边吞掉，谁都回不了。本次直接退出。\n"
                "    要重启：先跑 stop.cmd，或者删掉 %s",
                other, self.pid_path,
            )
            raise SystemExit(3)

        self._write_pid()

        interval = getattr(self.adapter, "POLL_INTERVAL", 3.0)
        # 待处理队列。
        # 聚合模式下每轮把队列**全部**处理掉（这些都是纯内存操作，很快），
        # 否则一波消息要花好几秒才进收集器，静默计时会被误判。
        # 非聚合模式下仍然每轮只处理一条：连珠炮式回复既不像真人，
        # 也会撞上最小间隔限制。
        pending: deque = getattr(self, "_pending", deque())
        self._pending = pending

        while True:
            # 暂停期间**仍然要轮询**：否则收不到 \\start，就再也恢复不了了。
            # 是否回复由 handle() 判断，这里不拦。
            try:
                pending.extend(self.adapter.fetch())
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001
                logging.exception("轮询出错：%s", exc)

            # 后台线程的产出（agent 结果、开程序回执）在这里发出去。
            # 放在处理 pending 之前：它们已经等了一会儿了。
            self._drain_outbox()

            if self.agg.enabled:
                while pending:
                    msg = pending.popleft()
                    try:
                        self.handle(msg)
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        logging.exception("处理消息出错：%s", exc)
                try:
                    self.flush_turns()
                except KeyboardInterrupt:
                    raise
            elif pending:
                msg = pending.popleft()
                try:
                    self.handle(msg)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logging.exception("处理消息出错：%s", exc)

            self.maintain()
            time.sleep(interval)

    # ------------------------------------------------------------------ 进程
    def _other_instance(self) -> int:
        """已经有一个机器人在跑的话返回它的 pid，否则返回 0。

        为什么必须挡住：两个实例会同时抢微信窗口、各自维护自己的「已读水位」，
        结果是消息被其中一个吞掉、两边都不回复；而且第二个实例可能用别的解释器
        启动，急停脚本按路径过滤根本杀不掉它。
        （实际踩过：一个用 venv 的 python、一个用 DSH 自带的 python 同时在跑。）
        """
        try:
            pid = int(self.pid_path.read_text(encoding="ascii").strip() or 0)
        except (OSError, ValueError):
            return 0
        if pid <= 0 or pid == os.getpid():
            return 0
        if os.name != "nt":
            try:
                pid_alive(pid)
                return pid
            except OSError:
                return 0
        import ctypes

        SYNCHRONIZE = 0x00100000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return 0
        try:
            # 等 0 毫秒还等得到，说明它已经退出了
            return 0 if kernel32.WaitForSingleObject(handle, 0) == 0 else pid
        finally:
            kernel32.CloseHandle(handle)

    def _write_pid(self) -> None:
        """写 data/bot.pid，供设置界面和急停脚本判断机器人是否在跑。

        这里只判断「这个进程还在不在」，不保证微信链路是通的——
        链路状态看日志更准。
        """
        try:
            self.pid_path.write_text(str(os.getpid()), encoding="ascii")
        except OSError as exc:
            logging.warning("写 pid 文件失败：%s", exc)

    def close(self) -> None:
        try:
            self.pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            self.mood.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.adapter.close()
        finally:
            self.store.close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def cmd_check(cfg: Config) -> int:
    print("配置自检")
    print("-" * 60)
    print(f"  模式            : {cfg.mode}")
    print(f"  白名单群        : {cfg.whitelist_groups or '（空！机器人不会回复）'}")
    print(f"  机器人昵称      : {cfg.bot_names}")
    print(f"  保留策略        : {cfg.history.retention} "
          f"({cfg.history.effective_retention_days} 天)")
    print(f"  每群条数上限    : {cfg.history.per_group_max}")
    print(f"  上下文读取上限  : {cfg.history.read_limit}")
    agg = cfg.aggregate
    print(f"  连续消息聚合    : {'开' if agg.enabled else '关'}"
          + (f"（静默 {agg.quiet_seconds:g}s / 最长 {agg.max_wait_seconds:g}s / "
             f"最多 {agg.max_messages} 条 / 吸收={agg.followup_sender}）"
             if agg.enabled else ""))
    style = load_style()
    print(f"  颜文字权重      : {style.kaomoji_rate:.0%}"
          f"（池 {len(style.kaomoji)} 个）" if style.enabled else "  颜文字          : 关")
    print(f"  去客服腔        : {'开' if style.strip_boilerplate else '关'}")
    sp = cfg.split
    print(f"  分段发送        : {'开' if sp.enabled else '关'}"
          + (f"（>{sp.min_chars} 字才拆 / 最多 {sp.max_parts} 条）" if sp.enabled else ""))
    book = MemeBook.load(cfg.memes, ROOT)
    print(f"  梗词库          : {'开' if cfg.memes.enabled else '关'} | {len(book.entries)} 条"
          f" | 命中上限 {cfg.memes.max_entries} 个")
    print(f"  历史单条截断    : {cfg.history.per_message_chars} 字")
    print(f"  dsh profile     : {cfg.dsh.profile}")
    print(f"  dsh 启动器      : {cfg.dsh.command}")
    if not Path(cfg.dsh.command).exists():
        print("  !! 找不到 dsh 启动器，请检查 config.yaml 的 dsh.command")
        return 2

    name, prompt = load_persona(cfg.whitelist_groups[0] if cfg.whitelist_groups else None)
    print(f"\n人设昵称：{name}")
    print("人设提示词：")
    for line in prompt.splitlines()[:6]:
        print(f"  {line}")

    print("\n调用大脑测试（会真实联网）...")
    brain = Brain(cfg.dsh, cwd=cfg.data_dir / "agent-cwd")
    try:
        reply = brain.ask(
            build_prompt(prompt, "", "自检", "你好，简单介绍一下你自己，一句话", "自检群")
        )
    except BrainError as exc:
        print(f"  !! 失败：{exc}")
        return 3
    print(f"  回复：{reply}")
    return 0


def cmd_groups(cfg: Config) -> int:
    """列出微信当前会话，方便把群名准确抄进 config.yaml。"""
    adapter = make_adapter(cfg)
    lister = getattr(adapter, "list_sessions", None)
    if not lister:
        print("当前适配器不支持列出会话")
        return 1

    rows = lister()
    adapter.close()
    if not rows:
        print("没有读到会话。请确认微信已登录、且窗口没有被最小化。")
        return 1

    print("=" * 66)
    print("微信当前会话（群名要一字不差地抄进 config.yaml）")
    print("=" * 66)
    for name, unread, ctype in rows:
        tag = f"[{ctype}]" if ctype else ""
        badge = f"  未读 {unread}" if unread and unread != "0" else ""
        print(f"  {name} {tag}{badge}")

    print("\n" + "-" * 66)
    print("把下面这段直接覆盖 config.yaml 里的 whitelist_groups: [] 即可")
    print("（记得删掉你不想让机器人说话的群）")
    print("-" * 66)
    print("whitelist_groups:")
    for name, _, _ in rows:
        print(f'  - "{name}"')
    print()
    return 0


def cmd_probe(cfg: Config) -> int:
    adapter = make_adapter(cfg)
    probe = getattr(adapter, "probe", None)
    if not probe:
        print("当前适配器不支持探测")
        return 1
    probe()
    adapter.close()
    return 0


def cmd_selftest(cfg: Config) -> int:
    """轮询链路自测：读白名单群的消息并核对字段映射。只读，不发送。"""
    print("=" * 66)
    print("轮询链路自测（只读，不发送任何消息）")
    print("=" * 66)

    if not cfg.whitelist_groups:
        print("!! 白名单为空，没有可读的群。请先在 config.yaml 里填 whitelist_groups。")
        return 1

    adapter = make_adapter(cfg)
    try:
        print(f"白名单群：{cfg.whitelist_groups}\n")

        rows = adapter.list_sessions() if hasattr(adapter, "list_sessions") else []
        names = [r[0] for r in rows]
        print(f"[1] 读到 {len(rows)} 个会话")
        for group in cfg.whitelist_groups:
            hit = "✓ 在会话列表里" if group in names else "✗ 不在会话列表里（小号可能不在该群）"
            print(f"    {group!r}  {hit}")

        print("\n[2] 逐个群读取消息并核对字段")
        ok_count = 0
        for group in cfg.whitelist_groups:
            print(f"\n  【{group}】")
            if not adapter._chat_with(group):
                print("    !! 切换聊天失败")
                continue
            try:
                msgs = adapter.wx.GetAllMessage() or []
            except Exception as exc:  # noqa: BLE001
                print(f"    !! 读取失败：{exc}")
                continue

            print(f"    消息数：{len(msgs)}")
            text_msgs = []
            for msg in msgs:
                parsed = adapter._parse(msg, group)
                if parsed:
                    text_msgs.append(parsed)

            if not text_msgs:
                print("    !! 没有可解析的文本消息")
                continue

            ok_count += 1
            last = text_msgs[-1]
            print(f"    可解析文本消息：{len(text_msgs)} 条")
            print("    最后一条：")
            print(f"      sender  = {last.sender!r}")
            print(f"      content = {last.content!r}")
            print(f"      msg_key = {last.msg_key!r}")
            print(f"      is_bot  = {last.is_bot}")
            print(f"      raw     = {getattr(last.raw, 'raw', None)!r}")

        print("\n" + "=" * 66)
        if ok_count:
            print(f"链路验证通过：{ok_count}/{len(cfg.whitelist_groups)} 个群能读到并解析消息。")
            print("可以运行 .\\start.cmd 正式启动（draft 模式只出草稿，不发群）。")
            return 0
        print("没有读到任何可解析的消息。请确认：")
        print("  1) 小号已登录且在那个群里；")
        print("  2) 微信主窗口可见（没有最小化、没有收进托盘）；")
        print("  3) 群里确实有消息。")
        print("=" * 66)
        return 4
    finally:
        adapter.close()


def cmd_watch(cfg: Config, seconds: float) -> int:
    """轮询观察：打印白名单群里的新消息，用来核对字段映射。不回复、不发送。"""
    if not cfg.whitelist_groups:
        print("白名单为空，没有可观察的群。请先填 whitelist_groups。")
        return 1
    adapter = make_adapter(cfg)
    watcher = getattr(adapter, "watch", None)
    if not watcher:
        print("当前适配器不支持消息转储")
        adapter.close()
        return 1
    try:
        watcher(seconds)
    finally:
        adapter.close()
    return 0


def cmd_demo(cfg: Config) -> int:
    print("控制台演示模式（不接触微信）。")
    print("输入格式：群名|发送者|内容   例如：  摸鱼群|张三|@蓝色大肥鱼 北京天气怎么样")
    print("只写一段内容时，沿用上一次的群名和发送者。输入 :quit 退出。")
    if cfg.aggregate.enabled:
        print(f"\n注意：聚合已开启，被 @ 之后要等静默 {cfg.aggregate.quiet_seconds:g} 秒"
              f"（或攒够 {cfg.aggregate.max_messages} 条）才会真的回复。")
    print()
    groups = cfg.whitelist_groups or ["演示群"]
    demo_cfg = cfg
    if not cfg.whitelist_groups:
        print(f"（白名单为空，临时把「{groups[0]}」当作白名单群）\n")
        demo_cfg.whitelist_groups = groups

    bot = Bot(demo_cfg, make_adapter(demo_cfg, console=True), console=True)

    # 输入放到线程里读，主循环才有机会推进「静默计时器」——
    # 否则阻塞在 input() 上，聚合永远等不到到期。
    inbox: queue.Queue = queue.Queue()

    def reader() -> None:
        while True:
            try:
                inbox.put(input("> "))
            except (EOFError, KeyboardInterrupt):
                inbox.put(None)
                return

    threading.Thread(target=reader, daemon=True).start()

    last_group, last_sender = groups[0], "张三"
    try:
        while True:
            try:
                line = inbox.get(timeout=0.5)
            except queue.Empty:
                bot.flush_turns()
                continue

            if line is None:
                break
            line = line.strip()
            if line in {":quit", ":q", "quit", "exit"}:
                break
            if not line:
                bot.flush_turns()
                continue

            parts = line.split("|", 2)
            if len(parts) == 3:
                group, sender, content = (p.strip() for p in parts)
            elif len(parts) == 1:
                group, sender, content = last_group, last_sender, line
            elif len(parts) == 2:
                group, content = (p.strip() for p in parts)
                sender = last_sender
            else:
                print("格式：群名|发送者|内容")
                continue
            last_group, last_sender = group, sender

            bot.handle(
                IncomingMessage(
                    group=group,
                    sender=sender,
                    content=content,
                    msg_key=make_msg_key(group, sender, content, str(time.time())),
                )
            )
            bot.flush_turns()
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        bot.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    parser = argparse.ArgumentParser(prog="wechat-bot", description="DSH 微信群助手")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="配置与大脑自检后退出")
    group.add_argument("--demo", action="store_true", help="控制台演示，不接触微信")
    group.add_argument("--groups", action="store_true", help="列出微信会话，方便填 whitelist_groups")
    group.add_argument("--selftest", action="store_true",
                       help="轮询链路自测：读白名单群的消息并核对字段（只读，不发送）")
    group.add_argument("--probe", action="store_true", help="打印 wxauto4 API 结构后退出")
    group.add_argument("--console", action="store_true", help="用控制台代替微信接入")
    parser.add_argument("--purge-now", action="store_true", help="立刻执行一次历史清理后退出")
    parser.add_argument("--watch", nargs="?", type=float, const=90.0, metavar="SECONDS",
                        help="轮询观察新消息（默认 90 秒），不回复不发送")
    args = parser.parse_args(argv)

    cfg = load_config()
    setup_logging(cfg)

    if args.watch:
        return cmd_watch(cfg, args.watch)
    if args.check:
        return cmd_check(cfg)
    if args.demo:
        return cmd_demo(cfg)
    if args.groups:
        return cmd_groups(cfg)
    if args.selftest:
        return cmd_selftest(cfg)
    if args.probe:
        return cmd_probe(cfg)
    if args.purge_now:
        store = HistoryStore(cfg.db_path, cfg.history)
        deleted = store.purge(force=True)
        store.vacuum()
        store.close()
        print(f"清理完成，删除 {deleted} 条历史消息")
        return 0

    bot = Bot(cfg, make_adapter(cfg, console=args.console), console=args.console)
    try:
        bot.run()
    except KeyboardInterrupt:
        print("\n收到中断，正在退出...")
    finally:
        bot.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
