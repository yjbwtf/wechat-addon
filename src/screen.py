"""屏幕截图，用于群内 `\\截图` 指令。

为什么要缩图
------------
现在很多机器是 2K/4K 双屏，全屏原始 PNG 能到十几 MB。微信发原图又慢又占流量，
群里也没人需要那个分辨率。默认缩到宽 1920，PNG 出来通常几百 KB。

隐私提醒
--------
截图会把**整块屏幕**拍下来，包括当时开着的任何窗口。如果指令是在群里发的、
图也发回那个群，那就是把屏幕内容给全群看了。
所以 `screenshot.target` 支持 `private`：把图私聊发给指定的人（比如你自己），
而不是发到群里。发到 19 人群之前请想清楚。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .config import ShotConfig


class ScreenError(RuntimeError):
    pass


def capture(cfg: ShotConfig, out_dir: Path) -> tuple[Path, tuple[int, int]]:
    """抓一张全屏（多显示器）截图，返回 (文件路径, 最终尺寸)。"""
    try:
        from PIL import ImageGrab
    except ImportError as exc:  # pragma: no cover
        raise ScreenError("没装 Pillow，无法截图") from exc

    try:
        img = ImageGrab.grab(all_screens=True)
    except Exception:  # noqa: BLE001
        # 某些环境（远程桌面、受限会话）不支持 all_screens
        try:
            img = ImageGrab.grab()
        except Exception as exc:  # noqa: BLE001
            raise ScreenError(f"截图失败：{exc}") from exc

    if cfg.max_width and img.width > cfg.max_width:
        ratio = cfg.max_width / float(img.width)
        img = img.resize((int(img.width * ratio), int(img.height * ratio)))

    fmt = (cfg.format or "PNG").upper()
    suffix = ".jpg" if fmt in {"JPG", "JPEG"} else ".png"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / time.strftime(f"shot_%Y%m%d_%H%M%S{suffix}")
    try:
        if suffix == ".jpg":
            img.convert("RGB").save(path, "JPEG", quality=max(50, min(95, cfg.quality)))
        else:
            img.save(path, "PNG")
    except OSError as exc:
        raise ScreenError(f"保存截图失败：{exc}") from exc

    logging.info("截图完成：%s（%dx%d）", path.name, img.width, img.height)
    return path, (img.width, img.height)


def cleanup(out_dir: Path, keep: int = 20) -> int:
    """只保留最近 N 张，免得截图越攒越多。"""
    try:
        files = sorted(out_dir.glob("shot_*"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return 0
    removed = 0
    for old in files[max(1, keep):]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass
    return removed
