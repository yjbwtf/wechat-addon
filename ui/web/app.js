/* ==========================================================================
   设置界面前端
   --------------------------------------------------------------------------
   页面完全由 /api/state 返回的 schema 生成 —— 字段表来自 ui/schema.json
   （由旧 ui.c 提取，已核对每页字段数一致）。所以这里没有硬编码任何一个配置项：
   加一个配置项只需要改 schema，前端一行都不用动。

   状态管理很简单：original 是服务端给的值，values 是当前界面上的值。
   两者不同的 key 就是「改动」，保存时只提交这些。
   ========================================================================== */

const $ = (id) => document.getElementById(id);

let SCHEMA = null;
let ORIGINAL = {};      // 服务端值
let VALUES = {};        // 界面值
let PAGE = 0;
let THEME = localStorage.getItem("theme") || "";

/* ---------------------------------------------------------------- 小工具 */
function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity .25s ease, transform .25s ease";
    el.style.opacity = "0";
    el.style.transform = "translateY(6px)";
    setTimeout(() => el.remove(), 260);
  }, kind === "err" ? 7000 : 3200);
}

function confirmBox(title, body, onYes) {
  $("modalTitle").textContent = title;
  $("modalBody").innerHTML = "";
  if (typeof body === "string") {
    const p = document.createElement("div");
    p.textContent = body;
    $("modalBody").appendChild(p);
  } else {
    $("modalBody").appendChild(body);
  }
  $("modal").hidden = false;
  $("modalYes").onclick = () => { $("modal").hidden = true; onYes(); };
  $("modalNo").onclick = () => { $("modal").hidden = true; };
}

async function api(path, body) {
  const opt = body
    ? { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) }
    : {};
  const r = await fetch(path, opt);
  if (!r.ok) throw new Error(`${path} -> HTTP ${r.status}`);
  return r.json();
}

/* ---------------------------------------------------------------- 主题 */
function applyTheme() {
  if (THEME) document.documentElement.dataset.theme = THEME;
  else delete document.documentElement.dataset.theme;
}

/* ---------------------------------------------------------------- 脏值 */
function changedKeys() {
  const out = {};
  for (const k of Object.keys(VALUES)) {
    if ((ORIGINAL[k] ?? "") !== (VALUES[k] ?? "")) out[k] = VALUES[k];
  }
  return out;
}
function refreshDirty() {
  const n = Object.keys(changedKeys()).length;
  $("dirtyBar").hidden = n === 0;
  $("btnSave").disabled = n === 0;
  $("btnRevert").disabled = n === 0;
}

/* ---------------------------------------------------------------- 控件 */
function makeSwitch(f) {
  const b = document.createElement("button");
  b.className = "switch";
  b.type = "button";
  b.setAttribute("role", "switch");
  const onVal = f.on ?? "1", offVal = f.off ?? "0";
  const sync = () => b.setAttribute("aria-checked",
    String(VALUES[f.key]) === String(onVal) ? "true" : "false");
  b.onclick = () => {
    const nowOn = b.getAttribute("aria-checked") === "true";
    VALUES[f.key] = nowOn ? offVal : onVal;
    sync();
    refreshDirty();
  };
  b._sync = sync;
  sync();
  return b;
}

