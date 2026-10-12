"""看一条文本会被机器人当成什么 —— 指令匹配的「拍片机」。

## 为什么要有它

群里发一条没反应、或者回的内容不对，光看聊天界面是查不出来的：
@ 有没有识别、走的是公开指令还是控制指令、指令词剥干净没有、
参数解析成了什么 —— 中间任何一步错了，现象都只是「它回的怪怪的」。

这个脚本把每一步的中间结果都打印出来，并且**用的是机器人同一份代码**
（同一个 Gatekeeper、同一个 `strip_command_words`、同一个 `parse_args`），
所以「脚本说会怎样」和「机器人实际怎样」不会分叉。

## 用法

    :: 分析一条
    venv\\Scripts\\python.exe tools\\check_command.py "@蓝色大肥鱼 /查询 原神 137602053 可莉"

    :: 分析多条
    venv\\Scripts\\python.exe tools\\check_command.py "文本1" "文本2"

    :: 从标准输入逐行读（管道 / 重定向都行）
    type 待查.txt | venv\\Scripts\\python.exe tools\\check_command.py -

    :: 只输出一行结论，方便脚本里比对
    venv\\Scripts\\python.exe tools\\check_command.py --brief "@机器人 /查询 137602053"

    :: 换一个群（影响白名单 / 可信会话的判断）
    venv\\Scripts\\python.exe tools\\check_command.py --group 群名 "文本"

    :: 换成你自己的配置
    venv\\Scripts\\python.exe tools\\check_command.py --config 别的.yaml "文本"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import enka, mona                      # noqa: E402
from src.config import load_config              # noqa: E402
from src.policy import Gatekeeper               # noqa: E402

#: 跟聊天有关的类别（这些走 AI）；其余是指令
CHAT = "chat"


def analyze(text: str, cfg, group: str, sender: str = "路人") -> dict:
    """把一条文本判定成结构化结果。**只读，不改任何状态。**

    判定顺序**必须和 `Bot.handle` 一致**，否则脚本会说谎：

        1. 公开指令（`/` 开头 + 严格指令词）-> **要求白名单群**
        2. 控制指令（群前缀 `\\` / 私聊 `/`）-> **不要求白名单**
           （这是故意的：主人要能从任何群把机器人停下来）
        3. 都不匹配 -> 普通聊天，要求白名单

    白名单只在 1 和 3 上判。一开始我把白名单放在最前面，
    结果「不在白名单的群里也能用的控制指令」被脚本说成「被丢弃」。
    """
    gate = Gatekeeper(cfg)
    out: dict = {
        "text": text,
        "group": group,
        "steps": [],
        "kind": CHAT,
        "detail": {},
    }

    def step(name: str, value: str) -> None:
        out["steps"].append((name, value))

    in_whitelist = group in (cfg.whitelist_groups or [])
    step("白名单", "在（共 %d 个）" % len(cfg.whitelist_groups)
         if in_whitelist else "**不在** —— 公开指令和聊天会被丢弃")

    trusted = gate.is_trusted_chat(group)
    step("可信会话", "是（免 @、免口令）" if trusted else "不是")

    cleaned, mentioned = gate.mention(text)
    step("@ 机器人", ("提到了，去掉 @ 之后是 %r" % cleaned) if mentioned
         else "没有提到")
    out["detail"]["mentioned"] = mentioned
    out["detail"]["cleaned"] = cleaned

    # ---- 1) 公开指令（群友也能用，但要求白名单群）------------------------
    body = gate.public_command(group, text)
    if body is not None:
        kind = gate.strict_command_kind(body, {"enka": cfg.enka.words})
        step("公开指令前缀 %r" % cfg.enka.prefix, "命中，正文 = %r" % body)
        step("指令词严格匹配", "类别 = %s" % (kind or "认不出（会被静默忽略）"))
        if kind != "enka":
            out["kind"] = "ignored"
            out["detail"]["reason"] = "公开指令前缀命中了，但指令词不认"
            _hint_control(out, step, cfg, gate, body)
            return out
        if not in_whitelist:
            step("白名单", "公开指令**必须**在白名单群里，这条会被丢弃")
            out["kind"] = "blocked"
            out["detail"]["reason"] = "公开指令 + 不在白名单"
            return out
        out["kind"] = "enka"
        _fill_enka(out, step, cfg, body, public=True, trusted=trusted, gate=gate)
        return out

    # 为什么没走公开指令 —— 把它说清楚，省得用户猜
    if not cfg.enka.enabled:
        step("公开指令", "没走 —— 账号查询功能是关的")
    elif not cfg.enka.allow_group_members:
        step("公开指令", "没走 —— 「允许群友查询」是关的")
    elif not mentioned and cfg.require_at and not trusted:
        step("公开指令", "没走 —— 没 @ 机器人（require_at 开着）")
    elif not (cleaned or "").lstrip().startswith(cfg.enka.prefix or "/"):
        step("公开指令", "没走 —— 去掉 @ 之后不是以 %r 开头"
             % (cfg.enka.prefix or "/"))
    else:
        step("公开指令", "没走")

    # ---- 2) 控制指令（要授权 + 口令，**但不要白名单**）-------------------
    ctl = gate.as_command(text, trusted=trusted)
    if ctl is not None:
        words = {"agent": cfg.agent.words, "shot": cfg.screenshot.words,
                 "launch": cfg.launch.words, "brush": cfg.brush.words,
                 "enka": cfg.enka.words}
        action = gate.classify_command(ctl, words)
        step("控制指令", "命中，正文 = %r，类别 = %s" % (ctl, action or "认不出"))
        step("白名单", "控制指令**不做要求**（主人要能从任何群停机）")
        if action is None:
            out["kind"] = "unknown_command"
            out["detail"]["body"] = ctl
            return out
        out["kind"] = "control:" + action
        out["detail"]["body"] = ctl
        if action == "enka":
            # 注意这里传的是 **ctl**（前缀之后的正文），不是去掉 @ 的原文 ——
            # 一开始传错了，`\uid 137602053` 会被解析成 focus='\uid'。
            # 机器人那边用的是 as_command 的返回，所以只有脚本错、机器人没错。
            # kind 也要传对，否则会被 _fill_enka 覆盖成 "enka"。
            _fill_enka(out, step, cfg, ctl, public=False, trusted=trusted,
                       gate=gate, kind="control:enka")
        return out

    step("控制指令", "没命中（没有群前缀 %r）" % cfg.commands.prefix)
    if not in_whitelist:
        out["kind"] = "blocked"
        out["detail"]["reason"] = "不在白名单"
        return out
    out["kind"] = CHAT
    out["detail"]["reason"] = "都不匹配 -> 当普通聊天，交给 AI"
    return out


def _hint_control(out: dict, step, cfg, gate: Gatekeeper, body: str) -> None:
    """`/` 前缀命中了、但指令词不是公开指令 —— 大概率是把控制指令写错了前缀。

    `\\截图` / `\\agent` 那些是**控制指令**（要授权 + 口令），不在 `/` 这套里。
    机器人对这种情况是**静默忽略**的（故意的：免得把控制机制暴露给群里
    随便一个人），所以这里替它解释一句。
    """
    words_ctl = {"agent": cfg.agent.words, "shot": cfg.screenshot.words,
                 "launch": cfg.launch.words, "brush": cfg.brush.words}
    as_ctl = gate.classify_command(body, words_ctl)
    if as_ctl:
        step("提示", "「%s」是**控制指令**，要用群前缀 %r（还要口令）；"
             "这里是公开指令前缀 %r，所以机器人会静默忽略"
             % (as_ctl, cfg.commands.prefix, cfg.enka.prefix))


def _fill_enka(out: dict, step, cfg, body: str, *, public: bool,
               trusted: bool, gate: Gatekeeper, kind: str = "enka") -> None:
    """账号查询这条路的中间结果 —— 这次出 bug 就在这几步。

    `body` 是**前缀之后的正文**（公开指令去掉斜杠、控制指令去掉反斜杠）。
    `kind` 是成功时要写的类别 —— 控制指令那条路是 `control:enka`，
    **不能写死成 `enka`**（一开始写死了，把调用方设的类别盖掉了）。
    """
    cfg_e = cfg.enka
    if not cfg_e.enabled:
        step("结果", "账号查询是关的，不会回")
        out["kind"] = "enka_disabled"
        return

    arg = gate.strip_command_words(gate.strip_secret(body), cfg_e.words)
    step("剥掉指令词后", "%r" % arg)

    game, uid, focus, err = enka.parse_args(arg)
    if err:
        step("参数解析", "失败：%s" % err)
        out["kind"] = "enka_usage"
        out["detail"]["message"] = err
        return
    # 空角色名统一成 None —— 机器人调用 query 时就是传 None，
    # 脚本这边保持一致，免得两边看起来不一样
    focus = focus or None
    step("参数解析", "游戏 = %s，UID = %s，角色 = %r"
         % (game or "（自动识别）", uid, focus or "（看整个展柜）"))

    weights = None
    if cfg_e.score.enabled:
        weights = mona.weights_for(cfg_e.score.stats, cfg_e.score.by_character,
                                   focus)
    if weights:
        step("算分权重", "、".join("%s×%g" % (k, v) for k, v in weights.items()))
    else:
        step("算分权重", "（不出分数）")

    out["detail"].update({"game": game, "uid": uid, "focus": focus,
                          "public": public, "trusted": trusted,
                          "weights": weights})
    out["kind"] = kind


def one_line(res: dict) -> str:
    """一行结论，方便脚本里 grep / 比对。"""
    kind = res["kind"]
    d = res["detail"]
    if kind == "enka":
        return "enka game=%s uid=%s focus=%s" % (
            d.get("game") or "auto", d.get("uid"), d.get("focus") or "-")
    if kind == "blocked":
        return "blocked 不在白名单"
    if kind == "ignored":
        return "ignored %s" % d.get("reason", "")
    if kind == "unknown_command":
        return "unknown_command body=%r" % d.get("body", "")
    if kind.startswith("control:"):
        return "control:%s body=%r" % (kind.split(":", 1)[1], d.get("body", ""))
    if kind == "enka_usage":
        return "enka_usage %s" % d.get("message", "")
    return "chat 走 AI"


def show(res: dict, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps({"text": res["text"], "group": res["group"],
                          "kind": res["kind"], "detail": res["detail"]},
                         ensure_ascii=False))
        return
    print("─" * 72)
    print("输入：%s" % res["text"])
    for name, value in res["steps"]:
        print("  %-18s %s" % (name, value))
    print("  %-18s %s" % ("→ 结论", one_line(res)))
    print()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="看一条文本会被机器人当成什么（用同一份代码判定）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("## 用法")[-1])
    ap.add_argument("texts", nargs="*",
                    help="要分析的文本；写 - 表示从标准输入逐行读；不写则交互式")
    ap.add_argument("--group", default=None,
                    help="当成哪个会话发的（默认取白名单里的第一个群）")
    ap.add_argument("--sender", default="路人", help="发送者（只影响显示）")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"),
                    help="用哪个配置（默认 config.yaml）")
    ap.add_argument("--brief", action="store_true",
                    help="只输出一行结论")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--strict", action="store_true",
                    help="有文本落不到指令上（被丢弃 / 认不出）时退出码 1，"
                         "方便放进脚本里当检查用")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    group = args.group or (cfg.whitelist_groups[0] if cfg.whitelist_groups
                           else "test")

    if not args.texts:
        texts = []
        if sys.stdin is not None and not sys.stdin.isatty():
            texts = [ln.rstrip("\n") for ln in sys.stdin if ln.strip()]
        else:
            print("逐行输入文本，空行结束（Ctrl+Z 再回车 也可以）：")
            try:
                while True:
                    ln = input("> ")
                    if not ln.strip():
                        break
                    texts.append(ln)
            except EOFError:
                pass
    elif args.texts == ["-"]:
        texts = [ln.rstrip("\n") for ln in sys.stdin if ln.strip()]
    else:
        texts = args.texts

    if not texts:
        print("没有输入。用法见 --help")
        return 2

    if not args.brief and not args.json:
        print()
        print("配置：%s" % args.config)
        print("当成「%s」里发的；机器人昵称 %s" % (group, cfg.bot_names))
        print("群前缀 %r / 私聊前缀 %r / 公开指令前缀 %r"
              % (cfg.commands.prefix, cfg.commands.short_prefix,
                 cfg.enka.prefix))
        print("查询触发词：%s" % "、".join(cfg.enka.words))
        print()

    bad = 0
    for t in texts:
        if not t.strip():
            continue
        res = analyze(t, cfg, group, args.sender)
        if args.brief:
            print("%-46s => %s" % (t, one_line(res)))
        else:
            show(res, as_json=args.json)
        if res["kind"] in ("blocked", "unknown_command", "ignored",
                           "enka_usage"):
            bad += 1
    # 默认 0：分析本身成功了。--strict 才把「落不到指令上」当失败。
    if args.strict and bad:
        print("（%d 条没落到指令上）" % bad)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
