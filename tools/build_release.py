"""打包一个发布版（zip），解压到任意路径就能用。

## 包里放什么

只放 **git 跟踪的文件**（"公开项目"的集合）+ `VERSION` + 快速开始。
不放：`config.yaml`（含口令/白名单，首次运行由 start.cmd 从 example 生成）、
`venv/`、`data/`、`.git/`、`__pycache__`。

## 为什么把"面板图"相关两项摘掉

`src/panel.py`、`data/miao/`、图标库都是**本地私有**的（不进版本库 ——
用户明确说过不推）。发布包里没有它们，所以示例配置里的 `image:` 和
UI 里那个开关也一并摘掉，免得新用户看到一个什么都不做的开关。

代码里的钩子是**故意写成能安静退化**的（`try: from . import panel except
ImportError: _panel = None`），留着不影响：不开就是发文字。

用法：
    venv\\Scripts\\python.exe tools\\build_release.py [版本号]
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
VERSION = (sys.argv[1] if len(sys.argv) > 1 else "1.0.0").strip()
TOP = "wechat-addon-%s" % VERSION
DROP_FIELD = "config:enka.image"

QUICKSTART = """\
微信助手 {ver} —— 快速开始
========================================

一、你需要先装好这两样
  1. 微信 PC 版（能正常登录、能手动打开群聊窗口）
  2. DSH（DeepSeek Harness）桌面版 —— 闲聊回复靠它的 headless 跑

二、第一次运行
  1. 把整个文件夹解压到**任意路径**（别放 C:\\Program Files 这种要管理员权限的
     地方，也别放进 DSH 的工作目录里）
  2. 双击 start.cmd
     · 第一次会自动建 venv、装依赖（几分钟）
     · 然后它会提示「已经生成了 config.yaml」并停下来
  3. 用记事本打开 config.yaml，至少改这三处：
       whitelist_groups         你的群名（必须和微信里显示的**完全一致**；
                                不确定就先跑 start.cmd -Groups 看一眼）
       commands.allowed_senders 谁有权发控制指令（建议填微信号）
       commands.secret          口令，别用群名或常见词
  4. 再双击 start.cmd —— 开始跑

三、常用命令
  start.cmd -Check      配置自检 + 静态检查（不碰微信）
  start.cmd -Groups     列出微信里的会话名（填 whitelist_groups 用）
  start.cmd -Demo       控制台演示，完全不碰微信
  start.cmd -UI         打开设置界面（改配置不用手改 yaml）
  stop.cmd              停止机器人

四、出问题先看哪
  data\\logs\\bot.log     运行日志（每步都有）
  start.cmd -Check       配置有没有写错
  README.md              完整文档（很长，按目录查）

