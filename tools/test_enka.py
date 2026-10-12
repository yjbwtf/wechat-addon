"""Enka 账号查询：不联网的单元测试。

网络调用用打桩替掉，所以这个测试随时能跑、不消耗 Enka 的限流配额。
真的打接口的验证在 `--live` 分支（手动跑，见文件末尾）。

打桩要验的四件事（都是真写错过或容易写错的）：
  1. 自动识别游戏时**不能**被自己的「最小间隔」挡住
     （第一版把间隔检查写在循环里，第二个游戏必然被挡）
  2. 每个游戏各发一次请求，配额也各记一次
  3. 命中缓存时**不消耗**配额
  4. 404 才换下一个游戏；429 / 网络错误立刻回报，不瞎试
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import enka  # noqa: E402

cases: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    cases.append((name, ok, detail))
    print("  %s  %s%s" % ("[OK]" if ok else "[!!]", name,
                          ("   " + detail) if detail and not ok else ""))


class Cfg:
    min_interval_seconds = 0.0
    max_per_hour = 60
    timeout_seconds = 20


# ---------------------------------------------------------------- 参数解析
print("参数解析")
print()
for text, want_game, want_uid, want_focus in [
    ("618285856", None, "618285856", ""),
    ("原神 618285856", "gi", "618285856", ""),
    ("hsr 618285856", "hsr", "618285856", ""),
    ("崩铁 618285856", "hsr", "618285856", ""),
    ("绝区零 618285856", "zzz", "618285856", ""),
    ("ZZZ 618285856", "zzz", "618285856", ""),
    # 末尾的角色名（可以只有两个字，也可以不完整）
    ("原神 618285856 可莉", "gi", "618285856", "可莉"),
    ("618285856 可莉", None, "618285856", "可莉"),
    ("原神 618285856 神里", "gi", "618285856", "神里"),
]:
    g, u, fo, err = enka.parse_args(text)
    check("%-22r -> %s/%s/%s" % (text, want_game, want_uid, want_focus),
          g == want_game and u == want_uid and fo == want_focus and not err,
          "得到 %r/%r/%r/%r" % (g, u, fo, err))

for text in ("", "不是数字", "123", "12345678901", "原神"):
    g, u, fo, err = enka.parse_args(text)
    check("%-14r 会被拒绝并给出人话" % text, u is None and bool(err),
          "得到 %r/%r/%r/%r" % (g, u, fo, err))

# 只有游戏名没有数字时，提示里要能看出格式
g, u, fo, err = enka.parse_args("原神")
check("只写游戏名时提示格式", u is None and "UID" in err, repr(err))

# ---------------------------------------------------------------- 名字表
print()
print("角色名映射")
print()
check("原神 10000002 = 神里绫华", enka.avatar_name("gi", 10000002) == "神里绫华")
check("原神 10000002 传字符串也行", enka.avatar_name("gi", "10000002") == "神里绫华")
check("崩铁 1001 = 三月七", enka.avatar_name("hsr", 1001) == "三月七")
check("绝区零 1011 = 安比", enka.avatar_name("zzz", 1011) == "安比")
check("查不到的名字退回 ID:xxx", enka.avatar_name("gi", 999) == "ID:999")

# 装备相关那三张表（原神专用）
check("套装 15001 = 角斗士的终幕礼",
      enka.gi_set_name(15001) == "角斗士的终幕礼", enka.gi_set_name(15001))
check("词条 FIGHT_PROP_CRITICAL = 暴击率",
      enka.gi_prop("FIGHT_PROP_CRITICAL") == "暴击率", enka.gi_prop("FIGHT_PROP_CRITICAL"))
check("词条 FIGHT_PROP_CHARGE_EFFICIENCY = 元素充能效率",
      enka.gi_prop("FIGHT_PROP_CHARGE_EFFICIENCY") == "元素充能效率")
check("武器名 1896898739 = 星锋剑",
      enka.gi_text("1896898739") == "星锋剑", enka.gi_text("1896898739"))
check("查不到的套装/词条**不编**，回可辨认的原文",
      enka.gi_set_name(999999).startswith("套装")
      and enka.gi_prop("FIGHT_PROP_NOPE") == "FIGHT_PROP_NOPE")
check("百分比词条加 %，绝对值不加",
      enka._stat_value("FIGHT_PROP_CRITICAL", 3.5) == "3.5%"
      and enka._stat_value("FIGHT_PROP_HP", 4780) == "4780",
      "%s / %s" % (enka._stat_value("FIGHT_PROP_CRITICAL", 3.5),
                   enka._stat_value("FIGHT_PROP_HP", 4780)))
check("套装摘要按件数排，同数按名字排；**单件不写数量**（输出要稳定）",
      enka._set_summary([{"set": "B"}, {"set": "A"}, {"set": "B"},
                         {"set": "C"}, {"set": "B"}]) == "B3 A C",
      enka._set_summary([{"set": "B"}, {"set": "A"}, {"set": "B"},
                         {"set": "C"}, {"set": "B"}]))

# ---------------------------------------------------------------- 格式化
print()
print("格式化")
print()


def relic(set_id, slot, lvl, main, main_val, subs):
    """造一件圣遗物。字段名和接口返回的一模一样（照着真实响应抄的）。"""
    return {
        "itemId": 1,
        "reliquary": {"level": lvl, "mainPropId": 1},
        "flat": {
            "itemType": "ITEM_RELIQUARY", "equipType": slot, "setId": set_id,
            "rankLevel": 5, "nameTextMapHash": "1",
            "reliquaryMainstat": {"mainPropId": main, "statValue": main_val},
            "reliquarySubstats": [{"appendPropId": p, "statValue": v}
                                  for p, v in subs],
        },
    }


def weapon(name_hash, lvl, refine, rank=5):
    return {
        "itemId": 2,
        "weapon": {"level": lvl, "affixMap": {"x": refine - 1}},
        "flat": {"itemType": "ITEM_WEAPON", "rankLevel": rank,
                 "nameTextMapHash": name_hash},
    }


GI = {
    "playerInfo": {
        "nickname": "旅行者", "level": 60, "worldLevel": 9,
        "finishAchievementNum": 900, "towerFloorIndex": 12, "towerLevelIndex": 3,
    },
    "avatarInfoList": [
        {"avatarId": 10000002, "propMap": {"4001": {"ival": "90"}},
         "talentIdList": [1, 2, 3, 4, 5, 6],
         "equipList": [
             weapon("1896898739", 80, 3),                     # 星锋剑 精3
             relic(15001, "EQUIP_BRACER", 21, "FIGHT_PROP_HP", 4780,
                   [("FIGHT_PROP_CRITICAL", 3.5),
                    ("FIGHT_PROP_CRITICAL_HURT", 6.2)]),
             relic(15001, "EQUIP_NECKLACE", 21, "FIGHT_PROP_ATTACK", 311, []),
             relic(15001, "EQUIP_SHOES", 21, "FIGHT_PROP_ATTACK_PERCENT", 46.6, []),
             relic(15001, "EQUIP_RING", 21, "FIGHT_PROP_FIRE_ADD_HURT", 46.6, []),
             relic(10001, "EQUIP_DRESS", 17, "FIGHT_PROP_CRITICAL_HURT", 62.2, []),
         ]},
        {"avatarId": 10000003, "propMap": {"4001": {"ival": "80"}}},
    ],
    "ttl": 300, "uid": "123456789", "region": "cn_gf01",
}
out = enka.format_reply("gi", "123456789", GI)
print(out)
print()
check("第一行有游戏、昵称、UID、区服",
      "原神" in out and "旅行者" in out and "123456789" in out and "官服" in out, out)
check("第二行有等级/世界等级/成就/深境",
      all(x in out for x in ("等级 60", "世界等级 9", "成就 900", "深境 12-3")), out)
check("角色带等级和命座", "神里绫华 90 C6" in out and "琴 80" in out, out)
check("**武器带等级和精炼**", "星锋剑 80 精3" in out, out)
check("**圣遗物按「套装+件数」压成一行**",
      "角斗士的终幕礼4 行者之心" in out, out)
check("没装备的角色不会多出空的分隔符", "琴 80\n" in out or "琴 80" in out, out)

# 点名看一个角色的完整面板
detail = enka.format_reply("gi", "123456789", GI, focus="神里")
print(detail)
print()
check("点名时输出那个角色的面板（名字可以只写两个字）",
      "神里绫华 Lv.90" in detail and "C6" in detail, detail)
check("详情里有武器", "四风原典" not in detail and "星锋剑 Lv.80 5★ 精3" in detail, detail)
check("详情里五件圣遗物都在",
      all(s in detail for s in ("生之花", "死之羽", "时之沙", "空之杯", "理之冠")), detail)
check("**每件都有部位/等级/套装/主词条**",
      "生之花 +20 角斗士的终幕礼 · 生命4780" in detail, detail)
check("**副词条在，且百分比带 %**",
      "暴击3.5%" in detail and "爆伤6.2%" in detail, detail)
check("部位按花羽沙杯冠排（不是接口给的顺序）",
      detail.index("生之花") < detail.index("死之羽") < detail.index("时之沙")
      < detail.index("空之杯") < detail.index("理之冠"), detail)

miss = enka.format_reply("gi", "123456789", GI, focus="钟离")
check("点名的角色不在展柜里 -> 说清楚并列出有哪些",
      "没找到「钟离」" in miss and "神里绫华" in miss, miss)

# 展柜关掉的情况（avatarInfoList 和 showAvatarInfoList 都没有）
GI_EMPTY = {"playerInfo": {"nickname": "无展柜", "level": 30}, "ttl": 60,
            "uid": "1", "region": "cn_gf01"}
out2 = enka.format_reply("gi", "1", GI_EMPTY)
check("没展柜时明确说清楚，而不是空白",
      "没看到角色展柜" in out2, out2)

# 「只有概要」的情况 —— 实测会遇到：avatarInfoList 空、showAvatarInfoList 有 12 个
# （137602053 那个号就是这样：玩家没开放角色详细数据）
GI_SUMMARY = {
    "playerInfo": {"nickname": "概要哥", "level": 60, "worldLevel": 9,
                   "showAvatarInfoList": [
                       {"avatarId": 10000002, "level": 90},
                       {"avatarId": 10000003, "level": 80},
                       {"avatarId": 10000005, "level": 70},
                   ]},
    "ttl": 60, "uid": "2", "region": "cn_gf01",
}
out3 = enka.format_reply("gi", "2", GI_SUMMARY)
print(out3)
print()
check("只有概要时用 showAvatarInfoList 列出角色",
      "神里绫华 90" in out3 and "琴 80" in out3 and "旅行者 70" in out3, out3)
check("**说明白为什么没有命座**（否则看着像程序坏了）",
      "只有概要" in out3, out3)
check("有详细数据时**不该**出现那句提示", "只有概要" not in out, out)

# 12 个以上时截断并报总数
GI_MANY = {
    "playerInfo": {"nickname": "一堆人", "level": 60,
                   "showAvatarInfoList": [
                       {"avatarId": 10000002 + i * 1000, "level": 90}
                       for i in range(15)]},
    "ttl": 60, "uid": "3", "region": "cn_gf01",
}
out4 = enka.format_reply("gi", "3", GI_MANY)
check("超过 12 个时列 12 个并说还有几个",
      out4.count("90") >= 12 and "还有 3 个" in out4, out4[:300])

# 绝区零：字段是 PascalCase，和原神完全两套（按官方文档的结构造）
ZZZ = {
    "uid": "1300000001", "ttl": 60,
    "PlayerInfo": {
        "SocialDetail": {"ProfileDetail": {"Nickname": "绳匠", "Level": 50}},
        "ShowcaseDetail": {"AvatarList": [
            {"Id": 1011, "Level": 60, "TalentLevel": 6},
            {"Id": 1021, "Level": 50},
        ]},
    },
}
out3 = enka.format_reply("zzz", "1300000001", ZZZ)
print(out3)
print()
check("绝区零取到了昵称/等级/角色",
      "绝区零" in out3 and "绳匠" in out3 and "绳网等级 50" in out3
      and "安比 60 命6" in out3, out3)

# ---------------------------------------------------------------- 缓存与限流
print()
print("缓存与限流")
print()
tmp = Path(tempfile.mkdtemp(prefix="enka-"))
c = enka.Cache(tmp / "c.json")
P1 = {"nick": "甲", "line2": [], "region": "官服", "avatars": [], "summary_only": False}
P2 = {"nick": "乙", "line2": [], "region": "官服", "avatars": [], "summary_only": False}
check("没查过时缓存是空的", c.get_payload("gi:1") is None)
c.put_payload("gi:1", P1, ttl=100)
check("写下之后能取到，而且是**结构化**的（不是渲染好的文本）",
      c.get_payload("gi:1") == P1, str(c.get_payload("gi:1")))
c.put_payload("gi:2", P2, ttl=0)          # ttl=0 会被兜到一个最小值
check("ttl=0 也不会立刻过期（兜到 60 秒）", c.get_payload("gi:2") == P2)
c.entries["gi:3"] = {"expire": time.time() - 1, "payload": P1}
check("过期的取不到", c.get_payload("gi:3") is None)
c.entries["gi:4"] = {"expire": time.time() + 99, "reply": "旧格式"}
check("旧格式（存的是文本不是 payload）当作没有，不会炸",
      c.get_payload("gi:4") is None)

c2 = enka.Cache(tmp / "c2.json")
check("另一个文件是空的（确认不是全局单例）", c2.get_payload("gi:1") is None)
c3 = enka.Cache(tmp / "c.json")
check("重新打开同一个文件能读到", c3.get_payload("gi:1") == P1)

c3.recent = []
c3.note_call()
why = c3.allow(min_interval=99, per_hour=60)
check("小于最小间隔时挡住", "这么快" in why, repr(why))
c3.recent = []
check("间隔够了就放行", c3.allow(min_interval=0.001, per_hour=60) == "")
c3.recent = [time.time()] * 5
why2 = c3.allow(min_interval=0.001, per_hour=5)
check("到每小时上限就挡住", "太多了" in why2, repr(why2))
c3.recent = [time.time() - 3700] * 5
check("一小时之前的记录不算数（会被清掉）",
      c3.allow(min_interval=0.001, per_hour=5) == "")

# ---------------------------------------------------------------- query 打桩
print()
print("query（打桩，不打真接口）")
print()

calls: list[tuple[str, str]] = []


def fake_fetch_ok(game, uid, timeout=20):
    """不记账的桩 —— 给需要委托的桩用，避免一次调用被记两次。"""
    return {"playerInfo": {"nickname": "N%s" % game, "level": 1},
            "ttl": 300, "uid": uid, "region": "cn_gf01"}


def fetch_ok_rec(game, uid, timeout=20):
    """记账 + 成功。给不需要委托的用例用。"""
    calls.append((game, uid))
    return fake_fetch_ok(game, uid)


def fake_fetch_404(game, uid, timeout=20):
    calls.append((game, uid))
    raise enka.EnkaError("查不到这个玩家。可能是：UID 写错了 / 服务器不对 / "
                         "没开过角色展柜。")


def fake_fetch_429(game, uid, timeout=20):
    calls.append((game, uid))
    raise enka.EnkaError("被限流了（查太频）。等一会儿再试。")


def fetch_gi_404(game, uid, timeout=20):
    """gi 假装 404，其它游戏成功。调用记录只在这里记一次 ——
    第一版让 fake_fetch_ok 也记，委托时就成了两次，测试自己把自己骗了。"""
    calls.append((game, uid))
    if game == "gi":
        raise enka.EnkaError("查不到这个玩家。可能是：UID 写错了。")
    return fake_fetch_ok(game, uid)


tmp2 = Path(tempfile.mkdtemp(prefix="enka-q-"))
cc = enka.Cache(tmp2 / "c.json")

enka.fetch, real_fetch = fetch_ok_rec, enka.fetch
try:
    calls.clear()
    ok, msg = enka.query(None, "111111111", Cfg(), cc)
    check("指定 GI 时只打一次", calls == [("gi", "111111111")], str(calls))
    check("缓存写进去了（结构化）",
          isinstance(cc.get_payload("gi:111111111"), dict),
          str(cc.get_payload("gi:111111111")))

    calls.clear()
    ok2, msg2 = enka.query(None, "111111111", Cfg(), cc)
    check("再查同一个 UID 走缓存、**不再发请求**", calls == [] and msg2 == msg,
          "calls=%s" % (calls,))

    # 自动识别：gi 404 -> 换 hsr 成功
    cc2 = enka.Cache(tmp2 / "c2.json")
    calls.clear()
    enka.fetch = fetch_gi_404
    ok3, msg3 = enka.query(None, "222222222", Cfg(), cc2)
    check("自动识别：gi 查不到就换 hsr，两次请求都记账",
          ok3 and calls == [("gi", "222222222"), ("hsr", "222222222")],
          "ok=%s calls=%s" % (ok3, calls))
    check("命中 hsr（不是 gi）", "星穹铁道" in msg3, msg3)

    # 自己的间隔限制**不能**挡自动识别
    calls.clear()
    cc3 = enka.Cache(tmp2 / "c3.json")
    cfg_slow = Cfg()
    cfg_slow.min_interval_seconds = 30.0
    enka.fetch = fetch_gi_404
    ok4, msg4 = enka.query(None, "333333333", cfg_slow, cc3)
    check("最小间隔 30 秒时，自动识别的内部换游戏仍然能走完",
          ok4 and len(calls) == 2, "ok=%s calls=%s msg=%r" % (ok4, calls, msg4))

    # 429 不该换来换去
    calls.clear()
    cc4 = enka.Cache(tmp2 / "c4.json")
    enka.fetch = fake_fetch_429
    ok5, msg5 = enka.query(None, "444444444", Cfg(), cc4)
    check("429 立刻回报，只打一次、不试别的游戏",
          (not ok5) and len(calls) == 1 and "限流" in msg5,
          "ok=%s calls=%s msg=%r" % (ok5, calls, msg5))

    # 全 404
    calls.clear()
    cc5 = enka.Cache(tmp2 / "c5.json")
    enka.fetch = fake_fetch_404
    ok6, msg6 = enka.query(None, "555555555", Cfg(), cc5)
    check("三个游戏都 404 时试满三次再回报",
          (not ok6) and len(calls) == 3 and "查不到" in msg6,
          "ok=%s calls=%s" % (ok6, calls))

    # ---- 点名看某个角色：**不该再打一次接口** -------------------------
    def fetch_with_char(game, uid, timeout=20):
        calls.append((game, uid))
        return {
            "playerInfo": {"nickname": "N", "level": 1,
                           "showAvatarInfoList": [{"avatarId": 10000002,
                                                   "level": 90}]},
            "avatarInfoList": [{
                "avatarId": 10000002,
                "propMap": {"4001": {"ival": "90"}},
                "talentIdList": [1, 2],
                "equipList": [
                    weapon("1896898739", 80, 3),
                    relic(15001, "EQUIP_BRACER", 21, "FIGHT_PROP_HP", 4780,
                          [("FIGHT_PROP_CRITICAL", 3.5)]),
                ]}],
            "ttl": 300, "uid": uid, "region": "cn_gf01"}

    calls.clear()
    cc6 = enka.Cache(tmp2 / "c6.json")
    enka.fetch = fetch_with_char
    ok7, msg7 = enka.query(None, "666666666", Cfg(), cc6)
    n_first = len(calls)
    ok8, msg8 = enka.query(None, "666666666", Cfg(), cc6, focus="神里")
    check("列表能出武器和套装",
          ok7 and "星锋剑 80 精3" in msg7 and "角斗士的终幕礼" in msg7, msg7)
    check("**点名看角色不再打接口**（缓存的是解析后的结构）",
          ok8 and len(calls) == n_first, "第一次 %d 次调用，点名后 %d 次"
          % (n_first, len(calls)))
    check("点名出的是详情（有圣遗物词条）",
          "神里绫华 Lv.90" in msg8 and "暴击3.5%" in msg8, msg8)

    # 换一个实例读同一个文件：验证缓存真的能过 JSON 往返
    cc7 = enka.Cache(tmp2 / "c6.json")
    ok9, msg9 = enka.query(None, "666666666", Cfg(), cc7, focus="神里")
    check("重启后（新实例读盘）点名同样不用再打接口",
          ok9 and len(calls) == n_first and "暴击3.5%" in msg9,
          "calls=%s" % (calls,))
finally:
    enka.fetch = real_fetch

# ------------------------------------------------- 群友公开指令的三道约束
print()
print("群友公开指令（安全相关的三道约束，少一道就是被人刷的入口）")
print()
import tempfile as _tf  # noqa: E402

from src.config import load_config  # noqa: E402
from src.policy import Gatekeeper  # noqa: E402
from src.wechat_io import IncomingMessage  # noqa: E402

_cfgdir = Path(_tf.mkdtemp(prefix="enka-cfg-"))
_cfgp = _cfgdir / "config.yaml"
_cfgp.write_text("""
dsh:
  command: dsh