function makeSeg(f) {
  const wrap = document.createElement("div");
  wrap.className = "seg";
  const opts = f.options || (f.on && f.off ? [f.on, f.off] : []);
  const vals = (f.kind === "choice") ? opts : [f.on ?? "1", f.off ?? "0"];
  const labels = (f.kind === "choice") ? opts
    : opts.map((o) => (o === (f.on ?? "1") ? "开" : "关"));

  // 滑块：一个绝对定位的色块，靠 --i / --n 两个变量平移。
  // 位置算在 CSS 里，JS 只改变量，所以过渡是浏览器做的，不占主线程。
  const thumb = document.createElement("div");
  thumb.className = "thumb";
  wrap.appendChild(thumb);
  wrap.style.setProperty("--n", vals.length);

  const btns = [];
  vals.forEach((v, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = labels[i];
    b.onclick = () => { VALUES[f.key] = v; syncSeg(); refreshDirty(); };
    wrap.appendChild(b);
    btns.push({ b, v });
  });

  function syncSeg() {
    let sel = 0;
    btns.forEach(({ b, v }, i) => {
      const on = String(VALUES[f.key]) === String(v);
      b.classList.toggle("on", on);
      if (on) sel = i;
    });
    wrap.style.setProperty("--i", sel);
  }
  wrap._sync = syncSeg;
  syncSeg();
  return wrap;
}

function makeInput(f) {
  const num = f.kind === "int" || f.kind === "float";
  const el = document.createElement("input");
  el.type = num ? "number" : "text";
  if (f.kind === "float") el.step = "any";
  el.value = VALUES[f.key] ?? "";
  el.oninput = () => { VALUES[f.key] = el.value; refreshDirty(); };
  el._sync = () => { el.value = VALUES[f.key] ?? ""; };
  return el;
}

function makeArea(f) {
  const el = document.createElement("textarea");
  el.className = f.kind === "text" ? "ta-text" : "ta-list";
  el.spellcheck = false;
  el.value = VALUES[f.key] ?? "";
  el.oninput = () => { VALUES[f.key] = el.value; refreshDirty(); };
  el._sync = () => { el.value = VALUES[f.key] ?? ""; };
  return el;
}

function makeField(f) {
  const card = document.createElement("div");
  card.className = "card";
  const wide = (f.kind === "list" || f.kind === "text");
  if (wide) card.classList.add("wide");
  if (f.key.includes("agent.enabled")) card.classList.add("danger");

  const text = document.createElement("div");
  text.className = "card-text";
  const lab = document.createElement("div");
  lab.className = "card-label";
  lab.textContent = f.label;
  const hint = document.createElement("div");
  hint.className = "card-hint";
  hint.textContent = f.hint || "";
  text.append(lab, hint);

  const ctl = document.createElement("div");
  ctl.className = "card-ctl";
  const isSwitch = (f.kind === "bool") && (f.on || f.off) &&
                   !(f.on && f.off && f.on.length > 2);
  // on/off 是 "1"/"0" 时用开关；是 auto/draft 这种有语义的字符串时用分段
  const useSwitch = f.kind === "bool" && (f.on === "1" || f.on == null);
  let widget;
  if (f.kind === "bool") widget = useSwitch ? makeSwitch(f) : makeSeg(f);
  else if (f.kind === "choice") widget = makeSeg(f);
  else if (f.kind === "list" || f.kind === "text") widget = makeArea(f);
  else widget = makeInput(f);

  ctl.appendChild(widget);
  if (wide) { card.append(text, ctl); } else { card.append(text, ctl); }
  card._widget = widget;
  card._field = f;
  return card;
}

/* ---------------------------------------------------------------- 渲染 */
function renderNav() {
  const nav = $("nav");
  nav.innerHTML = "";
  SCHEMA.pages.forEach((p, i) => {
    const b = document.createElement("button");
    b.className = "nav-item" + (i === PAGE ? " active" : "");
    b.textContent = p.title;
    b.onclick = () => { PAGE = i; render(); };
    nav.appendChild(b);
  });
}

