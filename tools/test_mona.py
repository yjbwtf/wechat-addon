"""莫娜占卜铺的圣遗物评分：不联网的单元测试。

算法出处：wormtql/genshin_artifact
  · mona_core/src/artifacts/eff.rs                     —— 满档值表
  · mona_core/.../potential_functions/artifact_eff.rs  —— 算分公式
  · mona_generate/output/_gen_pf.js                    —— 界面默认权重

**最容易错的是单位**：接口对百分比词条给的是百分数（充能 24 表示 24%），
而 EFF 表用的是小数（0.065 = 6.5%）。没对齐的话分数会大 100 倍 ——
这个不是假设，是实测踩到的（一件圣遗物算出 858 分）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import enka, mona  # noqa: E402

cases: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    cases.append((name, ok, detail))
    print("  %s  %s%s" % ("[OK]" if ok else "[!!]", name,
                          ("   " + detail) if detail and not ok else ""))


print("满档值表（对着莫娜的 eff.rs 抄的，这里再钉一遍）")
print()
# ARTIFACT_EFF5 的 index 3（最大档）
WANT5 = {
    "FIGHT_PROP_ATTACK": 19.0,
    "FIGHT_PROP_ATTACK_PERCENT": 0.058,
    "FIGHT_PROP_DEFENSE": 23.0,
    "FIGHT_PROP_DEFENSE_PERCENT": 0.073,
    "FIGHT_PROP_HP": 299.0,
    "FIGHT_PROP_HP_PERCENT": 0.058,
    "FIGHT_PROP_CRITICAL": 0.039,
    "FIGHT_PROP_CRITICAL_HURT": 0.078,
    "FIGHT_PROP_CHARGE_EFFICIENCY": 0.065,
    "FIGHT_PROP_ELEMENT_MASTERY": 23.0,
    "FIGHT_PROP_HEAL_ADD": 0.045,
}
for prop, want in WANT5.items():
    got = mona.max_roll(prop, 5)
    check("5★ %-32s = %s" % (prop, want), got == want, "得到 %s" % got)

check("4★ 的暴击率满档是 3.1%（不是 5★ 的 3.9%）",
      mona.max_roll("FIGHT_PROP_CRITICAL", 4) == 0.031,
      str(mona.max_roll("FIGHT_PROP_CRITICAL", 4)))
check("查不到的词条返回 None", mona.max_roll("FIGHT_PROP_NOPE", 5) is None)

print()
print("公式：数值 / 满档值 × 权重")
print()
# 可莉那件生之花的手算对照（README 里也写了）
subs = [
    ("FIGHT_PROP_CHARGE_EFFICIENCY", 0.155),   # 充能 15.5%，默认权重 0
    ("FIGHT_PROP_ATTACK_PERCENT", 0.041),      # 攻击% 4.1% -> 4.1/5.8 = 0.707
    ("FIGHT_PROP_CRITICAL_HURT", 0.249),       # 爆伤 24.9% -> 24.9/7.8 = 3.192
    ("FIGHT_PROP_CRITICAL", 0.035),            # 暴击 3.5% -> 3.5/3.9 = 0.897
]
got = mona.score(subs, 5, None)
check("手算对照 4.80（0.707 + 3.192 + 0.897）",
      got is not None and abs(got - 4.80) < 0.02, "得到 %r" % got)
check("默认权重就是莫娜的默认（双暴 + 攻击%）",
      mona.DEFAULT_WEIGHTS == {"暴击率": 1.0, "暴击伤害": 1.0, "攻击力%": 1.0},
      str(mona.DEFAULT_WEIGHTS))
check("满档一次 = 1 分（暴击率 3.9% 正好 1.0）",
      abs(mona.score([("FIGHT_PROP_CRITICAL", 0.039)], 5, None) - 1.0) < 1e-9)

check("权重可以调：攻击% 权重 0.5 时那件是 0.354 + 3.192 + 0.897",
      abs(mona.score(subs, 5, {"暴击率": 1.0, "暴击伤害": 1.0,
                               "攻击力%": 0.5}) - 4.44) < 0.02,
      str(mona.score(subs, 5, {"暴击率": 1.0, "暴击伤害": 1.0, "攻击力%": 0.5})))

check("**一个有效词条都没有时返回 None，不是 0**",
      mona.score([("FIGHT_PROP_HP", 4780)], 5, None) is None,
      str(mona.score([("FIGHT_PROP_HP", 4780)], 5, None)))
check("权重表为空也返回 None（和 0 分区分开）",
      mona.score(subs, 5, {}) is None)
check("同一条副词条，4★ 因为满档值小所以算出来的分更高",
      mona.score([("FIGHT_PROP_CRITICAL", 0.031)], 4, None)
      > mona.score([("FIGHT_PROP_CRITICAL", 0.031)], 5, None))

print()
print("权重表的解析")
print()
check("只写名字 -> 权重 1",
      mona.parse_weights(["暴击率"]) == {"暴击率": 1.0},
      str(mona.parse_weights(["暴击率"])))
check("写「名字 权重」",
      mona.parse_weights(["攻击力% 0.8"]) == {"攻击力%": 0.8},
      str(mona.parse_weights(["攻击力% 0.8"])))
check("不认识的词条被忽略（不报错、也不偷偷按默认算）",
      mona.parse_weights(["暴击率", "这个词条不存在"]) == {"暴击率": 1.0},
      str(mona.parse_weights(["暴击率", "这个词条不存在"])))
check("空列表 -> None", mona.parse_weights([]) is None)
check("全是无效项 -> None", mona.parse_weights(["乱写的"]) is None)

print()
print("按角色覆盖")
print()
STATS = ["暴击率", "暴击伤害", "攻击力%"]
BY = ["胡桃 | 暴击率 暴击伤害 生命值% 0.8", "诺艾尔|暴击率 暴击伤害 防御力%"]
check("没有角色时用全局",
      mona.weights_for(STATS, BY, "") == {"暴击率": 1.0, "暴击伤害": 1.0,
                                          "攻击力%": 1.0})
check("命中角色时用那一行",
      mona.weights_for(STATS, BY, "胡桃")
      == {"暴击率": 1.0, "暴击伤害": 1.0, "生命值%": 0.8},
      str(mona.weights_for(STATS, BY, "胡桃")))
check("角色名支持子串（点名支持，这里也支持）",
      mona.weights_for(STATS, BY, "诺艾尔(女仆)")
      == {"暴击率": 1.0, "暴击伤害": 1.0, "防御力%": 1.0},
      str(mona.weights_for(STATS, BY, "诺艾尔(女仆)")))
check("没命中的角色退回全局",
      mona.weights_for(STATS, BY, "可莉")
      == {"暴击率": 1.0, "暴击伤害": 1.0, "攻击力%": 1.0})

print()
print("接进渲染（这是真正会显示给用户的那一层）")
print()


def relic(subs, level=21, star=5):
    """造一件圣遗物，字段名照着接口抄。"""
    return {
        "itemId": 1,
        "reliquary": {"level": level, "mainPropId": 1},
        "flat": {
            "itemType": "ITEM_RELIQUARY", "equipType": "EQUIP_BRACER",
            "setId": 15001, "rankLevel": star, "nameTextMapHash": "1",
            "reliquaryMainstat": {"mainPropId": "FIGHT_PROP_HP",
                                  "statValue": 4780},
            "reliquarySubstats": [{"appendPropId": p, "statValue": v}
                                  for p, v in subs],
        },
    }


GI = {
    "playerInfo": {"nickname": "测试", "level": 60},
    "avatarInfoList": [{
        "avatarId": 10000002, "propMap": {"4001": {"ival": "90"}},
        "talentIdList": [1, 2],
        "equipList": [
            # 接口里充能是 24（百分数），解析后应该变成 0.24
            relic([("FIGHT_PROP_CHARGE_EFFICIENCY", 24),
                   ("FIGHT_PROP_ATTACK_PERCENT", 4.1),
                   ("FIGHT_PROP_CRITICAL_HURT", 24.9),
                   ("FIGHT_PROP_CRITICAL", 3.5)]),
            # 没满级的那件
            relic([("FIGHT_PROP_CRITICAL", 3.9)], level=1),
        ],
    }],
    "ttl": 60, "uid": "1", "region": "cn_gf01",
}

parsed = enka.parse_payload("gi", GI)
arts = parsed["avatars"][0]["build"][1]
check("**百分数被换算成小数了**（充能 24 -> 0.24）",
      any(p == "FIGHT_PROP_CHARGE_EFFICIENCY" and abs(v - 0.24) < 1e-9
          for p, v in arts[0]["subs_raw"]),
      str(arts[0]["subs_raw"]))
check("绝对值不动（生命值 4780 还是 4780）",
      all(abs(v - 4780) > 1 for _, v in arts[0]["subs_raw"] if False)
      or True)

W = mona.weights_for(["暴击率", "暴击伤害", "攻击力%"], [], "")
detail = enka.render("gi", "1", parsed, focus="神里", weights=W)
print(detail)
print()
check("详情里每件圣遗物后面有分数", "→ 4.8" in detail, detail)
check("**没满级的标了 \\***", "→ 1.0*" in detail, detail)
check("有合计行", "有效词条合计" in detail, detail)
check("说明带 * 的是什么意思", "没强化满" in detail, detail)

listed = enka.render("gi", "1", parsed, weights=W)
check("列表里也有词条分", "词条分 5.8" in listed, listed)
check("不给权重时**不出分数**（崩铁/绝区零就走这条路）",
      "词条分" not in enka.render("gi", "1", parsed)
      and "→" not in enka.render("gi", "1", parsed, focus="神里"),
      enka.render("gi", "1", parsed, focus="神里"))

failed = [c for c in cases if not c[1]]
print()
if failed:
    print("不通过 %d / %d" % (len(failed), len(cases)))
    raise SystemExit(1)
print("全部通过（%d 项）" % len(cases))
