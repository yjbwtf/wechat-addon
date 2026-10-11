"""设置界面（C 程序）与 YAML 配置之间的桥。

为什么要这一层
--------------
config.yaml 和 persona.yaml 里有大量中文注释，用户是要读的。
用 PyYAML 存回去会把注释全部吃掉，所以这里用 ruamel.yaml 做**往返**
（round-trip）读写：只动被改的那个值，其余字节原样保留。

接口（全是纯文本，C 端好解析）
------------------------------
    python ui_bridge.py dump
        输出若干行：  key \t kind \t value
        以及只读状态：@status.xxx \t str \t value

    python ui_bridge.py apply <文件>
        从文件读同样的 key\tkind\tvalue 行，写回 YAML。
        写坏了自己回滚，并输出 ERROR\t<原因>。

key 形如：
    config:rate_limit.per_group_per_hour      -> config.yaml
    persona:default.prompt                    -> persona.yaml

kind: bool | int | float | str | text | list

value 的转义：\\ -> \\\\   换行 -> \\n   制表 -> \\t   回车 -> \\r
（这样每个值都只占一行，C 端按行解析即可）
"""

from __future__ import annotations

import io
import os
import sqlite3
import sys
from pathlib import Path
from src.procutil import pid_alive

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
PERSONA_PATH = ROOT / "persona.yaml"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "history.sqlite3"
PID_PATH = DATA_DIR / "bot.pid"
STOP_PATH = DATA_DIR / "STOP"
LOG_PATH = DATA_DIR / "logs" / "bot.log"

FILES = {"config": CONFIG_PATH, "persona": PERSONA_PATH}

KINDS = {"bool", "int", "float", "str", "text", "list"}


# --------------------------------------------------------------------------
# 转义
# --------------------------------------------------------------------------
def esc(text: str) -> str:
    return (text.replace("\\", "\\\\")
                .replace("\n", "\\n")
                .replace("\r", "\\r")
                .replace("\t", "\\t"))