function renderFieldBlock(items, host) {
  let pending = null;
  const flush = () => {
    if (!pending) return;
    host.appendChild(pending);
    pending = null;
  };
  for (const it of items) {
    if (it.t === "field") {
      const f = SCHEMA.fields[it.index];
      if (!f) continue;
      const card = makeField(f);
      if (pending) {
        // 相邻字段可以并排？不 —— 每张卡一行，说明才看得全
        flush();
      }
      pending = card;
      flush();
    } else {
      flush();
      if (it.t === "head") {
        const h = document.createElement("div");
        h.className = "sec-head";
        h.textContent = it.text;
        host.appendChild(h);
      } else if (it.t === "note") {
        const n = document.createElement("div");
        n.className = "sec-note";
        n.textContent = it.text;
        host.appendChild(n);
      } else if (it.t === "button") {
        const a = SCHEMA.actions[it.index];
        if (!a) continue;
        const row = host.querySelector(".btn-row") || (() => {
          const r = document.createElement("div");
          r.className = "btn-row";
          host.appendChild(r);
          return r;
        })();
        const b = document.createElement("button");
        b.className = "btn" + (a.primary ? " primary" : "") +
                      (a.danger ? " danger" : "");
        b.textContent = a.label;
        b.onclick = () => runAction(a.label);
        b.dataset.action = a.label;
        row.appendChild(b);
      }
    }
  }
  flush();
}

function runAction(label) {
  if (label === "启动机器人") return botAction("start");
  if (label === "停止机器人") return botAction("stop");
  if (label === "刷新状态") return reload(true);
  if (label === "打开 config.yaml") return openIt("config");
  if (label === "打开 persona.yaml") return openIt("persona");
  if (label === "打开日志") return openIt("log");
  if (label === "打开目录") return openIt("dir");
}

async function openIt(which) {
  const r = await api("/api/open", { which });
  toast(r.message, r.ok ? "ok" : "err");
}

async function botAction(action) {
  if (action === "stop") {
    const body = document.createElement("div");
    body.textContent = "会先写 data\\STOP（立刻不再回复），再结束进程。";
    return confirmBox("停止机器人？", body, async () => {
      toast("正在停止…");
      const r = await api("/api/bot", { action });
      toast(r.message, r.ok ? "ok" : "err");
      reload(true);
      if (PAGE === 0) setTimeout(() => reload(true), 1200);
    });
  }
  if (action === "start") {
    toast("正在启动…");
    const r = await api("/api/bot", { action });
    toast(r.message, r.ok ? "ok" : "err");
    reload(true);
    return;
  }
}

function renderStatusPage(host) {
  const bar = document.createElement("div");
  bar.className = "btn-row";
  for (const label of ["启动机器人", "停止机器人", "刷新状态"]) {
    const a = (SCHEMA.actions || []).find((x) => x.label === label);
    const b = document.createElement("button");
    b.className = "btn" + (a && a.primary ? " primary" : "") +
                  (a && a.danger ? " danger" : "");
    b.textContent = label;
    // 实时刷新要靠这个 data-action 找到按钮，改可用状态。
    b.dataset.action = label;
    b.disabled = (label === "启动机器人" && SCHEMA_STATE.running) ||
                 (label === "停止机器人" && !SCHEMA_STATE.running);
    b.onclick = () => runAction(label);
    bar.appendChild(b);
  }
  host.appendChild(bar);

  const h = document.createElement("div");
  h.className = "sec-head";
  h.textContent = "当前状态";
  host.appendChild(h);

  const grid = document.createElement("div");
  grid.className = "stat-grid";
  for (const s of SCHEMA_STATE.status) {
    const d = document.createElement("div");
    d.className = "stat";
    const k = document.createElement("span");
    k.className = "stat-k"; k.textContent = s.label;
    const v = document.createElement("span");
    v.className = "stat-v"; v.textContent = s.value || "—";
    d.append(k, v);
    grid.appendChild(d);
  }
  host.appendChild(grid);

  const lh = document.createElement("div");
  lh.className = "sec-head";
  lh.textContent = "运行日志";
  // 实时刷新指示。日志现在是每 2 秒自己刷的，得让用户看得见这件事在发生 ——
  // 否则「它不动」和「它已经是最新」看起来一模一样。
  const at = document.createElement("span");
  at.id = "liveAt";
  at.className = "live-at";
  at.textContent = "读取中…";
  lh.appendChild(at);
  host.appendChild(lh);
  const box = document.createElement("div");
  box.className = "logbox";
  box.id = "logbox";
  box.textContent = "(读取中…)";
  host.appendChild(box);
  // 这里**不再**单独 fetch 一次日志：render() 结尾会立刻调一次 liveTick()，
  // 内容由它填。两条路填同一个框，迟早会打架。

  const fh = document.createElement("div");
  fh.className = "sec-head";
  fh.textContent = "文件";
  host.appendChild(fh);
  const fr = document.createElement("div");
  fr.className = "btn-row";
  for (const [label, which] of [["打开 config.yaml", "config"],
      ["打开 persona.yaml", "persona"], ["打开 memes.yaml", "memes"],
      ["打开日志", "log"], ["打开目录", "dir"]]) {
    const b = document.createElement("button");
    b.className = "btn";
    b.textContent = label;
    b.onclick = () => openIt(which);
    fr.appendChild(b);
  }
  host.appendChild(fr);
}

