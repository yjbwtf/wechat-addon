"""闲聊用哪个模型 —— 手动切换，不做任何自动判断。

## 为什么是「叠加层」而不是「改 profile」

`dsh` 支持 `--patch <file>`：在 profile 之上再叠一层覆盖。
所以切换模型**不需要动 `wechat/cordis.patch.yml`**，也不需要两套 profile：

    默认（不加 --patch）  -> profile 里配的 deepseek-account / deepseek-flash
    --patch dsh/model-zhipu.yml -> 覆盖成智谱的 GLM-4.7-Flash

回切就是「不加 --patch」，天然回到原样，不存在改坏原配置的可能。

## 为什么改完立刻生效、不用重启

`brain.py` **每收到一条消息都会新起一个 `dsh` 进程**，profile 是那时候
重新组合的。所以只要改了 `config.yaml` 里的 `dsh.model_preset`，下一条消息
就是新模型 —— 不用重启机器人。

（这也是为什么切换做成「手动档位」很便宜：不存在需要常驻的连接。）

## 关于凭据

`--patch` 那层里写的是 `apiKeyEnv: ZAI_API_KEY` —— 只是**变量名**，
不是密钥本身。真正的密钥由这里塞进子进程的环境变量，来源有两个：

    1. config.yaml 的 dsh.zhipu_api_key（config.yaml 是 gitignore 的）
    2. 环境变量 ZAI_API_KEY（config.yaml 里那个留空时用）

配置文件里永远不出现密钥，公开仓库里更不会。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 档位 -> 叠加层文件名。None 表示不加 --patch（用 profile 自己的配置）。
PRESETS: dict[str, str | None] = {
    "deepseek": None,                 # DSH 桌面账号，按量计费
    "zhipu": "model-zhipu.yml",       # 智谱 GLM-4.7-Flash，官方定价免费
}

#: 智谱凭据用的环境变量名。必须和叠加层里的 apiKeyEnv 一致。
ZHIPU_KEY_ENV = "ZAI_API_KEY"

DEFAULT_PRESET = "deepseek"


def normalize(preset: str | None) -> str:
    """把配置里的档位名收拾干净。不认识的一律当成默认档。"""
    name = (preset or "").strip().lower()
    if name not in PRESETS:
        if name:
            logging.warning("config.yaml 里的 dsh.model_preset=%r 不认识，按 %s 处理",
                            preset, DEFAULT_PRESET)
        return DEFAULT_PRESET
    return name


def preset_dir(cfg) -> Path:
    """叠加层目录。配置留空就用项目里的 dsh\\。"""
    raw = getattr(cfg, "preset_dir", "") or ""
    return Path(raw) if raw else ROOT / "dsh"


def patch_path(cfg, preset: str | None = None) -> Path | None:
    """档位对应的叠加层文件。默认档返回 None（不加 --patch）。

    返回的是**路径**，不保证文件存在 —— 调用方（brain.py）要检查。

    `preset` 显式指定时用它（超时换档要用兜底档），否则读 cfg。
    """
    name = normalize(preset or getattr(cfg, "model_preset", DEFAULT_PRESET))
    filename = PRESETS[name]
    if not filename:
        return None
    return preset_dir(cfg) / filename


def launch_args(cfg, preset: str | None = None) -> list[str]:
    """追加到 dsh 命令行后面的参数。

    文件不存在时**不报错、也不加参数** —— 宁可退回默认模型（能聊），
    也不要因为少了一个文件就让机器人彻底不回复。
    """
    path = patch_path(cfg, preset)
    if path is None:
        return []
    if not path.is_file():
        logging.warning("模型叠加层不存在：%s —— 这次先按默认模型走", path)
        return []
    return ["--patch", str(path)]


def env_overrides(cfg, preset: str | None = None) -> dict[str, str]:
    """要额外塞进子进程环境的变量。目前只有智谱的 Key。"""
    name = normalize(preset or getattr(cfg, "model_preset", DEFAULT_PRESET))
    if name != "zhipu":
        return {}
    key = (getattr(cfg, "zhipu_api_key", "") or "").strip()
    if not key:
        key = (os.environ.get(ZHIPU_KEY_ENV) or "").strip()
    if not key:
        logging.warning("档位是 zhipu 但没找到 API Key（config.yaml 的 "
                        "dsh.zhipu_api_key 或环境变量 %s），这次调用会失败",
                        ZHIPU_KEY_ENV)
        return {}
    return {ZHIPU_KEY_ENV: key}


def describe(cfg) -> str:
    """给人看的一句话，写进日志和「状态」里。"""
    name = normalize(getattr(cfg, "model_preset", DEFAULT_PRESET))
    if name == "zhipu":
        return "zhipu / glm-4.7-flash（免费）"
    return "deepseek / profile 自带（按量计费）"


def fallback_preset(cfg) -> str | None:
    """主模型失败时用来兜底的档位。已经在默认档就返回 None（没得退）。

    **这不是「自动挑模型」。** 档位仍然完全由用户手动决定；
    这里只在一次调用**彻底失败**之后退一步重试，理由是实测算出来的：

        2026-10-12 15:01  智谱返回 429 / code 1305「该模型当前访问量过大」
        DSH 内置重试跑满 5 次、17 秒后仍然失败
        然后 main.py 只记一行 ERROR —— 那条消息一个字都没回

    免费模型被挤爆是常态，而「沉默」不是可接受的失败方式。回退一次的花费
    是 DSH 账号那边的一条群聊消息（不到一分钱），换来的是不石沉大海。
    """
    name = normalize(getattr(cfg, "model_preset", DEFAULT_PRESET))
    if name == DEFAULT_PRESET:
        return None
    return DEFAULT_PRESET