（本文件是打包时生成的；README.md 里是完整说明。）
""".format(ver=VERSION)


def sh(args: list[str]) -> str:
    p = subprocess.run(args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=str(ROOT))
    if p.returncode != 0:
        raise SystemExit("命令失败：%s\n%s" % (" ".join(args), p.stderr[:300]))
    return p.stdout


def drop_panel_field(raw: str) -> tuple[str, int]:
    """从 schema 里摘掉 `config:enka.image` 字段、页里的引用和那段说明。

    **索引就是数组位置**，所以删完必须整体重排：先记下「旧索引 -> 新索引」
    的映射，再把页里的 `index` 按映射改掉。直接删了不管会让后面的字段
    全部错位（UI 会指到别的字段上去）。
    """
    d = json.loads(raw)
    fields = d.get("fields") or []
    old_idx = [(f.get("index"), f) for f in fields
               if isinstance(f, dict)]
    kept = [(i, f) for i, f in old_idx if f.get("key") != DROP_FIELD]
    if len(kept) == len(old_idx):
        return raw, 0
    remap = {old: new for new, (old, _f) in enumerate(kept)}
    d["fields"] = [f for _i, f in kept]
    for f in d["fields"]:
        f["index"] = remap.get(f["index"], f["index"])

    def fix(items):
        out = []
        for it in items or []:
            if not isinstance(it, dict):
                out.append(it)
                continue
            if it.get("t") == "field":
                if it.get("index") not in remap:
                    continue                      # 就是被摘掉的那个
                it = dict(it, index=remap[it["index"]])
            elif it.get("t") == "group":
                it = dict(it, items=fix(it.get("items")))
            elif it.get("t") == "note" and "面板图" in str(it.get("text", "")):
                continue                          # 面板说明也摘掉
            out.append(it)
        return out

    for p in d.get("pages") or []:
        if isinstance(p, dict):
            p["items"] = fix(p.get("items"))
    return json.dumps(d, indent=1, ensure_ascii=False), 1


def drop_image_key(text: str) -> str:
    """从示例配置里删掉「面板图」那三行（2 行注释 + image: true）。"""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    for line in lines:
        if "面板图" in line or line.strip() == "image: true":
            continue
        out.append(line)
    return "".join(out)


def main() -> int:
    print("=== 1) 敏感串闸门（硬门禁）")
    gate = subprocess.run([sys.executable,
                           str(ROOT / "tools" / "sanitize_for_publish.py"),
                           "--check"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=str(ROOT))
    print("  " + (gate.stdout or "").strip()[:300])
    if gate.returncode != 0:
        print("!! 发现敏感串，不打包")
        return 1

    print("=== 2) 收集文件（只收 git 跟踪的）")
    files = [x for x in sh(["git", "ls-files"]).splitlines() if x.strip()]
    print("   %d 个" % len(files))

    DIST.mkdir(exist_ok=True)
    zip_path = DIST / ("%s.zip" % TOP)
    staged = Path(tempfile.mkdtemp(prefix="release-"))
    try:
        root = staged / TOP
        for rel in files:
            src = ROOT / rel
            if not src.is_file():
                print("   跳过（工作区没有）:", rel)
                continue
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if rel == "ui/schema.json":
                text, n = drop_panel_field(src.read_text(encoding="utf-8"))
                print("   schema: 摘掉 %d 个字段并重排索引" % n)
                dst.write_text(text, encoding="utf-8", newline="")
            elif rel == "config.example.yaml":
                dst.write_text(
                    drop_image_key(src.read_text(encoding="utf-8")),
                    encoding="utf-8", newline="")
            else:
                shutil.copy2(src, dst)
        (root / "VERSION").write_text(VERSION + "\n", encoding="utf-8")
        (root / "快速开始.txt").write_text(QUICKSTART, encoding="utf-8")

        print("=== 3) 打包")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=9) as z:
            for f in sorted(root.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(staged).as_posix())
        sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        (DIST / ("%s.sha256" % TOP)).write_text(
            "%s  %s.zip\n" % (sha, TOP), encoding="utf-8")
        print("   %s  %.1f KB" % (zip_path.name, zip_path.stat().st_size / 1024))
        print("   sha256 %s" % sha)

        print("=== 4) 验包")
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
        bad = [n for n in names
               if any(k in n for k in ("config.yaml", "src/panel.py",
                                       "data/", "venv/", "__pycache__",
                                       ".git/", "gi_icons", "dmg.mjs"))]
        print("   文件数 %d | 不该有的：%s" % (len(names), bad or "无 ✓"))
        if bad:
            return 1
        for must in ("start.cmd", "config.example.yaml", "VERSION",
                     "快速开始.txt", "src/main.py", "requirements.txt",
                     "README.md", "stop.cmd"):
            if "%s/%s" % (TOP, must) not in names:
                print("   !! 缺文件:", must)
                return 1
        print("   必需的都在 ✓")

        # 解出来真检查一遍（schema 能不能解析、还有没有面板开关）
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(staged / "verify")
        v = staged / "verify" / TOP
        d = json.loads((v / "ui" / "schema.json").read_text(encoding="utf-8"))
        keys = [f.get("key") for f in d["fields"]]
        print("   解出来：schema %d 字段 | 还有面板开关吗：%s"
              % (len(keys), DROP_FIELD in keys))
        idx_ok = all(i == f.get("index")
                     for i, f in enumerate(d["fields"]))
        print("   索引连续：%s" % ("✓" if idx_ok else "✗ 错位了"))
        ex = (v / "config.example.yaml").read_text(encoding="utf-8")
        print("   示例配置里还有 image: 吗：%s" % ("image:" in ex))
        if DROP_FIELD in keys or not idx_ok or "image:" in ex:
            return 1
        # 语法检查（不跑，只编译）
        py = [p for p in v.rglob("*.py")]
        bad_py = []
        for p in py:
            try:
                compile(p.read_text(encoding="utf-8"), str(p), "exec")
            except SyntaxError as e:
                bad_py.append("%s: %s" % (p.name, e))
        print("   %d 个 .py 全部编译通过：%s" % (len(py), not bad_py))
        if bad_py:
            for b in bad_py[:5]:
                print("     ", b)
            return 1

        print()
        print("产物：%s" % zip_path)
        print("校验：%s" % (DIST / ("%s.sha256" % TOP)))
        return 0
    finally:
        shutil.rmtree(staged, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