let SCHEMA_STATE = null;

function render() {
  const page = SCHEMA.pages[PAGE];
  $("pageTitle").textContent = page.title;
  document.querySelectorAll(".nav-item").forEach((el, i) =>
    el.classList.toggle("active", i === PAGE));
  const host = $("content");
  host.innerHTML = "";
  host.scrollTop = 0;

  if (page.name === "pg_status") {
    renderStatusPage(host);
  } else {
    renderFieldBlock(page.items, host);
  }

  /* 给每个直接子元素编个号，CSS 用它算入场动画的延迟（依次浮现）。
     封顶 12 —— 再往后错开就得等太久，反而显得慢。 */
  [...host.children].forEach((el, i) =>
    el.style.setProperty("--i", Math.min(i, 12)));
  /* 重新加一次 .enter 让动画重放。
     必须先把类摘掉、强制一次样式重算（读 offsetWidth），
     否则浏览器会把「摘掉又加上」合并成没变化，动画不会重播。 */
  host.classList.remove("enter");
  void host.offsetWidth;
  host.classList.add("enter");

  updatePill();
  refreshDirty();
  // 页面刚建好就立刻拉一次实时数据（日志框还是「读取中…」）。
  // 定时器每 2 秒跑一次，但切页面不该等那 2 秒。
  liveTick();
}

/* ------------------------------------------------------------ 实时刷新 */
/* 日志区原来只在页面渲染时读一次，想看新日志只能点「刷新状态」——
   而那个按钮会把整页重建一遍（还会顺手丢掉没保存的修改）。
   现在改成独立轮询：只取运行状态 + 日志尾巴，**原地更新**，
   完全不碰任何输入控件，所以不可能影响正在编辑的内容。

   为什么是轮询而不是 SSE/WebSocket：后端是标准库的 http.server，
   加一条长连接要处理线程、重连、半开连接；而这里 2 秒一次 GET 一个
   很小的 JSON 就够了，本地回环没有带宽问题。简单优先。 */
const LIVE_MS = 2000;
let liveFails = 0;

function setLiveAt(text) {
  const el = $("liveAt");
  if (el && el.textContent !== text) el.textContent = text;
}

function updatePill() {
  const p = $("statusPill");
  if (!p || !SCHEMA_STATE) return;
  p.className = "pill" + (SCHEMA_STATE.stopped ? " stop"
                        : SCHEMA_STATE.running ? " on" : "");
  p.textContent = SCHEMA_STATE.stopped ? "运行中 · 已急停"
                : SCHEMA_STATE.running ? "运行中" : "已停止";
}

