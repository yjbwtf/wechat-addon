"""微信群助手：DSH 桥接服务。

分层：
    wechat_io  —— 微信接入层（wxauto4 / 控制台）
    policy     —— 安全策略层（白名单、@ 判定、限频、去重）
    history    —— 历史记录层（SQLite + TTL）
    brain      —— 大脑层（调用 dsh headless）
    main       —— 主循环与草稿确认
"""

__all__ = ["config", "history", "brain", "policy", "wechat_io"]
