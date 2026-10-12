"""静态检查：把「跑到那行才炸」的错误在提交前抓住。

## 为什么要这个

踩过一次：`send_file()` 里用了 `Path(path)`，但 `wechat_io.py` 没导入 `Path`。
   - import 这个模块不报错
   - 单元导入测试不报错
   - 只有真的走到发截图那一步才炸：`name 'Path' is not defined`
   现象是「截图拍好了但发不出去」，日志里才看得到。

函数体内的 `NameError` 属于**静态可查**的问题，pyflakes 一眼就能找到。
所以把它固化成一条命令，改完代码跑一下：

    venv\\Scripts\\python.exe tools\\check_py.py

除了未定义名字，还会报：未使用的导入/变量、f-string 缺占位符、
重复定义的键、`return` 外面的 `yield` 之类。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    # tools/ 也要查：那里同样是有函数体的真代码（elevated_request.py、
    # sanitize_for_publish.py），而 NameError 那类问题不分目录。
    # 之前漏了这里，结果一个「未使用的导入」在 tools/ 里躺了很久没人发现。
    targets = sorted(
        [p for p in (ROOT / "src").glob("*.py")]
        + [p for p in (ROOT / "tools").glob("*.py")]
        + [p for p in ROOT.glob("*.py")]
    )
    if not targets:
        print("没找到要检查的 .py")
        return 1

    cmd = [sys.executable, "-m", "pyflakes", *[str(p) for p in targets]]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except FileNotFoundError:
        print("没装 pyflakes。装一下：venv\\Scripts\\python.exe -m pip install pyflakes")
        return 2

    out = (proc.stdout or "") + (proc.stderr or "")
    # pyflakes 的输出里带绝对路径，压短一点好读
    for line in out.splitlines():
        print("  " + line.replace(str(ROOT) + "\\", "").replace(str(ROOT), "."))

    print()
    if proc.returncode == 0:
        print(f"静态检查通过（{len(targets)} 个文件）")
        return 0
    print(f"静态检查发现问题（{len(targets)} 个文件）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