function applyLive(r) {
  SCHEMA_STATE.running = r.running;
  SCHEMA_STATE.stopped = r.stopped;
  updatePill();

  for (const [label, off] of [["启动机器人", r.running],
                              ["停止机器人", !r.running]]) {
    const b = document.querySelector(`[data-action="${label}"]`);
    if (b) b.disabled = off;
  }

  // 统计格子按顺序一一对应。数量对不上就整体不动 —— 宁可不刷，
  // 也不要把值错位到别的标签底下（那比不刷新糟糕得多）。
  const vals = document.querySelectorAll("#content .stat-v");
  if (r.status.length === vals.length) {
    r.status.forEach((s, i) => {
      if (vals[i].textContent !== s.value) vals[i].textContent = s.value;
    });
  }

  const box = $("logbox");
  if (box) {
    if (box.textContent !== r.log) {
      // 用户往上翻着看历史的时候，别把他拽回底部。只在「本来就在底部」时跟随。
      const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 4;
      box.textContent = r.log;
      if (atBottom) box.scrollTop = box.scrollHeight;
    }
    setLiveAt("实时 · " + r.at);
  }
}

async function liveTick() {
  // 窗口在后台就别轮询了：看不见的东西不需要每 2 秒刷一次。
  if (document.hidden) return;
  try {
    const r = await api("/api/live");
    liveFails = 0;
    applyLive(r);
  } catch (e) {
    liveFails += 1;
    if (liveFails >= 3) {
      setLiveAt("刷新失败");
      const box = $("logbox");
      if (box) box.textContent = "(读取失败：" + e.message + ")";
    }
  }
}

/* ---------------------------------------------------------------- 加载/保存 */
async function reload(silent = false) {
  try {
    // **刷新不能吃掉没保存的修改。** 原来这里直接把 VALUES 覆盖成磁盘值，
    // 于是点一下「刷新状态」刚改的东西就没了 —— 而且悄无声息，没有任何提示。
    // 现在先把「脏」的键记下来，读回来之后再盖回去。
    const dirty = SCHEMA ? changedKeys() : {};
    const st = await api("/api/state");
    SCHEMA = st.schema;
    SCHEMA_STATE = st;
    ORIGINAL = { ...st.values };
    VALUES = { ...st.values, ...dirty };
    if (!silent) { PAGE = 0; }
    renderNav();
    render();
    if (!silent) toast("已读取最新配置", "ok");
  } catch (e) {
    toast("读取失败：" + e.message, "err");
  }
}

async function save() {
  const changes = changedKeys();
  if (!Object.keys(changes).length) return;
  $("btnSave").disabled = true;
  try {
    const r = await api("/api/save", { changes });
    if (r.ok) {
      toast(r.message + "。persona.yaml 立即生效；config.yaml 需要重启机器人。", "ok");
      await reload(true);
    } else {
      const pre = document.createElement("pre");
      pre.textContent = r.detail || r.message;
      confirmBox("保存失败", pre, () => {});
      toast(r.message, "err");
      await reload(true);
    }
  } catch (e) {
    toast("保存出错：" + e.message, "err");
  }
  refreshDirty();
}

/* ---------------------------------------------------------------- 启动 */
$("btnReload").onclick = () => reload();
$("btnSave").onclick = save;
$("btnRevert").onclick = () => {
  VALUES = { ...ORIGINAL };
  render();
  toast("已放弃修改");
};
$("themeBtn").onclick = () => {
  THEME = THEME === "dark" ? "light" : THEME === "light" ? "" : "dark";
  localStorage.setItem("theme", THEME);
  applyTheme();
};

document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault(); save();
  }
});
window.addEventListener("beforeunload", (e) => {
  if (Object.keys(changedKeys()).length) { e.preventDefault(); e.returnValue = ""; }
});

applyTheme();
reload();

/* 实时刷新的心跳。只开一个，和页面切换无关 —— 切到别的页时
   #logbox 不存在，applyLive 会跳过日志那部分，状态灯照样在刷。 */
setInterval(liveTick, LIVE_MS);
// 从后台切回前台时立刻补一次，别让用户盯着最多 2 秒的旧数据。
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) liveTick();
});