whitelist_groups: [群A]
bot_names: [鱼]
require_at: true
commands:
  enabled: true
  prefix: '\\\\'
  secret: 'sec'
  allowed_senders: [主人]
enka:
  enabled: true
  allow_group_members: true
  prefix: '/'
  words: [查询, uid]
""", encoding="utf-8")
CFG = load_config(_cfgp)


class _StubStop:
    stopped = False


class _FakeBot:
    """只为测 handle_public_command 的三道约束 —— 不跑 Bot.__init__。

    它要看的东西很少：cfg、gate、paused、stop、以及最后有没有调到 handle_enka。
    """

    def __init__(self, *, paused: bool = False, stopped: bool = False) -> None:
        self.cfg = CFG
        self.gate = Gatekeeper(CFG)
        self.paused = paused
        self.stop = type("S", (), {"stopped": stopped})()
        self.called: list[tuple[str, bool]] = []

    def handle_enka(self, msg, body, emit=None, *, public=False):
        self.called.append((body, public))


import src.main as _main  # noqa: E402

_FakeBot.handle_public_command = _main.Bot.handle_public_command


def _msg(group: str, content: str, key: str) -> IncomingMessage:
    return IncomingMessage(group=group, sender="路人", content=content, msg_key=key)


def run(group: str, content: str, *, key: str = "k1", paused=False,
        stopped=False, fill_quota=False, bot=None) -> list:
    """跑一次 handle_public_command。

    **要测去重就必须传同一个 bot** —— 每次新建实例等于新建一个 Deduper，
    第二次当然不会被当成重复（第一版就在这里骗了自己一次）。
    """
    b = bot or _FakeBot(paused=paused, stopped=stopped)
    if fill_quota:
        from collections import deque
        b.gate.limiter._hits["群A"] = deque(
            [time.time()] * CFG.rate_limit.per_group_per_hour)
    # body 就用 content —— 第一版这里写死了「查询 …」，
    # 于是「不认识的 /xxx」那条测试其实一直在测查询。
    b.handle_public_command(_msg(group, content, key), content)
    return b.called


ENKA_BODY = "查询 原神 618285856"

check("白名单群 + 内容是查询 -> 交给 handle_enka（且标记 public）",
      run("群A", ENKA_BODY) == [(ENKA_BODY, True)], str(run("群A", ENKA_BODY)))
check("**不在白名单的群 -> 不处理**（控制指令不走白名单，公开指令必须走）",
      run("别群", ENKA_BODY) == [], str(run("别群", ENKA_BODY)))
check("暂停中 -> 不处理", run("群A", ENKA_BODY, paused=True) == [])
check("急停（data/STOP）-> 不处理", run("群A", ENKA_BODY, stopped=True) == [])

_dedup_bot = _FakeBot()
_first = len(run("群A", ENKA_BODY, key="same", bot=_dedup_bot))
_second = len(run("群A", ENKA_BODY, key="same", bot=_dedup_bot))
# 注意比的是**长度有没有涨**：called 是累积的，第二次被拦掉之后
# 里面仍然留着第一次那条，断言 == [] 是错的（第一版就这么错了一次）。
check("同一 msg_key 第二次 -> 去重拦掉（called 不再增长）",
      _first == 1 and _second == 1, "第一次 %d 条，第二次 %d 条" % (_first, _second))

check("不认识的 /xxx -> 静默忽略（不变成开放聊天入口）",
      run("群A", "不是查询", key="k2") == [])
check("每群每小时上限到了 -> 不处理",
      run("群A", ENKA_BODY, key="k9", fill_quota=True) == [])

# 严格匹配 vs 模糊匹配的区别（公开指令用严格的那个）
_g = Gatekeeper(CFG)
_words = {"enka": ["查询", "uid"]}
check("严格匹配认「查询 原神 123」",
      _g.strict_command_kind("查询 原神 123", _words) == "enka")
check("严格匹配认「查询一下 原神 123」（以指令词开头）",
      _g.strict_command_kind("查询一下 原神 123", _words) == "enka")
check("严格匹配**不认**「不是查询」（模糊匹配会认，那是给授权者打错字用的）",
      _g.strict_command_kind("不是查询", _words) is None
      and _g.classify_command("不是查询", _words) == "enka")

# --------------------------------------------------- 关掉开关之后
print()
print("关掉「允许群友查询」之后")
print()
_off = _cfgdir / "off.yaml"
_off.write_text(_cfgp.read_text(encoding="utf-8").replace(
    "allow_group_members: true", "allow_group_members: false"), encoding="utf-8")
CFG_OFF = load_config(_off)
g_off = Gatekeeper(CFG_OFF)
check("群友的 @机器人 /查询 不再被认成公开指令",
      g_off.public_command("群A", "@鱼 /查询 原神 618285856") is None)
check("但授权者的 \\uid 还在（只是不经过公开这条路）",
      g_off.as_command("\\\\uid 618285856", trusted=False) == "uid 618285856")

_enkoff = _cfgdir / "enkoff.yaml"
_enkoff.write_text(_cfgp.read_text(encoding="utf-8").replace(
    "enabled: true\n  allow_group_members", "enabled: false\n  allow_group_members"),
    encoding="utf-8")
g3 = Gatekeeper(load_config(_enkoff))
check("总开关关掉 -> 公开指令也不认",
      g3.public_command("群A", "@鱼 /查询 原神 618285856") is None)

# ------------------------------------------------------------ handle() 的路由
print()
print("handle() 的分流（钩子加错地方的话，/查询 会掉进模型）")
print()


class _RouterBot:
    """只测 handle() 把消息分给谁 —— 前两个分支之外的东西都不碰。"""

    def __init__(self) -> None:
        self.cfg = CFG
        self.gate = Gatekeeper(CFG)
        self.paused = False
        self.stop = type("S", (), {"stopped": False})()
        self.agg = type("A", (), {"enabled": False,
                                  "drop": lambda *a, **k: None})()
        # handle() 在指令分支里会调 adapter.resolve_identity()（受
        # commands.verify_identity 控制，默认开着）。给个桩，
        # 否则每次跑测试都往 stderr 吐一条「身份解析失败」的告警。
        self.adapter = type("Ad", (), {
            "resolve_identity": staticmethod(lambda *a, **k: {})})()
        self.hit: list[str] = []

    def handle_public_command(self, msg, body):
        self.hit.append("public:%s" % body)

    def handle_command(self, msg, body):
        self.hit.append("control:%s" % body)


_RouterBot.handle = _main.Bot.handle


def route(content: str) -> tuple[list[str], str]:
    """返回 (这条消息被哪条路处理了, 说明)。"""
    b = _RouterBot()
    try:
        b.handle(_msg("群A", content, "r%d" % abs(hash(content))))
    except AttributeError as exc:
        # 普通聊天那条路要用数据库 + 模型，本测试不搭那一整套。
        # 崩在 store 上本身就说明它**没走**指令那两条路 —— 这正是要验的。
        if "store" not in str(exc):
            raise
        return b.hit, "掉进普通聊天（要 store/模型，本测试不搭）"
    return b.hit, "指令路处理完了"


hits, how = route("@鱼 /查询 原神 618285856")
check("@鱼 /查询 原神 618285856 -> 走公开指令那条路",
      hits == ["public:查询 原神 618285856"], "%s / %s" % (hits, how))

hits, how = route("\\\\uid 618285856")
check("@鱼 \\uid 618285856 -> 走控制指令那条路（要授权+口令）",
      hits == ["control:uid 618285856"], "%s / %s" % (hits, how))

hits, how = route("/查询 原神 618285856")
# 这条其实更早就被 accept() 拒了（没 @，require_at 生效），
# 比掉进模型还好。只要确认它没走那两条指令路。
check("/查询 但**没 @** -> 两条路都不走",
      hits == [], "%s / %s" % (hits, how))

hits, how = route("@鱼 随便说点什么")
check("@鱼 随便说点什么 -> 两条路都不走（当普通聊天）",
      hits == [] and "普通聊天" in how, "%s / %s" % (hits, how))

failed = [c for c in cases if not c[1]]
print()
if failed:
    print("不通过 %d / %d" % (len(failed), len(cases)))
    raise SystemExit(1)
print("全部通过（%d 项）" % len(cases))
print()
print("真接口验证（会消耗 Enka 的限流配额，所以不默认跑）：")
print("    venv\\Scripts\\python.exe tools\\test_enka.py --live")
