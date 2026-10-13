"""打包**私有迁移包**：把当前这台机器上的运行效果整体搬到服务器。

和公开的 1.0.0 不一样 —— 这个包里**带**：

  · `config.yaml`（口令 / 授权名单 / 白名单群）—— 公开包故意不带
  · `persona.yaml`、`data/memes_user.yaml`（人设与梗库叠加层）
  · 面板那整套（`src/panel.py`、`data/miao/`、`data/gi_icons/`）
  · 运行状态（聊天记录 sqlite、线程、审计、缓存、wuwa 凭据索引）

**不带**（服务器上重建，或者本来就不该带）：

  · `venv/`      —— 平台相关，start.cmd 会自动重建
  · `.git/`、`__pycache__/`、`dist/`
  · `data/logs/`、`data/shots/`、`data/ui_shots/`、`data/panels/`（截图和渲染产物）
  · `data/STOP`（带过去会让机器人起来就不回话）
  · `data/_*.py` 这些临时脚本

用法：
    venv\\Scripts\\python.exe tools\\build_private_bundle.py [版本号] [--no-icons]
    --no-icons  不打包 153MB 的图标库（服务器上再跑 tools/build_gi_icons.py 补）
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
NO_ICONS = "--no-icons" in sys.argv
VERSION = (ARGS[0] if ARGS else "1.0.0").strip()
TOP = "wechat-addon-%s-personal" % VERSION

#: data/ 下这些子目录不带（截图 / 渲染产物 / 日志）
SKIP_DIRS = {"logs", "shots", "ui_shots", "panels", "__pycache__"}
#: data/ 下不带的具体文件
SKIP_FILES = {"STOP"}

MIGRATE = """\
微信助手 {ver} —— 私有迁移包（含配置与运行状态）
=========================================================

