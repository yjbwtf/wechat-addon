"""日志文件的定期清理。

不清理的话 bot.log 会一直涨（每次重启都追加），加上 wxauto4 的控制台输出、
截图目录、审计日志，`data/` 几个月就能到几百 MB。

策略很简单：**按时间**删。超过 keep_days 天的 `*.log` 直接删掉。
不做轮转（rotate）—— 这个项目不需要保留历史日志，只需要能查最近的问题。

为什么不做轮转：
  · .log 文件是给人看的，出问题时翻最近几天就够
  · 轮转要处理"当前文件被打开着"的复杂性，而 Windows 上删一个被占用的
    文件会失败 —— 所以这里**忽略删不掉的**，下次再试，不报错
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger("wechat-bot.logkeep")


def cleanup_logs(log_dir: Path, keep_days: float, *, now: float | None = None) -> tuple[int, int]:
    """删掉 log_dir 下超过 keep_days 天的 .log 文件。

    返回 (删了几个, 释放了多少字节)。
    """
    if keep_days <= 0:
        return 0, 0
    now = time.time() if now is None else now
    cutoff = now - keep_days * 86400.0
    removed = freed = 0
    try:
        files = list(log_dir.glob("*.log"))
    except OSError:
        return 0, 0
    for f in files:
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_mtime >= cutoff:
            continue
        try:
            f.unlink()
        except OSError as exc:
            # 正被占用（比如当前这个进程就在写 bot.log）—— 跳过就行，下次再试。
            # **不要**报错：这是正常情况，不是故障。
            log.debug("暂时删不掉 %s：%s", f.name, exc)
            continue
        removed += 1
        freed += st.st_size
    return removed, freed
