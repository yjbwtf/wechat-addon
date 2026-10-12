"""莫娜占卜铺的圣遗物评分：「有效词条」。

## 这不是我发明的算法，是照抄的

来源：**[wormtql/genshin_artifact](https://github.com/wormtql/genshin_artifact)**
（莫娜占卜铺，<https://www.mona-uranai.com/>），两个文件：

- `mona_core/src/artifacts/eff.rs` —— 那张「单次强化各档位」的表
- `mona_core/src/potential_function/potential_functions/artifact_eff.rs`
  —— 怎么用它算分
- `mona_generate/output/_gen_pf.js` —— 界面上各项权重的默认值

它自己的说明是：**「有效词条 · 以单次强化最大档位为 1 分」**。

## 公式

```
        Σ     副词条数值 / 该词条满档值 × 该词条权重
  仅统计权重非零的词条
```

拿莫娜的默认权重（暴击率 1、暴击伤害 1、攻击力% 1，其余 0）算，
就是大家常说的「双暴 + 攻击词条分」：

    暴击率 3.5% -> 3.5 / 3.9  = 0.90 分
    暴击伤害 24.9% -> 24.9 / 7.8 = 3.19 分
    攻击力% 4.1% -> 4.1 / 5.8  = 0.71 分
                                合计 4.80 分

## 两个必须说清楚的限制

1. **满档值分 4★ / 5★ 两套**，接口给了 `rankLevel`，所以这个能区分。
2. **没满级的圣遗物，这里给的是「当前分」，不是莫娜的「期望分」。**
   莫娜对低级圣遗物跑一遍 DFS，搜索强化满级所有可能的结果，给出评分**期望**。
   那套搜索这里没实现 —— 所以界面上未满级的会标一个 `*`，
   别把它当成莫娜那个数字。

   实测踩过：同一个号里，满级的圣遗物和 +0 的圣遗物混在一起，
   不加标记的话看不出来哪个分数是「还没强化完」。
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# 单次强化的四档数值。index 3 = **最大档**（算分用的就是它）
#
# 抄自 mona_core/src/artifacts/eff.rs 的 ARTIFACT_EFF5 / ARTIFACT_EFF4。
# 键用的是游戏接口里的 FIGHT_PROP_*，这样能从接口数据直接查。
# --------------------------------------------------------------------------
_EFF5 = {
    "FIGHT_PROP_ATTACK": [14.0, 16.0, 18.0, 19.0],
    "FIGHT_PROP_ATTACK_PERCENT": [0.041, 0.047, 0.053, 0.058],
    "FIGHT_PROP_DEFENSE": [16.0, 19.0, 21.0, 23.0],
    "FIGHT_PROP_DEFENSE_PERCENT": [0.051, 0.058, 0.066, 0.073],
    "FIGHT_PROP_HP": [209.0, 239.0, 269.0, 299.0],
    "FIGHT_PROP_HP_PERCENT": [0.041, 0.047, 0.053, 0.058],
    "FIGHT_PROP_CRITICAL": [0.027, 0.031, 0.035, 0.039],
    "FIGHT_PROP_CRITICAL_HURT": [0.054, 0.062, 0.07, 0.078],
    "FIGHT_PROP_CHARGE_EFFICIENCY": [0.045, 0.052, 0.058, 0.065],
    "FIGHT_PROP_ELEMENT_MASTERY": [16.0, 19.0, 21.0, 23.0],
    "FIGHT_PROP_HEAL_ADD": [0.031, 0.036, 0.04, 0.045],
}
_EFF4 = {
    "FIGHT_PROP_ATTACK": [11.0, 12.0, 14.0, 16.0],
    "FIGHT_PROP_ATTACK_PERCENT": [0.033, 0.037, 0.042, 0.047],
    "FIGHT_PROP_DEFENSE": [13.0, 15.0, 17.0, 19.0],
    "FIGHT_PROP_DEFENSE_PERCENT": [0.041, 0.047, 0.053, 0.058],
    "FIGHT_PROP_HP": [167.0, 191.0, 215.0, 239.0],
    "FIGHT_PROP_HP_PERCENT": [0.033, 0.037, 0.042, 0.047],
    "FIGHT_PROP_CRITICAL": [0.022, 0.025, 0.028, 0.031],
    "FIGHT_PROP_CRITICAL_HURT": [0.044, 0.05, 0.056, 0.062],
    "FIGHT_PROP_CHARGE_EFFICIENCY": [0.036, 0.041, 0.047, 0.052],
    "FIGHT_PROP_ELEMENT_MASTERY": [13.0, 15.0, 17.0, 19.0],
    "FIGHT_PROP_HEAL_ADD": [0.025, 0.029, 0.032, 0.036],
}

#: 算分用的是**最大档**（index 3）。莫娜的说明：「以单次强化最大档位为 1 分」
_MAX_ROLL_INDEX = 3

#: 莫娜界面的默认权重（_gen_pf.js 里的 default）。
#: 只有这三项默认为 1，其余全是 0 —— 也就是「双暴 + 攻击%」。
DEFAULT_WEIGHTS = {
    "暴击率": 1.0,
    "暴击伤害": 1.0,
    "攻击力%": 1.0,
}

#: 词条中文名 -> 接口的 FIGHT_PROP_*。用户按中文名配权重。
PROP_BY_NAME = {
    "攻击力": "FIGHT_PROP_ATTACK",
    "攻击力%": "FIGHT_PROP_ATTACK_PERCENT",
    "防御力": "FIGHT_PROP_DEFENSE",
    "防御力%": "FIGHT_PROP_DEFENSE_PERCENT",
    "生命值": "FIGHT_PROP_HP",
    "生命值%": "FIGHT_PROP_HP_PERCENT",
    "暴击率": "FIGHT_PROP_CRITICAL",
    "暴击伤害": "FIGHT_PROP_CRITICAL_HURT",
    "元素充能效率": "FIGHT_PROP_CHARGE_EFFICIENCY",
    "元素精通": "FIGHT_PROP_ELEMENT_MASTERY",
    "治疗加成": "FIGHT_PROP_HEAL_ADD",
}


def max_roll(prop: str, star: int = 5) -> float | None:
    """这个词条的「单次强化最大档」是多少。查不到返回 None。"""
    table = _EFF5 if int(star or 5) >= 5 else _EFF4
    row = table.get(prop)
    return row[_MAX_ROLL_INDEX] if row else None


def score(subs: list[tuple[str, float]], star: int = 5,
          weights: dict[str, float] | None = None) -> float | None:
    """一件圣遗物的「有效词条」分。

    `subs` = [(FIGHT_PROP_*, 数值), …]，就是接口里 `reliquarySubstats` 那些。
    `weights` = {词条中文名: 权重}，默认用莫娜的（双暴 + 攻击%）。

    返回 None 表示这件圣遗物的副词条一个都没参与算分 ——
    和「算出来是 0 分」不是一回事，调用方要分得开。
    """
    w = DEFAULT_WEIGHTS if weights is None else weights
    # 中文名 -> 权重，转成 prop -> 权重
    by_prop: dict[str, float] = {}
    for name, weight in (w or {}).items():
        prop = PROP_BY_NAME.get(str(name).strip())
        if prop and weight:
            by_prop[prop] = float(weight)
    if not by_prop:
        return None

    total = 0.0
    counted = 0
    for prop, value in subs:
        weight = by_prop.get(str(prop))
        if not weight:
            continue
        denom = max_roll(str(prop), star)
        if not denom:
            continue
        try:
            total += float(value) / denom * weight
        except (TypeError, ValueError):
            continue
        counted += 1
    return total if counted else None


def weights_for(stats: list[str], by_character: list[str],
                character: str = "") -> dict[str, float] | None:
    """决定这次算分用哪些词条。

    先按角色找覆盖（`角色名 | 词条1 词条2 权重3`），找不到再用全局那份。

    为什么要有按角色的覆盖：莫娜的默认是「双暴 + 攻击%」，
    而胡桃（生命）、诺艾尔（防御）、纳西妲（精通）这类角色用它算出来的分
    没有意义 —— 他们的有效词条根本不是那三项。
    """
    if character:
        key = str(character).strip()
        for row in by_character or []:
            name, _, rest = str(row).partition("|")
            name = name.strip()
            # 和点名一样用子串匹配：「可莉」能匹配到「可莉」，
            # 「莉」也能 —— 免得用户还要写全名
            if name and key and (name in key or key in name):
                w = parse_weights(rest.replace("，", " ").split())
                if w:
                    return w
    return parse_weights(stats)


def parse_weights(spec: list[str] | None) -> dict[str, float] | None:
    """把配置里的字符串列表解析成权重表。

    一个词条后面**紧跟着一个数字**，那个数字就是它的权重：

        ["暴击率", "暴击伤害", "攻击力% 0.8"]
        ["暴击率", "暴击伤害", "生命值%", "0.8"]     # 展开写也一样

    两种写法都认 —— 界面上的「rows」控件是每行一个字符串，而按角色覆盖
    那行是「角色 | 词条 词条 权重」展开成一串，两种都会走到这里。

    返回 None 表示没有有效项，调用方应该退回默认权重。
    """
    if not spec:
        return None
    tokens: list[str] = []
    for raw in spec:
        tokens.extend(str(raw).replace("，", " ").split())

    out: dict[str, float] = {}
    last: str | None = None
    for tok in tokens:
        if tok in PROP_BY_NAME:
            out[tok] = 1.0
            last = tok
            continue
        # 不是词条名 —— 如果是个数字，就当成**上一个词条**的权重
        try:
            num = float(tok)
        except ValueError:
            last = None          # 不认识的东西，断开连接
            continue
        if last:
            out[last] = num
    return out or None


def format_score(value: float | None) -> str:
    """分数怎么显示。没参与算分的返回空串（调用方自己决定显不显示）。"""
    return "" if value is None else "%.1f" % value