def unesc(text: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "n":
                out.append("\n")
            elif nxt == "r":
                out.append("\r")
            elif nxt == "t":
                out.append("\t")
            elif nxt == "\\":
                out.append("\\")
            else:
                out.append(ch)
                out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# --------------------------------------------------------------------------
# ruamel 往返读写
# --------------------------------------------------------------------------
def _yaml():
    from ruamel.yaml import YAML

    y = YAML()
    y.preserve_quotes = True
    y.width = 100000          # 别把长中文提示词折行
    y.allow_unicode = True
    # 和手写风格一致：映射缩进 2，序列在键下面再缩进 2（"- " 对齐在 2 空格处）
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _quoted(items: list[str]) -> list:
    """列表元素统一用双引号包起来。

    群名里可能有 : 、# 、emoji 这类字符，加引号能避免 YAML 解析出意外；
    顺便也和文件原本的写法保持一致。
    """
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString as DQ

    return [DQ(str(item)) for item in items]


def load_doc(path: Path):
    with path.open("r", encoding="utf-8") as fh:
        return _yaml().load(fh)


def dump_doc(doc, path: Path) -> str:
    buf = io.StringIO()
    _yaml().dump(doc, buf)
    return buf.getvalue()


def get_path(doc, dotted: str):
    node = doc
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def set_path(doc, dotted: str, value) -> None:
    """按点分路径写值，并且**保住这个键上的注释**。

    ruamel 把「键前面那一整块注释和空行」存在 CommentedMap.ca.items[key] 里，
    直接 node[key] = value 有概率把这块一起丢掉（实测：改 bot_names 时，
    它上面那行「# 机器人在群里的昵称，用于识别 @」被吃掉了）。
    这个文件的注释是给人看的，掉了就白写了，所以这里显式搬运一次。
    """
    parts = dotted.split(".")
    node = doc
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child

    key = parts[-1]
    ca = getattr(node, "ca", None)
    saved = None
    if ca is not None and hasattr(ca, "items"):
        saved = ca.items.get(key)

    old = node.get(key) if hasattr(node, "get") else None
    if isinstance(old, list) and isinstance(value, list):
        same = len(old) == len(value) and all(
            str(a) == str(b) for a, b in zip(old, value)
        )
        if not same:
            # 列表靠原地替换元素来保留节点身份，但元素上的注释会被清掉
            # （「# 机器人在群里的昵称」那类注释就挂在列表节点上），
            # 所以先把 ca 里的注释存下来，替换完再放回去。
            old_ca = getattr(old, "ca", None)
            keep = dict(old_ca.items) if old_ca is not None and hasattr(old_ca, "items") else None
            old[:] = value
            if keep is not None:
                old_ca.items.clear()
                old_ca.items.update(keep)
        # 内容一样就一个字节都不动——注释、引号、空行全都原样保留
    else:
        node[key] = value

    if saved is not None and ca is not None:
        ca.items[key] = saved


# --------------------------------------------------------------------------
# 取值 / 赋值（带类型）
# --------------------------------------------------------------------------
def value_to_text(value, kind: str) -> str:
    if value is None:
        return ""
    if kind == "bool":
        return "1" if value in (True, "true", "True", 1) else "0"
    if kind == "list":
        if isinstance(value, (list, tuple)):
            return "\n".join(str(v) for v in value)
        return str(value)
    return str(value)


def text_to_value(text: str, kind: str, current=None):
    if kind == "bool":
        return str(text).strip().lower() in {"1", "true", "yes", "on", "是"}
    if kind == "int":
        return int(float(str(text).strip() or 0))
    if kind == "float":
        num = float(str(text).strip() or 0)
        # 6.0 写成 6，免得每次保存都把配置改得越来越难看
        return int(num) if float(num).is_integer() else num
    if kind == "list":
        items = [line.strip() for line in str(text).splitlines()]
        return _quoted([item for item in items if item])
    return str(text)


# --------------------------------------------------------------------------
# 只读状态
# --------------------------------------------------------------------------
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            pid_alive(pid)
            return True
        except OSError:
            return False
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def status_lines() -> list[tuple[str, str, str]]:
    pid = 0
    try:
        pid = int(PID_PATH.read_text(encoding="ascii").strip() or 0)
    except (OSError, ValueError):
        pid = 0
    running = _pid_alive(pid)

    total = 0
    groups = 0
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            total = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
            groups = int(conn.execute(
                "SELECT COUNT(DISTINCT group_id) FROM messages").fetchone()[0])
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass

    rows = [
        ("@status.running", "bool", "1" if running else "0"),
        ("@status.pid", "str", str(pid) if running else ""),
        ("@status.stopped", "bool", "1" if STOP_PATH.exists() else "0"),
        ("@status.history_total", "int", str(total)),
        ("@status.history_groups", "int", str(groups)),
        ("@status.root", "str", str(ROOT)),
        ("@status.log", "str", str(LOG_PATH)),
        ("@status.db", "str", str(DB_PATH)),
        ("@status.config", "str", str(CONFIG_PATH)),
        ("@status.persona", "str", str(PERSONA_PATH)),
    ]
    stop_note = ""
    if STOP_PATH.exists():
        try:
            stop_note = STOP_PATH.read_text(encoding="utf-8").strip().splitlines()[0]
        except OSError:
            stop_note = "存在"
    rows.append(("@status.stop_note", "str", stop_note))
    return rows


def emit(rows: list[tuple[str, str, str]]) -> None:
    out = sys.stdout
    for key, kind, value in rows:
        out.write(f"{key}\t{kind}\t{esc(value)}\n")
    out.flush()


# --------------------------------------------------------------------------
# 命令
# --------------------------------------------------------------------------
def cmd_dump() -> int:
    rows: list[tuple[str, str, str]] = []
    for prefix, path in FILES.items():
        try:
            doc = load_doc(path)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR\t读取 {path.name} 失败：{exc}")
            return 1
        if not isinstance(doc, dict):
            continue
        for key, kind, value in _flatten(prefix, doc):
            rows.append((key, kind, value))
    rows.extend(status_lines())
    emit(rows)
    return 0


def _flatten(prefix: str, doc, trail: str = ""):
    """把嵌套的标量/列表摊平成 key\tkind\tvalue。

    C 端只关心它自己那张表里的字段，多出来的会被忽略，
    所以这里可以放心地把所有非映射节点都吐出去。
    """
    for key, value in doc.items():
        path = f"{trail}.{key}" if trail else str(key)
        if isinstance(value, dict):
            yield from _flatten(prefix, value, path)
            continue
        if value is None:
            kind = "str"
        elif isinstance(value, bool):
            kind = "bool"
        elif isinstance(value, int):
            kind = "int"
        elif isinstance(value, float):
            kind = "float"
        elif isinstance(value, (list, tuple)):
            kind = "list"
        else:
            text = str(value)
            kind = "text" if "\n" in text else "str"
        yield f"{prefix}:{path}", kind, value_to_text(value, kind)


def cmd_apply(argv: list[str]) -> int:
    if not argv:
        print("ERROR\tapply 需要一个输入文件")
        return 1
    src = Path(argv[0])
    try:
        lines = src.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        print(f"ERROR\t读不到 {src}：{exc}")
        return 1

    # 先按文件分组
    wanted: dict[str, list[tuple[str, str, str]]] = {"config": [], "persona": []}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        key, kind, raw = parts[0], parts[1], "\t".join(parts[2:])
        if not key.startswith("@") and ":" in key:
            prefix, path = key.split(":", 1)
            if prefix in wanted and kind in KINDS:
                wanted[prefix].append((path, kind, unesc(raw)))

    # 备份原文，写坏了能回滚
    backups: dict[str, str] = {}
    for prefix, changes in wanted.items():
        if not changes:
            continue
        path = FILES[prefix]
        backups[prefix] = path.read_text(encoding="utf-8")

    written: list[str] = []
    try:
        for prefix, changes in wanted.items():
            if not changes:
                continue
            path = FILES[prefix]
            doc = load_doc(path)
            if not isinstance(doc, dict):
                print(f"ERROR\t{path.name} 顶层不是映射，无法修改")
                raise RuntimeError("bad root")
            for dotted, kind, text in changes:
                current = get_path(doc, dotted)
                set_path(doc, dotted, text_to_value(text, kind, current))
            text = dump_doc(doc, path)
            path.write_text(text, encoding="utf-8")
            written.append(prefix)
    except Exception as exc:  # noqa: BLE001
        for prefix, original in backups.items():
            try:
                FILES[prefix].write_text(original, encoding="utf-8")
            except OSError:
                pass
        print(f"ERROR\t写入失败已回滚：{exc}")
        return 1

    # 写完立刻验证一遍，不合法就整体回滚
    try:
        _validate(written)
    except Exception as exc:  # noqa: BLE001
        for prefix, original in backups.items():
            try:
                FILES[prefix].write_text(original, encoding="utf-8")
            except OSError:
                pass
        print(f"ERROR\t配置不合法，已回滚：{exc}")
        return 1

    print("OK\t" + ",".join(written))
    return 0


def _validate(written: list[str]) -> None:
    sys.path.insert(0, str(ROOT))
    if "config" in written:
        from src.config import load_config

        load_config(CONFIG_PATH)
    if "persona" in written:
        from src.config import load_persona
        from src.style import load_style

        load_persona(None, PERSONA_PATH)
        load_style(PERSONA_PATH)


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    if not argv:
        print("用法: ui_bridge.py dump | apply <文件>")
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "dump":
        return cmd_dump()
    if cmd == "apply":
        return cmd_apply(rest)
    print(f"ERROR\t未知命令 {cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
