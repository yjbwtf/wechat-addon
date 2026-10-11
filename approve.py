"""草稿确认命令行工具。

用法：
    python approve.py list              列出待确认草稿
    python approve.py show <id>         查看某条草稿
    python approve.py send <id>         确认并发送
    python approve.py send-all          发送全部待确认草稿
    python approve.py reject <id>       丢弃
    python approve.py reject-all        丢弃全部
    python approve.py stop              紧急停止（写入 data/STOP）
    python approve.py resume            恢复运行
"""

from __future__ import annotations

import argparse
import sys
import time

from src.config import load_config
from src.drafts import Draft, DraftStore
from src.policy import StopSwitch
from src.wechat_io import build_adapter


def _fmt(draft: Draft) -> str:
    when = time.strftime("%m-%d %H:%M", time.localtime(draft.created_at))
    return (f"#{draft.id}  [{when}]  {draft.group}  <- {draft.sender}\n"
            f"    对方：{draft.incoming}\n"
            f"    回复：{draft.reply}")


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    parser = argparse.ArgumentParser(prog="approve", description="草稿确认工具")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="列出待确认草稿")
    for name in ("show", "send", "reject"):
        p = sub.add_parser(name, help=f"{name} <id>")
        p.add_argument("id")
    sub.add_parser("send-all", help="发送全部待确认草稿")
    sub.add_parser("reject-all", help="丢弃全部待确认草稿")
    sub.add_parser("stop", help="紧急停止机器人")
    sub.add_parser("resume", help="恢复机器人运行")
    args = parser.parse_args(argv)

    cfg = load_config()
    stop = StopSwitch(cfg.data_dir)

    if args.command == "stop":
        stop.engage("由 approve.py stop 触发")
        print(f"已紧急停止。删除 {stop.path} 或执行 python approve.py resume 恢复。")
        return 0
    if args.command == "resume":
        stop.release()
        print("已恢复运行。")
        return 0
    if args.command == "list":
        drafts = store_list(cfg)
        if not drafts:
            print("没有待确认的草稿。")
            return 0
        for draft in drafts:
            print(_fmt(draft))
            print()
        print(f"共 {len(drafts)} 条。")
        return 0

    store = DraftStore(cfg.data_dir / "drafts" / "pending.jsonl")

    if args.command == "show":
        draft = store.get(args.id)
        if not draft:
            print(f"找不到草稿 {args.id}")
            return 1
        print(_fmt(draft))
        print(f"状态：{draft.status}")
        return 0

    if args.command == "reject":
        draft = store.update(args.id, "rejected")
        print(f"已丢弃 #{args.id}" if draft else f"找不到草稿 {args.id}")
        return 0 if draft else 1

    if args.command == "reject-all":
        count = 0
        for draft in store.list("pending"):
            store.update(draft.id, "rejected")
            count += 1
        print(f"已丢弃 {count} 条草稿。")
        return 0

    if args.command in ("send", "send-all"):
        targets = [store.get(args.id)] if args.command == "send" else store.list("pending")
        targets = [d for d in targets if d]
        if not targets:
            print("没有要发送的草稿。")
            return 1

        adapter = build_adapter(cfg)
        sent = 0
        for draft in targets:
            assert draft is not None
            try:
                adapter.send(draft.group, draft.reply)
            except Exception as exc:  # noqa: BLE001
                print(f"  !! #{draft.id} 发送失败：{exc}")
                continue
            store.update(draft.id, "sent")
            print(f"  已发送 #{draft.id} -> {draft.group}")
            sent += 1
            time.sleep(2)  # 多条之间留间隔，别像机器
        adapter.close()
        print(f"完成，成功发送 {sent}/{len(targets)} 条。")
        return 0 if sent else 1

    return 0


def store_list(cfg) -> list[Draft]:
    return DraftStore(cfg.data_dir / "drafts" / "pending.jsonl").list("pending")


if __name__ == "__main__":
    raise SystemExit(main())
