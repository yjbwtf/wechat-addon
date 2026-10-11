"""微信接入层。

适配器：
  ConsoleAdapter  不接触微信，从控制台读消息，用于联调与演示
  WxAutoAdapter   wxauto4（免费版），使用轮询方式读消息

为什么用轮询而不是监听
----------------------
官方文档把 `AddListenChat` / `StartListening` 标为 ✨（Plus 付费版专属）。
免费版包里这两个方法**存在**，调用也不报错，但实测回调永远不会触发：

    实测：监听线程存活、主窗口在前台、群里确实发了消息 —— 回调触发 0 次。

所以免费版只能轮询。轮询只用文档里明确属于免费版的 API：

    GetSession()   找有未读的群（info 字段实测为 name/time/content/isnew/new_count/ismute）
    ChatWith()     切到目标群
    ChatInfo()     确认当前窗口
    GetAllMessage()读当前窗口的消息
    SendMsg()      发送

消息对象实测字段（FriendTextMessage）：

    msg.type    = 'text'
    msg.sender  = '大号'
    msg.content = '@蓝色大肥鱼\u2005测试'
    msg.id      = '9fdffb36...'      # 唯一 ID，用于去重
    msg.hash    = 'beca7fd7...'
    msg.raw     = {'chat_type': 'group', 'chat_name': 'test',
                   'group_member_count': 3, 'type': 'text',
                   'src': 'friend'|'self', 'content': ..., 'sender': ...}

注意 `\\u2005` 是微信 @ 后面那个特殊空格，识别 @ 时必须一并剥掉。

轮询会抢占微信窗口焦点（ChatWith 会切窗口），所以这台机器最好专用于跑机器人。
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .policy import make_msg_key, strip_mention

log = logging.getLogger("wechat-bot.io")


@dataclass
class IncomingMessage:
    group: str
    sender: str
    content: str
    msg_key: str
    is_bot: bool = False
    # 内容指纹（发送者 + wxauto 的 hash）。用于第二重去重：
    # GetAllMessage 返回的是「当前渲染出来的窗口」，同一条消息可能在不同轮次
    # 以不同组合出现，只靠消息 ID 不足以防住"旧消息晚些才渲染出来"的情况。
    fingerprint: str = ""
    # 引用消息。wxauto4 会把引用的两部分单独给出来（实测确认）：
    #   content        = 回复正文（引用部分已被剥掉）
    #   quote_nickname = 被引用的人
    #   quote_content  = 被引用的原文
    # 「引用」是群里唯一能明确指向某条消息的动作，多线程对话靠它区分。
    quote_nickname: str = ""
    quote_content: str = ""
    # 发送者的身份资料（微信号 / 备注 / 微信昵称 / 群昵称）。
    # **只在指令消息上才去解析** —— 取资料要点一下头像，约 400ms，
    # 普通聊天没必要付这个代价。
    identity: dict = field(default_factory=dict)
    raw: Any = field(default=None, repr=False)

    @property
    def is_quote(self) -> bool:
        return bool(self.quote_nickname or self.quote_content)


class BaseAdapter:
    POLL_INTERVAL = 3.0

    def fetch(self) -> list[IncomingMessage]:
        raise NotImplementedError

    def send(self, group: str, text: str) -> None:
        raise NotImplementedError

    def send_file(self, group: str, path: str) -> None:
        """把本地文件（图片、文档）发到某个聊天。"""
        raise NotImplementedError

    def resolve_identity(self, msg: IncomingMessage) -> dict:
        """解析发送者身份。默认实现返回空（控制台适配器没有微信资料）。"""
        return {}

    def close(self) -> None:
        pass


def _pick(obj: Any, names: tuple[str, ...], default: Any = None) -> Any:
    """按候选名字取值，兼容不同版本的字段命名。"""
    for name in names:
        try:
            value = getattr(obj, name)
        except Exception:  # noqa: BLE001
            continue
        if value not in (None, ""):
            return value
    if isinstance(obj, dict):
        for name in names:
            if obj.get(name) not in (None, ""):
                return obj[name]
    return default


# --------------------------------------------------------------------------
# 控制台适配器（不碰微信，用来验证大脑链路）
# --------------------------------------------------------------------------
class ConsoleAdapter(BaseAdapter):
    """从 stdin 读消息，格式：群名|发送者|内容。"""

    POLL_INTERVAL = 0.5

    def __init__(self, groups: Iterable[str]) -> None:
        self.groups = list(groups)
        self.sent: list[tuple[str, str]] = []

    def fetch(self) -> list[IncomingMessage]:
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit(0)
        if not line.strip():
            return []
        parts = line.split("|", 2)
        if len(parts) < 3:
            print("格式：群名|发送者|内容", file=sys.stderr)
            return []
        group, sender, content = (p.strip() for p in parts)
        return [
            IncomingMessage(
                group=group,
                sender=sender,
                content=content,
                msg_key=make_msg_key(group, sender, content, str(time.time())),
            )
        ]

    def send(self, group: str, text: str) -> None:
        self.sent.append((group, text))
        print(f"\n>>> 发送到「{group}」：{text}\n")

    def send_file(self, group: str, path: str) -> None:
        self.sent.append((group, f"[文件] {path}"))
        print(f"\n>>> 发送文件到「{group}」：{path}\n")


# --------------------------------------------------------------------------
# wxauto4 适配器（轮询）
# --------------------------------------------------------------------------
class WxAutoAdapter(BaseAdapter):
    POLL_INTERVAL = 3.0
    # 每 N 轮额外读一次所有白名单群（不管有没有未读），
    # 防止未读标记不刷新导致漏消息。
    FULL_SCAN_EVERY = 6          # 默认；可用 config 的 poll.full_scan_every 覆盖

    @property
    def full_scan_every(self) -> int:
        """每几轮做一次全量读取。

        调成 1 = 每轮都读所有白名单群，**不会漏消息**，代价是每轮都切
        微信窗口（每 POLL_INTERVAL 秒抢一次焦点）。
        调大 = 省窗口切换，但漏读窗口变长（默认 6 轮 ≈ 18 秒）。
        """
        try:
            return max(1, int(getattr(self.cfg.poll, "full_scan_every", 6)))
        except Exception:       # noqa: BLE001
            return self.FULL_SCAN_EVERY

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.groups = list(cfg.whitelist_groups)
        from wxauto4 import WeChat  # 延迟导入，方便无微信环境跑测试

        self.wx = WeChat()
        # 两重去重：
        #   _seen      —— 消息 ID（微信自带，实测跨轮次稳定）
        #   _seen_fp   —— 内容指纹（发送者 + hash）
        #
        # 为什么需要两重：GetAllMessage() 返回的是**当前渲染出来的窗口**，
        # 不是稳定的消息流。实测同一个群第一次读到 15 条、8 秒后只读到 13 条，
        # 也就是说某条消息可能这一轮没渲染、下一轮才冒出来。
        # 只靠 ID 去重时，这种"迟到的旧消息"会被当成新消息回复（实测踩过）。
        self._seen: set[str] = set()
        self._seen_fp: set[str] = set()
        # 身份资料缓存：key=(群, 发送者)。备注/昵称基本不变，缓存住省掉点头像的开销。
        self._identity_cache: dict[tuple[str, str], dict] = {}
        # 机器人自身名称缓存（识别「读到自己」的错误读取）
        self._self_names_cache: set[str] | None = None
        # 微信号锁：(群, 发送者) -> 第一次验证过的微信号。
        # 和读缓存分开存 —— 缓存会被清空重读，锁不能跟着丢。
        self._id_locks: dict[tuple[str, str], str] = {}
        self._baselined: set[str] = set()
        # 每个群「上一轮窗口」的 (sender, content) 序列。新消息靠它做序列重叠
        # 判断 —— **不依赖消息 id**，因为 wxauto4 给不出稳定的 id。
        self._prev_window: dict[str, list[tuple[str, str]]] = {}
        self._cycle = 0
        self.listening = False  # 免费版监听不可用，保留属性便于上层判断
        log.info("wxauto4 就绪（轮询模式），白名单群：%s", self.groups)

    # ------------------------------------------------------------ 会话
    def list_sessions(self) -> list[tuple[str, str, str]]:
        """列出当前会话，返回 [(名称, 未读数, 类型)]。"""
        out: list[tuple[str, str, str]] = []
        try:
            sessions = self.wx.GetSession() or []
        except Exception as exc:  # noqa: BLE001
            log.error("GetSession 失败：%r", exc)
            return out

        for sess in sessions:
            info = _pick(sess, ("info",), None)
            name = unread = ctype = ""
            if isinstance(info, dict):
                name = str(info.get("name") or info.get("chat_name") or "")
                count = info.get("new_count")
                if count in (None, ""):
                    count = info.get("unread") or info.get("unread_count")
                unread = str(count) if count not in (None, "") else ""
                if (not unread or unread == "0") and info.get("isnew"):
                    unread = "有"
                ctype = str(info.get("chat_type") or info.get("type") or "")
            if not name:
                name = str(_pick(sess, ("name", "chat_name", "nickname"), "") or "")
            if not name:
                continue
            out.append((name, unread, ctype))
        return out

    def _unread_groups(self) -> set[str] | None:
        """返回有未读的白名单群。无法判断时返回 None（表示"都得读一遍"）。"""
        rows = self.list_sessions()
        if not rows:
            return None
        found = set()
        for name, unread, _ in rows:
            if name not in self.groups:
                continue
            if unread == "" or unread == "有" or (unread.isdigit() and int(unread) > 0):
                found.add(name)
        return found

    # ------------------------------------------------------------ 读取
    def _chat_with(self, group: str) -> bool:
        try:
            self.wx.ChatWith(who=group)
        except TypeError:
            self.wx.ChatWith(group)
        except Exception as exc:  # noqa: BLE001
            log.warning("切换聊天失败：%s -> %r", group, exc)
            return False
        return True

    def _chat_name(self) -> str:
        try:
            info = self.wx.ChatInfo()
        except Exception:  # noqa: BLE001
            return ""
        if isinstance(info, dict):
            return str(info.get("chat_name") or info.get("name") or "")
        return str(_pick(info, ("chat_name", "name"), "") or "")

    def _read_group(self, group: str) -> list[IncomingMessage]:
        """读取一个群，用「水位线」判断哪些才是真的新消息。

        为什么不用会话列表的未读标记：实测大群的 new_count/isnew 不可靠，
        按它决定"回不回"会导致大群永远不说话。

        水位线做法：窗口里的消息是有序的，先找出**最后一条我们已见过的消息**
        的位置，它之后的消息才算新的。

        这同时解决了"窗口渲染波动"的问题：某条旧消息这一轮没渲染、下一轮才
        冒出来时，它的位置在已见消息**之前**，所以不会被误判成新消息发出去。
        """
        if not self._chat_with(group):
            return []

        # 先加载更多历史。**这一步是根治性的，不是优化**：
        # 微信默认只在窗口里装最近十几条，而机器人自己发的截图也占位置 ——
        # 实测用户连发 4 条只收到 1 条回复，因为前几条被自己发的截图挤出了窗口。
        # 窗口装得下，这个自我竞争才不存在。
        #
        # 加载更多是在窗口**前面**插入更老的消息，尾部不变，所以序列重叠判定
        # 不会因此误判出新消息。
        load_times = 0
        try:
            load_times = int(getattr(self.cfg.poll, "load_more_times", 2) or 0)
        except Exception:  # noqa: BLE001
            load_times = 2
        if load_times > 0:
            try:
                self.wx.LoadMoreCache(load_times)
            except Exception as exc:  # noqa: BLE001
                # **必须打 WARNING，不能是 DEBUG。**
                # 实测这个 wxauto4 版本里 LoadMoreCache 是坏的
                # （内部调 load_more_message()，而 WeChatMainWnd 没这个方法），
                # 每轮都抛 AttributeError。写在 DEBUG 时日志里完全看不到，
                # 结果就是「以为在加载更多历史，其实一次都没成功」——
                # 窗口一直卡在 17 条，load_more_times 调多少都没用。
                # 只在第一次喊一声，免得每轮刷屏。
                if not getattr(self, "_loadmore_warned", False):
                    self._loadmore_warned = True
                    log.warning(
                        "加载更多历史不可用（%s: %s）——窗口仍然只装最近十几条。"
                        "load_more_times 这个配置目前无效，漏消息要靠别的手段。",
                        type(exc).__name__, exc)

        try:
            raw_msgs = self.wx.GetAllMessage() or []
        except Exception as exc:  # noqa: BLE001
            log.warning("读取消息失败：%s -> %r", group, exc)
            return []

        # 一次遍历同时构建三个**下标对齐**的列表：
        #   parsed —— 解析出的文本消息（过滤掉 system 时间戳、机器人自己的图片）
        #   sig    —— 全部原始消息的签名（含 system/图片，那是唯一能区分先后的信息）
        #   items  —— 与 sig 一一对应，非文本位置为 None
        #
        # 为什么要 items：新消息的下标 k 是在 **sig 里**算出来的，必须用同一个
        # 下标空间去取消息。上一版写成 parsed[k:] —— parsed 是过滤后的子列表，
        # 下标空间完全不同，k 一超过 len(parsed) 就永远是空，
        # 「读 test(0)」成了必然结果（实测 raw=8 parsed=4 却判定 0 条新消息）。
        parsed: list[IncomingMessage] = []
        sig: list[tuple[str, str]] = []
        items: list[IncomingMessage | None] = []
        for raw in raw_msgs:
            item = self._parse(raw, group)
            if item:
                parsed.append(item)
            try:
                who = str(getattr(raw, "sender", "") or getattr(raw, "type", ""))
                what = str(getattr(raw, "content", "") or "")
            except Exception:  # noqa: BLE001
                who, what = "", ""
            sig.append((who, what))
            items.append(item)

        if not parsed and raw_msgs:
            log.info("「%s」读到 %d 条但都不是文本消息", group, len(raw_msgs))

        first_time = group not in self._baselined

        # 用「窗口序列重叠」判断新消息，**不看消息 id**。
        # 为什么不用 id：wxauto4 给不出稳定的消息唯一标识，实测同一条消息
        # 两次读取会算出不同的 key，同时内容相同的消息又算出相同的 key ——
        # 靠身份判断，两个方向都会错。详见 tools/fix_window_overlap.py。
        # 签名用**全部原始消息**，包括 system 时间戳和机器人自己的消息 ——
        # 那些恰恰是唯一能区分先后的信息。用过滤后的 parsed 会丢掉它们，
        # 导致「用户重复发同一句话 + 窗口已满」时签名前后逐字相同、
        # 新消息判定成 0。实测踩过。
        prev = self._prev_window.get(group)

        fresh: list[IncomingMessage] = []
        if first_time or prev is None:
            # 第一轮：只登记不出声
            self._prev_window[group] = sig
        else:
            # 在「上一轮的后缀」和「这一轮的前缀」之间找最大重叠
            k = 0
            for cand in range(min(len(prev), len(sig)), 0, -1):
                if prev[-cand:] == sig[:cand]:
                    k = cand
                    break
            if k:
                # 用 items[k:] —— 和 sig 是同一个下标空间。
                # 千万不能写 parsed[k:]：那是过滤后的子列表，下标对不上，
                # 会静默返回空（实测就是这么白丢了两轮消息的）。
                fresh = [it for it in items[k:] if it is not None]
                for item in fresh:
                    log.info("「%s」判定为新消息（重叠 %d/%d）：%s -> %r",
                             group, k, len(sig), item.sender, item.content[:40])
            else:
                # 窗口整体换掉了（滚动太远或被清空）：保守处理，本轮只登记。
                # 原来这里是静默的，消息被吞时日志里一条都没有，没法定位。
                log.info("「%s」窗口与上一轮无重叠（%d -> %d 条），本轮只登记",
                         group, len(prev), len(sig))
                if prev and sig:
                    log.info("  prev 末尾: %r / sig 开头: %r",
                             prev[-1], sig[0])
            self._prev_window[group] = sig

        # 仍然登记 msg_key / 指纹：prefilter 那层还在用它们做粗筛。
        # 但**新消息的判定已经不看它们了**（见上面的序列重叠）。
        for item in parsed:
            self._seen.add(item.msg_key)
            if item.fingerprint:
                self._seen_fp.add(item.fingerprint)

        # INFO 而非 DEBUG：排查「消息在眼皮底下漏掉」时必须能看到这三行，
        # 它是区分「没读到」「读到了但判成旧的」「窗口撞满无法区分」的唯一依据。
        log.info("「%s」窗口 raw=%d parsed=%d sig=%d",
                 group, len(raw_msgs), len(parsed), len(sig))

        if first_time:
            self._baselined.add(group)
            log.info("已建立「%s」基线，登记 %d 条消息", group, len(parsed))
            # 开机补回：把停机期间被 @ 却没人回的消息挑出来补答
            return self._catch_up_candidates(parsed, group)

        # 防止集合无限增长
        if len(self._seen) > 20000:
            self._seen = set(list(self._seen)[-10000:])
        if len(self._seen_fp) > 20000:
            self._seen_fp = set(list(self._seen_fp)[-10000:])

        return fresh

    def _catch_up_candidates(
        self, parsed: list[IncomingMessage], group: str
    ) -> list[IncomingMessage]:
        """挑出「停机期间被 @ 但明显没人回」的消息，用于开机补回。

        判断依据：窗口最后一条**机器人自己发的**消息之后，出现的、带 @ 的
        别人消息。如果机器人已经回过，那条回复会排在后面，这些消息就落不到
        候选里，所以不会重复补。

        三重上限：只看窗口最后 lookback_messages 条、每群最多 max_per_group 条。
        """
        cfg = self.cfg.catch_up
        if not cfg.enabled or not parsed:
            return []

        tail = parsed[-max(1, cfg.lookback_messages):]

        last_self = -1
        for index, item in enumerate(tail):
            if item.is_bot:
                last_self = index

        candidates: list[IncomingMessage] = []
        for item in tail[last_self + 1:]:
            if item.is_bot:
                continue
            _, mentioned = strip_mention(item.content, self.cfg.bot_names)
            if mentioned:
                candidates.append(item)

        if not candidates:
            return []

        picked = candidates[-max(1, cfg.max_per_group):]
        log.info(
            "「%s」发现 %d 条未回复的 @ 消息，本次补答 %d 条",
            group, len(candidates), len(picked),
        )
        for item in picked:
            log.info("  补答 → %s：%s", item.sender, item.content[:40])
        return picked

    def _parse(self, msg: Any, group: str) -> IncomingMessage | None:
        raw = getattr(msg, "raw", None)
        raw = raw if isinstance(raw, dict) else {}

        mtype = str(raw.get("type") or _pick(msg, ("type",), "") or "").lower()
        sender = str(raw.get("sender") or _pick(msg, ("sender", "nickname"), "") or "")
        content = str(raw.get("content") or _pick(msg, ("content", "text"), "") or "").strip()
        msg_id = str(_pick(msg, ("id",), "") or "")
        src = str(raw.get("src") or "")
        chat_name = str(raw.get("chat_name") or group)

        # 引用消息：content 是回复正文（引用部分 wxauto4 已剥离），
        # 另外两个字段告诉我们引用了谁、引用的是什么。
        quote_nick = quote_content = ""
        if mtype == "quote":
            quote_nick = str(_pick(msg, ("quote_nickname",), "") or "").strip()
            quote_content = str(_pick(msg, ("quote_content",), "") or "").strip()
            log.info("读到引用消息：[%s] %s 引用「%s」-> %r",
                     chat_name, sender, quote_nick, quote_content[:40])
        elif mtype and mtype != "text":
            return None
        if not content:
            return None

        is_bot = src == "self" or sender in {"Self", "self", "我"}
        key = msg_id or make_msg_key(chat_name, sender, content, str(_pick(msg, ("hash",), "")))
        content_hash = str(_pick(msg, ("hash",), "") or content)
        return IncomingMessage(
            group=chat_name,
            sender=sender or "群友",
            content=content,
            msg_key=key,
            is_bot=is_bot,
            fingerprint=f"{sender}\x1f{content_hash}",
            quote_nickname=quote_nick,
            quote_content=quote_content,
            raw=msg,
        )

    def fetch(self) -> list[IncomingMessage]:
        self._cycle += 1
        unread = self._unread_groups()

        # 未读标记只用来决定「这一轮要不要去读这个群」（省得一直抢窗口），
        # **不用来决定要不要回复**——实测大群的 new_count/isnew 不可靠，
        # 拿它当开关会导致大群永远不说话。
        #
        # 轮到读的时候，哪些算新消息由 _read_group 的水位线判断，见那里的说明。
        can_determine = unread is not None
        every = self.full_scan_every
        full = (not can_determine) or (self._cycle % every == 0)
        if full and can_determine:
            log.debug("第 %d 轮：附带一次全量读取", self._cycle)

        out: list[IncomingMessage] = []
        read: list[str] = []
        skipped: list[str] = []
        for group in self.groups:
            has_unread = (not can_determine) or (group in unread)
            if not has_unread and not full:
                skipped.append(group)
                continue
            got = self._read_group(group)
            read.append(f"{group}({len(got)})")
            out.extend(got)
        # 这条日志是为了查「消息在眼皮底下却漏了」——那种情况日志里
        # 一条都没有，完全没法定位。有了它就能区分：
        #   · 某群出现在 skipped 里     -> 被未读门限跳过了，是漏读
        #   · 某群 read 了但 (0)        -> 读了但水位线判成旧的
        #   · 群压根没被读到            -> fetch 没被调用（主循环卡住）
        if read or skipped:
            log.info("轮询第 %d 轮：读 %s%s", self._cycle,
                     "、".join(read) or "无",
                     ("；跳过 " + "、".join(skipped)) if skipped else "")
        return out

    # ------------------------------------------------------------ 发送
    def _verify_target(self, group: str) -> tuple[bool, str]:
        """发送前的目标校验：优先用消息里的 chat_name，其次 ChatInfo。"""
        name = self._chat_name()
        if name:
            return name == group, name
        try:
            msgs = self.wx.GetAllMessage() or []
        except Exception:  # noqa: BLE001
            return False, ""
        for msg in reversed(msgs):
            raw = getattr(msg, "raw", None)
            if isinstance(raw, dict) and raw.get("chat_name"):
                return raw["chat_name"] == group, str(raw["chat_name"])
        return False, ""

    def send(self, group: str, text: str) -> None:
        if not self._chat_with(group):
            raise RuntimeError(f"无法切换到「{group}」")
        ok, actual = self._verify_target(group)
        if not ok:
            raise RuntimeError(f"目标校验失败：期望「{group}」，当前是「{actual or '未知'}」")
        self.wx.SendMsg(text)
        log.info("已发送到「%s」：%s", group, text[:40])

    def send_file(self, group: str, path: str) -> None:
        """把本地文件（截图）发到某个聊天。

        和发文字一样先切换、再核对目标群名，避免发错群——
        截图这种东西发错群比发错一句话严重得多。
        """
        target = Path(path)
        if not target.is_file():
            raise RuntimeError(f"文件不存在：{target}")
        if not self._chat_with(group):
            raise RuntimeError(f"无法切换到「{group}」")
        ok, actual = self._verify_target(group)
        if not ok:
            raise RuntimeError(f"目标校验失败：期望「{group}」，当前是「{actual or '未知'}」")
        resp = self.wx.SendFiles(str(target))
        log.info("已发送文件到「%s」：%s（%s）", group, target.name, resp)

    def _self_names(self) -> set[str]:
        """机器人自己账号的名字，用来识别「读到了自己的资料」这种错误。

        `sender_info()` 偶尔会点到别的头像上 —— 实测出现过给它自己的昵称
        （display_name='<机器人昵称>'，而 sender 明明是群友 '<你的群昵称>'）。
        拿到自己的资料说明这次读取是错的，必须丢掉，不能进缓存。
        """
        if self._self_names_cache is not None:
            return self._self_names_cache
        names: set[str] = {str(n).strip() for n in (self.cfg.bot_names or []) if n}
        for getter in (
            lambda: getattr(self.wx, "nickname", ""),
            lambda: (self.wx.GetMyInfo() or {}).get("nickname", ""),
            lambda: getattr(getattr(self.wx, "root", None), "name", ""),
        ):
            try:
                v = str(getter() or "").strip()
                if v:
                    names.add(v)
            except Exception:  # noqa: BLE001
                pass
        names.discard("")
        self._self_names_cache = names
        if names:
            log.info("机器人自身名称（用于身份校验）：%s", "、".join(sorted(names)))
        return names

    def resolve_identity(self, msg: IncomingMessage) -> dict:
        """拿到发送者的微信号 / 备注 / 微信昵称 / 群昵称。

        做法是调用 wxauto4 消息对象的 `sender_info()`，它返回的字典实测长这样：

            {"display_name": "<你的群昵称>",                      <- 机器人账号自己设的备注
             "nickname": "望舒下清波",
             "group_nickname": "为什么美杜莎的眼睛是最漂亮的",  <- 全群看到的
             "id": "<你的微信号>",                              <- 微信号，改不了
             "region": "瓦利斯群岛和富图纳群岛 "}

        ## 这个接口不可靠，所以有三道校验

        实测见过两种错法：
          1. 给出的是**别人**的资料（点头像点到了机器人自己那条消息上）；
          2. 有时给得出微信号，有时只给 display_name。

        只靠「读一次存起来」是不行的 —— 错的会被当成对的缓存下来，
        而且 `identity_mode: id` 下会直接把人锁在门外。所以：

          - **丢弃读到自己的情况**：display_name/nickname 命中机器人自身名称就重读；
          - **第一次读到微信号就锁住**：以后读到不一样的 id，保留锁定的那个并告警；
          - **只并入可信字段**：宁缺毋滥，缺字段总比字段是错的强。

        只对指令消息调用（约 400ms），结果按 (群, 发送者) 缓存。
        """
        key = (msg.group, msg.sender)
        cached = self._identity_cache.get(key)
        if cached is not None and cached.get("id"):
            # 已经读到过微信号就不用再点头像了，省掉 400ms
            return dict(cached)

        self_names = self._self_names()

        def read_once() -> dict:
            got: dict = {}
            getter = getattr(msg.raw, "sender_info", None)
            if callable(getter):
                try:
                    raw = getter()
                    if isinstance(raw, dict):
                        got = {str(k): v for k, v in raw.items()}
                except Exception as exc:  # noqa: BLE001
                    log.warning("读取 %s 的身份失败（回退到只比昵称）：%r", msg.sender, exc)
            return got

        info = {}
        for attempt in (1, 2):
            info = read_once()
            if not info:
                break
            shown = {str(info.get(k) or "").strip()
                     for k in ("display_name", "nickname")}
            hit_self = {n for n in shown if n and n in self_names}
            if hit_self:
                log.warning(
                    "身份解析读到的是机器人自己（%s），这次结果作废%s",
                    "、".join(sorted(hit_self)),
                    "，重读一次" if attempt == 1 else "，已放弃",
                )
                info = {}
                continue
            break

        # --- 微信号锁定 ---
        # 锁**单独存**，不放在读缓存里。放在缓存里的话，「清掉缓存重读」
        # 会连锁一起清掉，保护就没了 —— 实测就是这么发现的。
        # 已经验证过的微信号只认第一次读到的：sender_info() 会点错头像，
        # 读到不一致说明**整次读取**都不可信（别的字段也可能是别人的），
        # 所以整次作废，而不是只丢掉 id 那一个字段。
        lock = self._id_locks.get(key)
        new_id = str(info.get("id") or "").strip()
        if lock:
            if new_id and new_id != lock:
                log.warning(
                    "身份解析 [%s] %s 读到的微信号 %r 和锁定的 %r 不一致，"
                    "本次读取作废（sender_info 会点错头像）",
                    msg.group, msg.sender, new_id, lock,
                )
                info = {}
        elif new_id:
            self._id_locks[key] = new_id
            log.info("锁定 [%s] %s 的微信号：%s", msg.group, msg.sender, new_id)

        # wxauto 给的消息原始字段里也有一部分，补上不冲突的
        rawdict = getattr(msg.raw, "raw", None)
        if isinstance(rawdict, dict):
            for k in ("sender", "sender_remark"):
                if rawdict.get(k) and k not in info:
                    info[k] = rawdict[k]
        info["sender_seen"] = msg.sender

        if cached:
            # 合并：新读到的字段覆盖旧的，但绝不覆盖锁定的微信号
            merged = dict(cached)
            merged.update(info)
            info = merged
        lock = self._id_locks.get(key)
        if lock:
            info["id"] = lock

        self._identity_cache[key] = dict(info)
        log.info("身份解析 [%s] %s -> %s", msg.group, msg.sender,
                 {k: v for k, v in info.items() if k != "region"})
        return dict(info)

    # ------------------------------------------------------------ 诊断
    def probe(self) -> None:
        """打印真实 API 结构与消息字段，用于核对映射。只读。"""
        print("=" * 70)
        print("wxauto4 探测报告（轮询模式）")
        print("=" * 70)
        print("WeChat 可用方法：")
        print("  " + ", ".join(sorted(m for m in dir(self.wx) if not m.startswith("_"))))

        print("\n--- GetSession() ---")
        rows = self.list_sessions()
        print(f"  会话数：{len(rows)}")
        for name, unread, ctype in rows[:8]:
            print(f"    {name!r}  未读={unread!r} 类型={ctype!r}")

        print("\n--- ChatInfo() ---")
        try:
            print(f"  {self.wx.ChatInfo()!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"  失败：{exc!r}")

        print("\n--- 每个白名单群的消息字段 ---")
        for group in self.groups:
            print(f"\n  【{group}】")
            if not self._chat_with(group):
                print("    切换失败")
                continue
            print(f"    ChatInfo: {self._chat_name()!r}")
            try:
                msgs = self.wx.GetAllMessage() or []
            except Exception as exc:  # noqa: BLE001
                print(f"    读取失败：{exc!r}")
                continue
            print(f"    消息数：{len(msgs)}")
            for msg in msgs[-3:]:
                parsed = self._parse(msg, group)
                print(f"    - 类名={type(msg).__name__}")
                print(f"      raw={getattr(msg, 'raw', None)!r}")
                print(f"      解析={parsed!r}")
        print("\n" + "=" * 70)

    def watch(self, seconds: float = 90.0) -> None:
        """轮询观察模式：只打印新消息，不回复、不发送。"""
        print("=" * 70)
        print(f"观察中（{seconds:.0f} 秒）。请现在到白名单群里 @ 机器人发一条消息。")
        print("只打印，不回复、不发送任何内容。")
        print("=" * 70)

        self.fetch()  # 第一次读取用于建立基线，丢弃历史
        print("基线已建立，从现在起的新消息才会显示。\n", flush=True)

        deadline = time.time() + seconds
        started = time.time()
        last_beat = started
        count = 0
        while time.time() < deadline:
            if time.time() - last_beat >= 30:
                last_beat = time.time()
                print(f"  ...观察中，已 {int(time.time() - started)} 秒，"
                      f"新消息 {count} 条", flush=True)
            try:
                for msg in self.fetch():
                    count += 1
                    print(f"\n--- 第 {count} 条新消息 ---")
                    print(f"  群名   : {msg.group!r}")
                    print(f"  发送者 : {msg.sender!r}")
                    print(f"  内容   : {msg.content!r}")
                    print(f"  消息ID : {msg.msg_key!r}")
                    print(f"  来自自己: {msg.is_bot}")
                    print(f"  raw    : {getattr(msg.raw, 'raw', None)!r}")
            except Exception as exc:  # noqa: BLE001
                log.exception("观察模式读取出错：%s", exc)
            time.sleep(self.POLL_INTERVAL)

        print(f"\n共捕获 {count} 条新消息。")
        print("=" * 70)

    def close(self) -> None:
        pass


def build_adapter(cfg: Config, *, console: bool = False) -> BaseAdapter:
    if console:
        return ConsoleAdapter(cfg.whitelist_groups)
    return WxAutoAdapter(cfg)