## 这个包里有什么（和公开版不一样的地方）

  · config.yaml          你的口令 / 授权名单 / 白名单群
  · persona.yaml         你的人设
  · data/memes_user.yaml 梗库叠加层
  · src/panel.py         面板图渲染（本地私有模块，公开版没有）
  · data/miao/           miao-plugin 的评分/伤害引擎 + 模板 + CSS + node_modules
  · data/gi_icons/       原神图标库（天赋/命座/立绘，1142 个文件）
  · data/*.json|sqlite3  运行状态（聊天记录、线程、审计、缓存、凭据索引）

**没带**：venv/（平台相关，会自动重建）、.git/、日志、截图、渲染产物、STOP。

## 服务器上要装什么

  1. 微信 PC 版（能登录；这个机器人是靠操作微信窗口干活的）
  2. DSH（DeepSeek Harness）桌面版  —— 闲聊回复靠它
  3. Python 3.10+（start.cmd 会用它建 venv）
  4. **Node.js**（面板图要用它渲染 HTML -> PNG）—— 没装也能跑，
     只是点名查角色时会退回发文字，不会报错

## 怎么迁移

  1. 把整个文件夹解压到服务器上**任意路径**
     （别放 C:\\Program Files 这种要管理员权限的地方）
  2. 打开 config.yaml，确认这一行改成服务器上的实际路径：
         dsh:
           command: "C:\\\\...\\\\dsh.cmd"      # 用 `where dsh` 找
  3. 双击 start.cmd —— 会自动建 venv、装依赖，然后开始跑
  4. 想确认环境没问题：先跑 `start.cmd -Check`

## 注意

  · **这个包里有你的口令和授权名单，别外传。**
  · 同一时间只能有一个机器人在跑（两个实例会互相抢微信窗口）。
    服务器起来之后，旧机器上先跑 stop.cmd。
  · 聊天记录是 data/history.sqlite3，跟着包一起过去了，
    所以机器人“记得”之前的对话。
"""


def sh(args: list[str]) -> str:
    p = subprocess.run(args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=str(ROOT))
    if p.returncode != 0:
        raise SystemExit("命令失败：%s\n%s" % (" ".join(args), p.stderr[:300]))
    return p.stdout


def collect() -> list[tuple[Path, str]]:
    """[(源文件, 包内相对路径)]"""
    out: list[tuple[Path, str]] = []

    # 1) git 跟踪的文件（公开项目那套）
    for rel in sh(["git", "ls-files"]).splitlines():
        if not rel.strip():
            continue
        src = ROOT / rel
        if src.is_file():
            out.append((src, rel))

    # 2) 私有文件：配置 + 本地模块 + 脚本
    for rel in ("config.yaml", "persona.yaml", "persona.yaml.local",
                "src/panel.py", "tools/build_gi_icons.py",
                "tools/build_release.py", "tools/build_private_bundle.py"):
        src = ROOT / rel
        if src.is_file():
            out.append((src, rel))

    # 3) 运行状态：data/ 顶层的文件（跳过 STOP / 临时脚本 / 日志）
    d = ROOT / "data"
    if d.is_dir():
        for f in sorted(d.iterdir()):
            if not f.is_file():
                continue
            if f.name in SKIP_FILES or f.name.startswith("_"):
                continue
            if f.suffix.lower() in (".log",):
                continue
            out.append((f, "data/" + f.name))

    # 4) 面板要的目录
    for sub in ("miao",) + (() if NO_ICONS else ("gi_icons",)):
        base = d / sub
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*")):
            if not f.is_file():
                continue
            if any(part in SKIP_DIRS for part in f.parts):
                continue
            out.append((f, str(f.relative_to(ROOT)).replace("\\", "/")))
    return out


def main() -> int:
    print("=== 收集文件")
    files = collect()
    total = sum(s.stat().st_size for s, _ in files)
    print("   %d 个文件，原始 %.1f MB%s"
          % (len(files), total / 1024 / 1024,
             "（--no-icons，不含图标库）" if NO_ICONS else ""))

    must = ["config.yaml", "src/panel.py", "data/miao/score.mjs",
            "data/miao/dmg.mjs", "data/miao/render.mjs"]
    if not NO_ICONS:
        must.append("data/gi_icons")
    rels = {r for _s, r in files}
    for m in must:
        hit = (m in rels) or any(r.startswith(m + "/") for r in rels)
        if not hit:
            print("   !! 少了关键内容：%s" % m)
            return 1
    print("   关键内容齐 ✓（config.yaml / panel.py / miao 引擎%s）"
          % ("/ 图标库" if not NO_ICONS else ""))

    DIST.mkdir(exist_ok=True)
    zip_path = DIST / ("%s.zip" % TOP)
    staged = Path(tempfile.mkdtemp(prefix="private-"))
    try:
        root = staged / TOP
        for src, rel in files:
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        # 迁移说明放最外层，解压就看得到
        (root / "迁移说明.txt").write_text(MIGRATE.format(ver=VERSION),
                                          encoding="utf-8")
        (root / "VERSION").write_text(VERSION + "\n", encoding="utf-8")

        print("=== 打包")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=6) as z:
            for f in sorted(root.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(staged).as_posix())
        size = zip_path.stat().st_size
        sha = hashlib.sha256()
        with zip_path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                sha.update(chunk)
        (DIST / ("%s.sha256" % TOP)).write_text(
            "%s  %s.zip\n" % (sha.hexdigest(), TOP), encoding="utf-8")
        print("   %s  %.1f MB（压缩前 %.1f MB）"
              % (zip_path.name, size / 1024 / 1024, total / 1024 / 1024))
        print("   sha256 %s" % sha.hexdigest())

        print("=== 验包")
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
        print("   条目 %d" % len(names))
        bad = [n for n in names
               if any(k in n for k in ("/venv/", "/.git/", "__pycache__",
                                       "data/logs/", "data/shots/",
                                       "data/panels/", "data/STOP"))
               # **顶层**的 dist/ 才算（node_modules 里也有叫 dist 的目录，
               # 一刀切 "/dist/" 会把它们全误判成"不该带的"）
               or n.startswith("%s/dist/" % TOP)]
        if bad:
            print("   !! 不该带的：%s" % bad[:6])
            return 1
        print("   没有 venv/.git/日志/截图/STOP ✓")
        for must2 in ("config.yaml", "persona.yaml", "src/panel.py",
                      "data/miao/score.mjs", "data/miao/node_modules",
                      "data/memes_user.yaml", "迁移说明.txt",
                      "start.cmd", "requirements.txt"):
            if "%s/%s" % (TOP, must2) not in names and not any(
                    n.startswith("%s/%s/" % (TOP, must2)) for n in names):
                print("   !! 缺：%s" % must2)
                return 1
        print("   该带的都在 ✓")
        print()
        print("产物：%s" % zip_path)
        print()
        print("⚠ 这个包里有你的口令 / 授权名单 / 群名 / 聊天记录 —— 别外传。")
        return 0
    finally:
        shutil.rmtree(staged, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
