/* 工作台前端：只跟本地服务打交道（fetch / SSE），业务逻辑全在 Python 侧。
   界面语言：强调色只给「主按钮 / 选中 / 焦点 / 拖拽目标」，其余走灰阶。 */
const $ = (id) => document.getElementById(id);
const api = async (path, body) => {
  const opt = body ? {method: "POST", headers: {"Content-Type": "application/json"},
                      body: JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  try { return await r.json(); } catch (e) { return {ok: false, error: "服务返回异常"}; }
};
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const size = (n) => !n ? "" : (n > 1048576 ? (n / 1048576).toFixed(1) + " MB"
                                             : Math.max(1, Math.round(n / 1024)) + " KB");
let toastTimer = null;
function toast(msg) {
  const el = $("toast"); el.textContent = msg; el.classList.add("on");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove("on"), 2600);
  // 指挥台盖在上面时让它也显示一份，否则用户看不到任何反馈
  if (BOARD.open && BOARD.ready) BOARD.frame.contentWindow.postMessage({t: "toast", msg}, "*");
}

/* 流程指挥台：**不是弹窗**，是一整页 iframe（tools/workbench/flow.html）。
   这里只做三件事 —— 喂状态、执行它发来的命令、把进度与提示转过去；
   页面本体独立成 HTML，好改好换皮。声明放前面：toast 里要用到它。 */
const BOARD = {wrap: null, frame: null, open: false, ready: false};

/* 图标按钮：栏标题那排"小动作"用图标（主按钮仍带文字——一个按钮区最多一个主按钮） */
const ICON = {
  refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 3v6h-6"/></svg>',
  reload: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 12a8 8 0 0 1 13.7-5.7L20 8"/><path d="M20 3v5h-5"/><path d="M20 12a8 8 0 0 1-13.7 5.7L4 16"/><path d="M4 21v-5h5"/></svg>',
  copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>',
  plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
  x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
};
function paintIcons() {
  document.querySelectorAll("[data-icon]").forEach((b) => {
    if (!b.dataset.done) { b.innerHTML = ICON[b.dataset.icon] || ""; b.dataset.done = "1"; }
  });
}

/* ── 滑动变阻器：一条轨道 + 一个滑块，点一下或拖着走 ──────────────────
   为什么要拖：三选一的评测项用"一排按钮"每次都要瞄准点，拖起来更像调档位；
   点任意格仍然可用（不强迫拖）。滑块位置由 transform 控制，跟着指针走。 */
function mkRheostat(box, options, colors, value, onChange) {
  box.className = "rh";
  box.innerHTML = `<span class="knob"></span>` +
    options.map((o, i) => `<button data-i="${i}" data-c="${colors[i] || ""}">${esc(o)}</button>`).join("");
  const btns = [...box.querySelectorAll("button")];
  const knob = box.querySelector(".knob");
  let cur = Math.max(0, options.indexOf(value));
  const paint = () => {
    const w = box.clientWidth - 6;
    const seg = w / options.length;
    knob.style.width = seg + "px";
    knob.style.transform = `translateX(${cur * seg}px)`;
    btns.forEach((b, i) => {
      b.classList.toggle("on", i === cur);
      b.classList.toggle(b.dataset.c || "x", i === cur);
      if (i !== cur) b.classList.remove("g", "y", "r");
    });
  };
  const setIdx = (i, fire) => {
    i = Math.max(0, Math.min(options.length - 1, i));
    if (i === cur && fire !== "force") { paint(); return; }
    cur = i;
    paint();
    if (fire !== false && onChange) onChange(options[cur]);
  };
  const idxAt = (clientX) => {
    const r = box.getBoundingClientRect();
    const w = r.width / options.length;
    return Math.floor((clientX - r.left) / w);
  };
  let dragging = false;
  box.addEventListener("pointerdown", (e) => {
    dragging = true; box.classList.add("drag");
    try { box.setPointerCapture(e.pointerId); } catch (err) {}
    setIdx(idxAt(e.clientX));
  });
  box.addEventListener("pointermove", (e) => { if (dragging) setIdx(idxAt(e.clientX)); });
  const stop = (e) => { dragging = false; box.classList.remove("drag"); };
  box.addEventListener("pointerup", stop);
  box.addEventListener("pointercancel", stop);
  btns.forEach((b) => { b.onclick = () => setIdx(+b.dataset.i); });
  requestAnimationFrame(paint);
  return {set: (v) => setIdx(options.indexOf(v), false), paint};
}

function logLine(text, cls) {
  const el = $("log");
  el.insertAdjacentHTML("beforeend",
    `<div class="${cls || ""}">${esc(text)}</div>`);
  el.scrollTop = el.scrollHeight;
}

/* ── 状态 ─────────────────────────────────────────────────────────── */
const S = {state: null, pending: [], review: {}, dims: [], video: "", agentTimer: null,
           rh: {}, agent: null, manualStep: null, prompts: [], uploads: [], prog: null};

const STEPS = [
  {n: "丢素材", who: "你", man: "把素材（文案/形象图/音频/原片）拖进②栏，然后点「建框架归类」"},
  {n: "出提示词", who: "agent", man: "点「出提示词」，agent 会读技能与框架、写完写回本栏"},
  {n: "去平台生成", who: "你", man: "复制提示词 → 上传清单里的素材 → 手选 9:16 → 生成"},
  {n: "收成片", who: "你", man: "生成完把成片拖回②栏（或点「收成片」选文件）"},
  {n: "打分反馈", who: "软件", man: "在④栏逐项打分 → 保存；写清现象 → 提交让 agent 再出一版"},
];

function currentStep() {
  const st = S.state;
  if (!st || !st.project) return {i: 0, why: "先在①栏点一个项目；没有就把素材拖进②栏建新的"};
  const f = st.project.folders || {};
  const mats = (f["素材"] || []).length + (f["文案"] || []).length;
  const prompts = S.prompts || [];
  const vids = (f["成片"] || []).length;
  const revs = (f["评价"] || []).length;
  if (!mats && !S.pending.length) return {i: 0, why: "把素材拖进②栏"};
  if (S.pending.length) return {i: 0, why: "还有没归类的素材，点「建框架归类」"};
  if (!prompts.length) return {i: 1, why: "素材已归类，点「出提示词」让 agent 开工"};
  if (!vids) return {i: 2, why: "复制提示词去平台生成，成片回来拖进②栏"};
  if (!revs) return {i: 4, why: "成片到了，去④栏打分保存"};
  return {i: 4, why: "打分已保存。还想改就写反馈 → 提交让 agent 再出一版"};
}

function activeStep() {                       // 手动选的优先（可回到上一步），没选就按状态自动判定
  return S.manualStep == null ? currentStep().i : S.manualStep;
}

function renderFlow() {
  const auto = currentStep().i;
  const cur = activeStep();
  $("flow").innerHTML = STEPS.map((s, i) => {
    const cls = i < cur ? "done" : (i === cur ? "now" : "");
    const man = (S.manualStep != null && i === cur) ? " manual" : "";
    return `<div class="step ${cls}${man}" data-i="${i}" title="点一下切到这一步（可以回退）">`
         + `<i class="dot"></i>${esc(s.n)}<span class="who">${esc(s.who)}</span></div>`
         + (i < STEPS.length - 1 ? '<span class="sep"></span>' : "");
  }).join("") + (S.manualStep != null ? '<button class="autochip" id="btnAuto">回到自动</button>' : "");
  $("flow").querySelectorAll(".step").forEach((el) => {
    el.onclick = () => { S.manualStep = +el.dataset.i; renderFlow(); };
  });
  const au = $("btnAuto");
  if (au) au.onclick = () => { S.manualStep = null; renderFlow(); };

  const c = currentStep();
  const who = (c.i === 1 && S.agent && S.agent.label) ? `（用 ${esc(S.agent.label)}）` : "";
  $("nextText").innerHTML = `<b>${esc(STEPS[cur].n)}</b>：`
    + (cur === auto ? esc(c.why) + who : "你手动切到了这一步；点「回到自动」恢复按状态判断");
  const prev = $("btnPrev"), next = $("btnNext");
  if (prev) { prev.disabled = cur <= 0; prev.onclick = () => stepBy(-1); }
  if (next) { next.disabled = cur >= STEPS.length - 1; next.onclick = () => stepBy(1); }
  const b = $("nextBtn");
  b.disabled = false;
  b.textContent = ["建框架归类", "出提示词", "复制提示词", "收成片", "去打分"][cur];
  b.onclick = () => {
    if (cur === 0) return intakeGo();
    if (cur === 1) return askAgent("prompt");
    if (cur === 2) return copyPrompt();
    if (cur === 3) return receive(true);
    $("scoreCol").scrollIntoView({behavior: "smooth", block: "start"});
  };
  hintColumns();
  syncBoard();
}

/* 当前阶段对应哪一栏 → 那一栏高亮（用户不用猜"现在该看哪儿"） */
const STEP_COL = [1, 2, 2, 2, 3];
function stepBy(d) {                 // 上一步 / 下一步（手动切换，可回退）
  const cur = activeStep();
  const t = Math.max(0, Math.min(STEPS.length - 1, cur + d));
  S.manualStep = (t === currentStep().i) ? null : t;    // 回到自动判定时清掉手动标记
  renderFlow();
}

/* ── 主题：默认「原版」＝用户基准页那套；其余只换 CSS 变量 ───────────────── */
const THEMES = [
  ["原版", "#f4f6f9", "#2f6fed"], ["拾光", "#f6f4f1", "#e4622e"],
  ["深色", "#1b2028", "#5b8def"], ["莫兰迪", "#faf8f5", "#7d8f76"],
  ["护眼绿", "#f6faf4", "#2e7d32"], ["暗夜", "#15203a", "#4f8cff"],
  ["暖夜", "#241f1c", "#e0913f"],
];
function applyTheme(name) {
  document.body.dataset.theme = (name === "原版") ? "" : name;
  try { localStorage.setItem("heronbo.theme", name); } catch (e) {}
  const b = $("btnTheme");
  if (b) { b.textContent = "主题 · " + name; b.title = "换主题（默认原版）"; }
  syncBoard();               // 板子是独立文档，主题得喂给它（不然它自成一套配色）
}
function themeModal() {
  const cur = (document.body.dataset.theme || "原版");
  const rows = THEMES.map(([n, bg, ac]) =>
    `<div class="arow ${n === cur ? "on" : ""}" data-t="${esc(n)}" style="cursor:pointer">
       <i class="adot" style="background:${ac};box-shadow:0 0 0 3px ${ac}22"></i>
       <span class="an">${esc(n)}</span>
       <span class="aw">底色 <code>${bg}</code> · 强调 <code>${ac}</code></span>
       <span class="ar">${n === cur ? "✓ 当前" : "点这里用"}</span></div>`).join("");
  const m = document.createElement("div");
  m.className = "modal";
  m.innerHTML = `<div class="box"><h3>主题</h3>
    <div class="amod">${rows}</div>
    <div style="text-align:right;margin-top:12px"><button class="btn" id="mclose">知道了</button></div></div>`;
  document.body.appendChild(m);
  m.querySelector("#mclose").onclick = () => m.remove();
  m.onclick = (e) => { if (e.target === m) m.remove(); };
  m.querySelectorAll(".arow").forEach((r) => {
    r.onclick = () => { applyTheme(r.dataset.t); m.remove(); toast("主题已换成 " + r.dataset.t); };
  });
}

function hintColumns() {
  const cur = activeStep();
  document.querySelectorAll(".col").forEach((el, i) => {
    el.classList.toggle("hl", i === STEP_COL[cur]);
  });
}

/* ── 流程指挥台：喂状态 + 执行命令 ─────────────────────────────────────
   板上"怎么做"的文案与布局在 flow.html/flow.js 里；动作仍只有这一套实现（命令发回来执行），
   所以以后改动作只改这里，不会出现"板上一套、四栏里一套"两个版本。 */
function boardFacts() {
  const st = S.state || {};
  const f = (st.project && st.project.folders) || {};
  const pr = (st.project && st.project.state) || {};
  const cnt = (k) => (f[k] || []).length;
  const ps = S.prompts || [], ups = S.uploads || [];
  const names = ups.slice(0, 2).map((u) => u.name).join("、");
  return [
    `待归类 ${S.pending.length} 项 · 已归类：素材 ${cnt("素材")} · 文案 ${cnt("文案")}`,
    ps.length ? `提示词 ${ps.length} 条（最新 ${esc(ps[ps.length - 1].ver || "v")}） · 即梦上传 ${ups.length} 件`
              : "还没有提示词",
    ups.length ? `上传清单 ${ups.length} 件：${esc(names)}${ups.length > 2 ? " 等" : ""}`
               : "上传清单还是空的（agent 出提示词时会按引用编号放进来）",
    `成片 ${cnt("成片")} · 废片 ${cnt("废片")}`,
    `评分 ${cnt("评价")} 份 · 阶段 ${esc(pr.stage || "—")} · 第 ${pr.round || 0} 轮`,
  ];
}

function boardState() {
  const c = currentStep();
  const st = S.state || {};
  const fo = (st.project && st.project.folders) || {};
  return {
    t: "init",
    theme: document.body.dataset.theme || "原版",
    build: (st.build && st.build.stamp) || "",
    root: st.root || st.rootConfigured || "",
    project: st.project ? {name: st.project.name, dir: st.project.dir,
                           createdAt: st.project.createdAt} : null,
    steps: STEPS, cur: activeStep(), auto: c.i, why: c.why,
    manual: S.manualStep != null,
    prompts: {n: (S.prompts || []).length},
    uploads: (S.uploads || []).map((u) => u.name),
    // 「评价反哺 skill」的门槛：有评分或有废片才亮
    hasReview: ((fo["评价"] || []).length + (fo["废片"] || []).length) > 0,
    pending: S.pending || [], facts: boardFacts(),
    need: (S.state && S.state.need) || "",
    wenan: (S.wenan || []).length,
    mats: (S.mats || []).map((m) => ({name: m.name, role: m.role, size: m.size,
                                      missing: m.missing})),
    agent: S.agent || {}, prog: S.prog || null,
  };
}

function syncBoard() {                 // 状态一变就喂一遍；板没开就什么都不做
  if (!BOARD.open || !BOARD.ready) return;
  const w = BOARD.frame.contentWindow;
  if (w) w.postMessage(boardState(), "*");
}

function boardProg() {
  if (!BOARD.open || !BOARD.ready) return;
  const w = BOARD.frame.contentWindow;
  if (w) w.postMessage({t: "prog", prog: S.prog}, "*");
}

function openBoard() {
  BOARD.wrap.hidden = false;
  BOARD.open = true;
  if (!BOARD.frame.getAttribute("src")) BOARD.frame.setAttribute("src", "flow.html");
  syncBoard();
}

function closeBoard() {
  BOARD.wrap.hidden = true;
  BOARD.open = false;
  loadState();                         // 板上做过的事可能改了项目状态，回来刷一遍
}

function boardAct(d) {
  const k = d.k;
  if (k === "pick") return pickFiles("files");
  if (k === "pickdir") return pickFiles("dir");
  if (k === "intake") return intakeGo();
  if (k === "ask") return askAgent("prompt");
  if (k === "copy") return copyPrompt();
  if (k === "reload") return reloadPrompts();
  if (k === "good") return receive(true);
  if (k === "bad") { $("why").value = d.why || ""; return receive(false); }
  if (k === "score") {                 // "去④栏打分"就回四栏去（板上打分太挤）
    S.manualStep = 4; renderFlow(); closeBoard();
    setTimeout(() => $("scoreCol").scrollIntoView({behavior: "smooth", block: "start"}), 60);
    return;
  }
  if (k === "feedback") return submitFeedback(d.fb);
  if (k === "intakeCheck") return askAgent("intake");     // 让 agent 逐件核对角色归类
  if (k === "learn") return askAgent("learn");            // 评价/废因沉淀回 skill
}

window.addEventListener("message", (ev) => {
  const d = ev.data || {};
  if (d.t === "hello") { BOARD.ready = true; syncBoard(); return; }
  if (d.t !== "cmd") return;
  if (d.cmd === "close") return closeBoard();
  if (d.cmd === "goto") { S.manualStep = (+d.i === currentStep().i) ? null : +d.i; return renderFlow(); }
  if (d.cmd === "step") return stepBy(d.d);
  if (d.cmd === "auto") { S.manualStep = null; return renderFlow(); }
  if (d.cmd === "agent") return agentModal();
  if (d.cmd === "act") return boardAct(d);
});

/* ── 渲染 ─────────────────────────────────────────────────────────── */
function renderSamples() {
  const st = S.state;
  const rp = $("rootPath");
  rp.textContent = st.root || st.rootConfigured || "（未配置样本库根）";
  rp.title = st.root || st.rootConfigured || "";
  const issue = $("rootIssue");
  if (st.rootIssue) {
    issue.hidden = false;
    issue.innerHTML = "<span>" + esc(st.rootIssue) + "</span>" +
      '<button class="btn ghost sm" id="btnSetRoot">选样本库根</button>';
    issue.querySelector("#btnSetRoot").onclick = async () => {
      const r = await api("/api/pick", {kind: "dir"});
      if (!r.paths || !r.paths.length) return;
      const w = await api("/api/root", {dir: r.paths[0]});
      if (!w.ok) return toast(w.error || "设置失败");
      toast("样本库根已设为 " + w.root);
      await loadState();
    };
  } else {
    issue.hidden = true;
  }
  const cur = st.project ? st.project.dir : "";
  $("sampleList").innerHTML = (st.samples || []).map((s) =>
    `<div class="row ${s.dir === cur ? "on" : ""}" data-dir="${esc(s.dir)}">
       <span class="g">${esc(s.name)}</span>
       <span class="m">素材 ${s.materials} · 成片 ${s.videos}</span></div>`).join("")
    || '<div class="meta">样本库里还没项目。把素材拖进②栏就能建。</div>';
  $("sampleList").querySelectorAll(".row").forEach((el) => {
    el.onclick = () => selectProject(el.dataset.dir);
  });
}

/* agent 通道：显示选中谁，点开看候选（exe 跟着 skill 走，谁装了本技能就用谁） */
function renderAgent(agent) {
  const chip = $("btnAgent");
  const ok = agent && agent.ok;
  chip.classList.toggle("bad", !ok);
  chip.textContent = ok ? `agent ${agent.label}` : "agent 不可用";
  const usable = (((agent || {}).list) || []).filter((r) => r.ok).length;
  chip.title = ok
    ? ((agent.chosen ? "已选定：" : "自动挑选：") + (agent.why || "")
       + (usable > 1 && !agent.chosen ? "（点一下可改）" : ""))
    : ((agent && agent.why) || "没找到可用的 agent");
  $("btnAsk").disabled = !ok;
  $("btnAsk").title = ok ? ("用 " + (agent.label || "") + " 出提示词") : "agent 不可用";
  S.agent = agent;
}

function agentModal() {
  const a = S.agent || {};
  const rows = (a.list || []).map((r) => {
    const now = r.ok && r.key === a.picked;
    const cls = (r.ok ? "" : " no") + (now ? " on" : "");
    const why = r.ok ? (r.host ? "本 skill 就装在它名下" : "本机可用") : (r.why || "不可用");
    const right = now ? "✓ 当前使用" : (r.ok ? "点这里切换" : "不可用");
    return `<div class="arow${cls}" data-k="${esc(r.key)}" data-ok="${r.ok ? 1 : 0}" title="${esc(r.why || "")}">
        <i class="adot ${r.ok ? "g" : "r"}"></i>
        <span class="an">${esc(r.label)}</span>
        <span class="aw">${esc(why)}</span>
        <span class="ar">${right}</span>
      </div>`;
  }).join("");
  const m = document.createElement("div");
  m.className = "modal";
  m.innerHTML = `<div class="box"><h3>agent 通道</h3>
    <p class="meta">工作台跟着 skill 走：谁把本技能装在自己名下、且命令行可用，就用谁。
      <span style="color:var(--ok)">● 可用</span>　<span style="color:var(--bad)">● 不可用</span>　点一行即可切换。</p>
    <div class="amod">${rows}</div>
    <p class="note" style="margin-top:4px">选择记在 <code>${esc(a.cfg || "—")}</code>（关掉工作台也在）。
      想直接写文件：<code>{"agent": "codex"}</code>；要接没适配的命令行：
      <code>{"cmd": ["命令", "{prompt}"], "cmd_mode": "text"}</code>。</p>
    <div style="text-align:right;margin-top:12px">
      ${a.chosen ? '<button class="btn ghost" id="mauto">恢复自动挑选</button>' : ""}
      <button class="btn" id="mclose">知道了</button></div>
    </div>`;
  document.body.appendChild(m);
  m.querySelector("#mclose").onclick = () => m.remove();
  m.onclick = (e) => { if (e.target === m) m.remove(); };
  m.querySelectorAll(".arow").forEach((row) => {
    row.onclick = async () => {
      if (row.dataset.ok !== "1") return toast("这个 agent 现在不可用");
      const r = await api("/api/agent/set", {key: row.dataset.k});
      if (!r.ok) return toast(r.why || "设置失败");
      S.agent = r.agent; renderAgent(S.agent); m.remove();
      toast("已切换为 " + (S.agent.label || ""));
    };
  });
  const auto = m.querySelector("#mauto");
  if (auto) auto.onclick = async () => {
    const r = await api("/api/agent/set", {key: ""});
    if (!r.ok) return toast(r.why || "设置失败");
    S.agent = r.agent; renderAgent(S.agent); m.remove();
    toast("已恢复自动挑选（当前 " + (S.agent.label || "") + "）");
  };
}

function renderProject() {
  const p = S.state.project;
  $("projName").textContent = p ? p.name : "未选项目";
  if (!p) { $("projCard").innerHTML = '<span class="meta">还没选项目</span>'; }
  else {
    const f = p.folders || {};
    const cnt = (k) => (f[k] || []).length;
    const st = p.state || {};
    $("projCard").innerHTML =
      `<div><b>${esc(p.name)}</b></div>
       <div class="meta" style="margin-top:4px">素材 ${cnt("素材")} · 成片 ${cnt("成片")} ·
         废片 ${cnt("废片")} · 评分 ${cnt("评价")}</div>
       <div class="meta">阶段 ${esc(st.stage || "—")} · 第 ${st.round || 0} 轮 ·
         待办 ${st.pending || 0}</div>
       <div class="meta" style="word-break:break-all">${esc(p.dir)}</div>`;
  }
  // ②栏「文案 / 素材识别」：框架里登记的素材（**顺序＝引用编号顺序**，可拖动改）+ 没登记的兜底
  const f = p ? (p.folders || {}) : {};
  const rows = S.mats || [];
  $("matList").innerHTML = rows.length ? rows.map(matRow).join("")
    : '<div class="meta">还没有素材——丢进来 → 点右上「建框架归类」</div>';
  bindMatRows($("matList"), "material");
  // ④栏成片下拉
  const vids = (f["成片"] || []).map((x) => x.name);
  $("videoSel").innerHTML = (vids.length ? vids : ["（无成片）"])
    .map((v) => `<option>${esc(v)}</option>`).join("");
}

/* 素材一行：拖动手柄 + 角色 + 文件名 + 大小 + 移除（移除＝移到项目里的 _已移除/，不真删） */
function matRow(m) {
  const role = m.role ? esc(m.role) : "待识别";
  return `<div class="row mat${m.role ? "" : " noready"}${m.missing ? " gone" : ""}"
      draggable="true" data-file="${esc(m.file)}" data-name="${esc(m.name)}">
    <span class="grip" title="拖动改顺序（顺序就是 @图片1 / @音频1 的编号顺序）">⋮⋮</span>
    <span class="role">${role}</span>
    <span class="g">${esc(m.name)}</span>
    <span class="m">${m.missing ? "文件不在" : size(m.size)}</span>
    <button class="ibtn sm" data-del="1" data-icon="x"
      title="移除这件素材（文件移到项目里的 _已移除/，不会真删）"></button>
  </div>`;
}

/* 素材行的交互：移除按钮 + 拖动排序。两种列表都走这里（已登记 / 待投放）。 */
function bindMatRows(box, kind) {
  paintIcons();
  box.querySelectorAll(".row.mat").forEach((row) => {
    const del = row.querySelector("[data-del]");
    if (del) {
      del.onclick = async (e) => {
        e.stopPropagation();
        if (kind === "pending") {
          const r = await api("/api/material/remove",
                              {pending: S.pending[+row.dataset.i], name: row.dataset.name});
          if (r.ok) { S.pending = r.pending || []; renderPending(); renderFlow(); }
          else toast(r.error || "移除失败");
          return;
        }
        const r = await api("/api/material/remove", {file: row.dataset.file,
                                                     name: row.dataset.name});
        if (!r.ok) return toast(r.error || "移除失败");
        toast("已移除到项目里的 _已移除/（能捞回来）");
        logLine("已移除素材：" + row.dataset.name, "warn");
        await loadState();
      };
    }
  });
  // 拖动排序：只在同一种列表内拖；拖的是"顺序"，不是文件投放
  box.querySelectorAll(".row.mat").forEach((row) => {
    row.addEventListener("dragstart", (e) => {
      row.classList.add("drag");
      try {
        e.dataTransfer.setData("text/heronbo-mat", row.dataset.file || row.dataset.name || "");
        e.dataTransfer.effectAllowed = "move";
      } catch (err) {}
    });
    row.addEventListener("dragend", () => {
      row.classList.remove("drag");
      box.querySelectorAll(".over").forEach((x) => x.classList.remove("over"));
    });
    row.addEventListener("dragover", (e) => {
      if (row.classList.contains("drag")) return;
      e.preventDefault();
      // 落在上半 → 插到这行前面；落在下半 → 插到这行后面。
      // 少了这个判断，"往下拖一行"会变成空操作（插到目标前面＝原地不动，2026-09-16 实测踩到）。
      const r = row.getBoundingClientRect();
      const after = (e.clientY || 0) > r.top + r.height / 2;
      row.classList.toggle("over", !after);
      row.classList.toggle("over-after", after);
    });
    row.addEventListener("dragleave", () => {
      row.classList.remove("over"); row.classList.remove("over-after");
    });
    row.addEventListener("drop", async (e) => {
      const dragged = box.querySelector(".row.drag");
      e.preventDefault(); e.stopPropagation();
      const r = row.getBoundingClientRect();
      const after = (e.clientY || 0) > r.top + r.height / 2;
      row.classList.remove("over"); row.classList.remove("over-after");
      if (!dragged || dragged === row) return;
      if (after) box.insertBefore(dragged, row.nextSibling);
      else box.insertBefore(dragged, row);
      if (kind === "pending") {                 // 待投放只改页面上的次序
        const order = [...box.querySelectorAll(".row.mat")].map((x) => x.dataset.i);
        S.pending = order.map((i) => S.pending[+i]);
        renderPending();
        return;
      }
      const files = [...box.querySelectorAll(".row.mat")].map((x) => x.dataset.file);
      const r2 = await api("/api/material/order", {files});
      if (!r2.ok) return toast(r2.error || "排序没存上");
      toast("顺序已存（@图片1/@音频1 按这个来）");
      await loadState();
    });
  });
}

function renderPending() {
  // 待投放的也放在「文案 / 素材识别」那张清单里，角色标「待识别」，跟已登记的一起能拖能删
  $("pendList").innerHTML = S.pending.length ? S.pending.map((p, i) =>
    `<div class="row mat noready" draggable="true" data-i="${i}" data-pend="${esc(p)}"
        data-name="${esc(p.split(/[\\/]/).pop())}">
      <span class="grip" title="拖动改顺序">⋮⋮</span>
      <span class="role">待识别</span>
      <span class="g">${esc(p.split(/[\\/]/).pop())}</span>
      <span class="m">待归类</span>
      <button class="ibtn sm" data-del="1" data-icon="x" title="从待投放里拿掉"></button>
    </div>`).join("") : "";
  bindMatRows($("pendList"), "pending");
  syncBoard();
}

function renderPrompts() {
  /* 这里**只放提示词正文**（能直接复制去平台的那份成品）。文案稿不在这一栏——
     2026-09-16 用户指出：从前把 文案/*.txt 也算提示词，于是没出提示词时这一栏显示的是文案稿。 */
  const ps = S.prompts || [];
  $("promptBox").textContent = ps.length
    ? ps.map((p) => `【${p.ver || "v"}${p.ratio ? " · " + p.ratio : ""}】\n${p.text || ""}`).join("\n\n")
    : "（还没有提示词正文——素材归类后点右上「出提示词」）";
  $("promptBox").scrollTop = 0;                 // 重新读取后回到开头，别停在半截
  const ups = S.uploads || [];
  $("upList").innerHTML = ups.length ? ups.map((u) =>
    `<div class="row plain"><span class="g">${esc(u.name)}</span>
      <span class="m">${size(u.size)}</span></div>`).join("")
    : '<div class="meta">还没生成（agent 出提示词时会按引用编号把副本放进来）</div>';
  const st = S.state.project ? (S.state.project.state || {}) : {};
  const wf = (S.wenan || []).length;
  $("stageLab").textContent = `· 阶段 ${st.stage || "—"} · 第 ${st.round || 0} 轮 · 待办 ${st.pending || 0}`
    + (wf ? ` · 文案稿 ${wf} 份（不在这一栏）` : "");
}

function renderScoreForm() {
  const st = S.state;
  S.rh = {};
  $("dimBox").innerHTML = (st.dims || []).map((d) =>
    `<div class="fld"><label title="${esc(d.hint || "")}">${esc(d.key)}</label>
      <div class="rhbox" data-key="${esc(d.key)}"></div></div>`).join("");
  (st.dims || []).forEach((d) => {
    const colors = (d.options || []).map((o, i, a) =>
      i === 0 ? "g" : (i === a.length - 1 ? "r" : "y"));
    const box = $("dimBox").querySelector(`.rhbox[data-key="${CSS.escape(d.key)}"]`);
    S.rh[d.key] = mkRheostat(box, d.options || [], colors, (d.options || [])[0],
                             (v) => { S.review.六维[d.key] = v; });
  });
  $("forbidBox").innerHTML = (st.forbid || []).map((k) =>
    `<span class="sw" data-key="${esc(k)}"><i></i>${esc(k)}</span>`).join("");
  const conclBox = $("conclBox");
  S.rh["结论"] = mkRheostat(conclBox, st.concl || ["可用", "可改", "作废"],
                            ["g", "y", "r"], (st.concl || ["可用"])[0],
                            (v) => { S.review.结论 = v; });
  $("stars").innerHTML = [1, 2, 3, 4, 5].map((i) =>
    `<span data-v="${i}">★</span>`).join("");
  $("forbidBox").querySelectorAll(".sw").forEach((sw) => {
    sw.onclick = () => {
      sw.classList.toggle("on");
      S.review.违禁项[sw.dataset.key] = sw.classList.contains("on")
        ? st.forbid_on || "有" : st.forbid_default || "无";
    };
  });
  $("stars").querySelectorAll("span").forEach((sp) => {
    sp.onclick = () => { S.review.整体评分 = +sp.dataset.v; paintStars(); };
  });
  paintStars();
  window.addEventListener("resize", () => {
    Object.values(S.rh).forEach((r) => r && r.paint && r.paint());
  });
}

function paintStars() {
  const v = S.review.整体评分 || 0;
  $("stars").innerHTML = [1, 2, 3, 4, 5].map((i) =>
    i <= v ? `<span data-v="${i}"><b>★</b></span>` : `<span data-v="${i}">★</span>`).join("");
  $("stars").querySelectorAll("span").forEach((sp) => {
    sp.onclick = () => { S.review.整体评分 = +sp.dataset.v; paintStars(); };
  });
}

function fillReview(rev) {
  rev = rev || {};
  S.review = {六维: Object.assign({}, rev["六维"] || {}),
              违禁项: Object.assign({}, rev["违禁项"] || {}),
              整体评分: rev["整体评分"] || 4, 结论: rev["结论"] || "可改",
              备注: rev["备注"] || ""};
  (S.state.dims || []).forEach((d) => {
    const v = S.review.六维[d.key];
    if (S.rh[d.key]) S.rh[d.key].set(v || (d.options || [])[0]);
  });
  if (S.rh["结论"]) S.rh["结论"].set(S.review.结论);
  $("forbidBox").querySelectorAll(".sw").forEach((sw) => {
    sw.classList.toggle("on", (S.review.违禁项[sw.dataset.key] || "无") === "有");
  });
  $("note").value = S.review.备注;
  paintStars();
}

/* ── 动作 ─────────────────────────────────────────────────────────── */
async function loadState(scrollTop) {
  const st = await api("/api/state");
  S.state = st;
  S.dims = st.dims || [];
  S.mats = st.materials || [];
  S.pending = st.pending || [];        // 待投放以服务端为准（刷新页面后不会"丢"）
  const pr = await api("/api/prompts");
  S.prompts = pr.prompts || []; S.uploads = pr.uploads || []; S.wenan = pr.wenan || [];
  const rv = await api("/api/review");
  renderSamples(); renderProject(); renderPrompts();
  const nd = $("need");                 // 需求：别在用户正打字时覆盖他的输入
  if (nd && document.activeElement !== nd) nd.value = st.need || "";
  renderScoreForm(); fillReview(rv.review);
  renderFlow(); renderPending(); renderAgent(st.agent);
  paintIcons();
  const bs = $("buildStamp");
  const p = st.project;
  bs.textContent = p ? `项目创建 ${p.createdAt || "（未知）"}` : `构建 ${st.build.stamp}`;
  bs.title = `exe 构建 ${st.build.stamp}` + (p ? ` · 项目 ${p.name}` : "");
  if (!st.agent.ok) logLine("agent 通道不可用：" + st.agent.why, "bad");
  syncBoard();
}

async function selectProject(dir) {
  const r = await api("/api/project", {dir});
  if (!r.ok) return toast(r.error || "切项目失败");
  S.pending = [];
  logLine("已切到项目 " + dir.split(/[\\/]/).pop());
  await loadState();
}

/* ── ①栏「＋ 新建项目」：起一张白纸（空素材、空提示词）─────────────────
   为什么要：工作台记着"上次打开的项目"，用旧项目干活时它身上已经堆满了东西，
   想从干净状态开始得自己去样本库翻。 */
function newProjectModal() {
  const d = new Date();
  const guess = "新项目-" + String(d.getMonth() + 1).padStart(2, "0")
    + String(d.getDate()).padStart(2, "0") + "-"
    + String(d.getHours()).padStart(2, "0") + String(d.getMinutes()).padStart(2, "0");
  const m = document.createElement("div");
  m.className = "modal";
  m.innerHTML = `<div class="box"><h3>新建项目</h3>
    <p class="meta">建在样本库根下，空素材、空提示词。名字留空就自动起一个。</p>
    <div class="rowline" style="margin:10px 0 4px">
      <input id="npName" value="${esc(guess)}" placeholder="项目名，例如 四六级背单词带货"></div>
    <p class="note" style="margin-top:10px">建完把素材拖进②栏即可；想换项目点①栏列表里的名字。</p>
    <div style="text-align:right;margin-top:12px">
      <button class="btn ghost" id="npCancel">取消</button>
      <button class="btn" id="npOk">建这个项目</button></div></div>`;
  document.body.appendChild(m);
  const close = () => m.remove();
  m.querySelector("#npCancel").onclick = close;
  m.onclick = (e) => { if (e.target === m) close(); };
  m.querySelector("#npName").focus();
  m.querySelector("#npOk").onclick = async () => {
    const r = await api("/api/project/new", {name: m.querySelector("#npName").value.trim()});
    if (!r.ok) return toast(r.error || "建项目失败");
    close();
    S.pending = [];
    logLine("已新建项目：" + ((r.project || {}).name || r.dir), "ok");
    toast("新项目建好了，把素材拖进②栏");
    await loadState();
  };
}

/* ── ②栏「简单需求」：落进项目（框架.json→need + 备注/需求.txt），agent 会读它 ── */
async function saveNeed() {
  const r = await api("/api/need", {text: $("need").value});
  if (!r.ok) return toast(r.error || "存不上");
  if (S.state) S.state.need = $("need").value.trim();   // 本地状态跟着更新，指挥台那张卡也才显示
  $("needHint").textContent = "已存（agent 会按它来）";
  syncBoard();
  toast(r.pending ? "先记着了；建框架归类时带进新项目" : "需求已存进这个项目");
}

async function pickFiles(kind) {
  const r = await api("/api/pick", {kind});
  if (r.error) return toast(r.error);
  if (!r.paths || !r.paths.length) return;
  if (kind === "dir" || (r.paths.length === 1 && /[\\/]$/.test(r.paths[0]))) {
    await intake(r.paths);
  } else {
    await intake(r.paths);
  }
}

async function intake(paths) {
  const r = await api("/api/intake", {paths});
  if (r.error) return toast(r.error);
  S.pending = r.pending || [];
  renderPending(); renderFlow();
  toast(`已收下 ${paths.length} 个路径`);
}

async function intakeGo() {
  if (!S.pending.length) return toast("先把素材拖进②栏");
  $("btnIntakeGo").disabled = true;
  const r = await api("/api/intake/go", {});
  $("btnIntakeGo").disabled = false;
  if (!r.ok) return toast(r.error || "建框架失败");
  S.pending = [];
  logLine("已建框架并归类：" + (r.project ? r.project.name : ""), "ok");
  toast("框架已建好，素材归类完成");
  await loadState();
}

async function uploadFile(file) {
  /* 直接送 File 对象（浏览器会带 Content-Length）；**别用 file.stream()**：
     fetch 的流式 body 必须配 duplex:"half"，否则 Chromium 直接抛 TypeError →
     界面只会显示"投递失败"（2026-09-15 用户截图里的那条提示就是这个原因）。*/
  try {
    const r = await fetch("/api/upload", {
      method: "POST",
      headers: {"X-File-Name": encodeURIComponent(file.name)},
      body: file,
    });
    const j = await r.json();
    if (j.ok) { S.pending = j.pending || []; renderPending(); renderFlow(); }
    else logLine("上传失败：" + (j.error || ""), "bad");
    return j.ok;
  } catch (e) {
    logLine("上传出错：" + e.message, "bad");
    return false;
  }
}

/* ── agent 选择门 ──────────────────────────────────────────────────
   本机检索到多个可用 agent 时，第一次点"出提示词"先问一次用哪个，选完写进
   agent_bridge.local.json 记住；以后不再问（想换就点③栏 agent 徽章）。只有一个
   可用时直接用它，不打断。 */
function usableAgents() { return (((S.agent || {}).list) || []).filter((r) => r.ok); }

function ensureAgent(cb) {
  const a = S.agent || {};
  const use = usableAgents();
  if (!use.length) return toast("没找到可用的 agent：" + (a.why || ""));
  if (a.chosen || use.length === 1) return cb();
  pickAgentModal(cb);
}

function pickAgentModal(cb) {
  const rows = (((S.agent || {}).list) || []).map((r) => {
    const tags = (r.host ? '<span class="who">装了本技能</span> ' : "") +
      (r.ok ? '<span style="color:var(--ok)">可用</span>'
            : '<span class="meta">' + esc(r.why) + "</span>");
    const btn = r.ok ? '<button class="btn sm" data-k="' + esc(r.key) + '">用它</button>' : "";
    return '<div class="step"><b style="flex:0 0 92px">' + esc(r.label) +
           '</b><span style="flex:1">' + tags + "</span>" + btn + "</div>";
  }).join("");
  const m = document.createElement("div");
  m.className = "modal";
  m.innerHTML = '<div class="box"><h3>这次用哪个 agent？</h3>' +
    '<p class="meta">本机检索到多个可用 agent。选一个我就记住，以后不再问；想换点③栏的 agent 徽章。</p>' +
    rows +
    '<div style="text-align:right;margin-top:12px"><button class="btn ghost" id="mclose">取消</button></div></div>';
  document.body.appendChild(m);
  m.querySelector("#mclose").onclick = () => m.remove();
  m.onclick = (e) => { if (e.target === m) m.remove(); };
  m.querySelectorAll("button[data-k]").forEach((b) => {
    b.onclick = async () => {
      const r = await api("/api/agent/set", {key: b.dataset.k});
      if (!r.ok) return toast(r.why || "设置失败");
      S.agent = r.agent;
      renderAgent(S.agent);
      m.remove();
      toast("以后就用 " + (S.agent.label || ""));
      cb();
    };
  });
}

/* 四种 agent 任务（都走同一个 /api/agent，只是 todo 不同）：出提示词 / 按反馈重出 /
   核对归类（软件只按文件名猜角色，不准的靠 agent 复核）/ 评价反哺 skill（把评价与废因沉淀回规则） */
const ASK_LABEL = {prompt: "出提示词", feedback: "按反馈重出一版",
                   intake: "核对归类", learn: "评价反哺 skill"};

function askAgent(what, feedback) {
  ensureAgent(async () => {
    const r = await api("/api/agent", {what, feedback});
    if (r.error) return toast(r.error);
    const name = ASK_LABEL[what] || "出提示词";
    $("prog").hidden = false;
    const who = (S.agent && S.agent.label) ? "（" + S.agent.label + "）" : "";
    logLine("→ agent 开始干活：" + name + "…" + who);
    toast(`已叫 ${S.agent && S.agent.label || "agent"} 干「${name}」，预计 `
          + Math.round((r.eta || 180) / 60) + " 分钟"
          + (r.eta_n ? `（按本项目 ${r.eta_n} 次历史）` : ""));
    S.prog = {on: true, job: r.job, stage: 0, stageName: "正在叫 agent…", pct: 0,
              elapsed: 0, eta: r.eta || 180, done: false, lines: []};
    boardProg();
    watchAgent();
  });
}

function watchAgent() {
  if (S.agentTimer) return;
  const pst = $("pstages");
  pst.innerHTML = ["读技能 / 框架", "写提示词", "落即梦上传", "写回执"]
    .map((s, i) => `<span data-i="${i}">${s}</span>`).join("");
  const es = new EventSource("/api/progress");
  S.agentTimer = es;
  es.onmessage = (ev) => {
    let d; try { d = JSON.parse(ev.data); } catch (e) { return; }
    if (d.bye) { es.close(); S.agentTimer = null; return; }
    $("pstage").innerHTML = (d.running ? '<span class="spin"></span>' : "") + esc(d.stageName);
    $("pfill").style.width = (d.pct || 0) + "%";
    $("ptime").textContent = `已用 ${fmt(d.elapsed)} · ` +
      (d.done ? `用时 ${fmt(d.elapsed)}` : `预计还要 ${fmt(Math.max(0, d.eta - d.elapsed))}`);
    pst.querySelectorAll("span").forEach((sp) => {
      const i = +sp.dataset.i;
      sp.className = i < d.stage ? "done" : (i === d.stage ? "on" : "");
    });
    (d.lines || []).forEach((ln) => logLine(ln,
      /✅/.test(ln) ? "ok" : (/^!!/.test(ln) ? "bad" : "")));
    /* 指挥台那张卡里也显示进度：服务端每次只发新增的行，所以按 job 号累计（换任务就清空）。 */
    const prevLines = (S.prog && S.prog.job === d.job) ? (S.prog.lines || []) : [];
    S.prog = {on: true, job: d.job, stageName: d.stageName, stage: d.stage, pct: d.pct,
              elapsed: d.elapsed, eta: d.eta, done: d.done, ok: d.ok,
              lines: prevLines.concat(d.lines || []).slice(-100)};
    boardProg();
    if (d.done) {
      es.close(); S.agentTimer = null;
      $("pstage").innerHTML = d.ok ? "完成 · 提示词已写回" : "agent 没跑成";
      $("pfill").style.width = (d.ok ? 100 : d.pct) + "%";
      toast(d.ok ? "agent 回来了，提示词已刷新" : "agent 报错，看执行记录");
      loadState();
    }
  };
  es.onerror = () => { es.close(); S.agentTimer = null; };
}

const fmt = (s) => { s = Math.max(0, Math.round(s || 0));
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); };

async function receive(good) {
  const note = $("why").value.trim();
  if (!good && !note) return toast("收废片要写废因");
  const r0 = await api("/api/pick", {kind: "files"});
  if (r0.error) return toast(r0.error);
  if (!r0.paths || !r0.paths.length) return;
  const r = await api("/api/receive", {good, note, paths: r0.paths});
  if (!r.ok) return toast(r.error || "接收失败");
  logLine(`已接收 ${r.placed.length} 个到 ${good ? "成片" : "废片"}/`, "ok");
  toast(good ? "成片已收，去④栏打分" : "废片已收");
  $("why").value = "";
  await loadState();
}

async function submitFeedback(given) {
  const text = String(given == null ? $("fb").value : given).trim();
  if (!text) return toast("先写两句反馈");
  const r = await api("/api/feedback", {text, callAgent: false});
  if (!r.ok) return toast(r.error || "提交失败");
  $("fb").value = "";
  logLine(`已记入第 ${r.round} 轮反馈`);
  askAgent("feedback", text);
}

async function saveScore() {
  const r = await api("/api/score", {review: S.review, video: $("videoSel").value});
  if (!r.ok) return toast(r.error || "保存失败");
  logLine("评分已保存：" + r.file, "ok");
  toast("评分已保存");
  loadState();
}

function copyPrompt() {
  const ps = S.prompts || [];
  const body = ps.length ? (ps[ps.length - 1].text || "") : "";
  const ups = (S.uploads || []).map((u) => u.name).join("\n");
  const text = body + (ups ? "\n\n---- 要上传的素材 ----\n" + ups : "");
  if (!text) return toast("还没有提示词");
  navigator.clipboard.writeText(text).then(
    () => toast("提示词已复制（含上传清单）"),
    () => toast("复制失败，手动选中①栏文本"));
}

/* 「怎么用」不再是弹窗念一遍流程 —— 换成整页的流程指挥台（flow.html，见上面的 BOARD 段）。 */
async function reloadPrompts() {
  const pr = await api("/api/prompts");
  S.prompts = pr.prompts || []; S.uploads = pr.uploads || [];
  renderPrompts(); renderFlow(); toast("已重新读取提示词");
}

/* ── 拖拽：整栏虚线 + 投放区强化 + 落点文案（StoryVia 四态）──────────── */
function bindDrop() {
  const dz = $("dz"), col2 = $("col2"), col = col2.closest(".col");
  let depth = 0;
  // ②栏内部的"拖动排序"也走 HTML5 拖放：靠这个私有类型把它跟"投放文件"区分开
  const isMatDrag = (e) => {
    try { return [...((e.dataTransfer && e.dataTransfer.types) || [])]
      .includes("text/heronbo-mat"); } catch (err) { return false; }
  };
  const set = (on, extra) => {
    dz.classList.toggle("hot", on); col.classList.toggle("hot", on);
    dz.querySelector("b").textContent = on ? "松开即投放 → 自动归类" : "把素材拖到这里";
    if (extra !== undefined) $("dzHint").textContent = extra;
  };
  window.addEventListener("dragenter", (e) => {
    e.preventDefault();
    if (isMatDrag(e)) return;                 // 拖的是清单里的素材行，不是要投放文件
    depth++; set(true);
  });
  window.addEventListener("dragover", (e) => { e.preventDefault(); });
  window.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; set(false); } });
  window.addEventListener("drop", async (e) => {
    e.preventDefault(); depth = 0; set(false);
    if (isMatDrag(e)) return;                 // 排序落点由素材行自己处理
    const items = [...((e.dataTransfer && e.dataTransfer.items) || [])];
    const dirs = items.filter((it) => it.kind === "file"
      && it.webkitGetAsEntry && (it.webkitGetAsEntry() || {}).isDirectory);
    const files = [...((e.dataTransfer && e.dataTransfer.files) || [])];
    if (dirs.length && !files.length) {
      return toast("文件夹请用「选文件夹」按钮——浏览器不把文件夹路径交给网页");
    }
    if (!files.length) return toast("这次拖拽里没有可用的文件");
    toast(`正在投递 ${files.length} 个文件…`);
    let ok = 0;
    for (const f of files) { if (await uploadFile(f)) ok++; }
    if (ok) toast(`收下 ${ok} 个文件，点「建框架归类」落位`);
    else toast("投递失败，改用「选择素材」");
  });
}

/* ── 心跳：页面关了让服务自己退 ────────────────────────────────── */
setInterval(() => { fetch("/api/ping", {method: "POST"}).catch(() => {}); }, 3000);

/* ── 绑定 ─────────────────────────────────────────────────────────── */
$("btnRefresh").onclick = () => loadState();
$("btnLoadPrompts").onclick = () => reloadPrompts();
$("btnAgent").onclick = () => agentModal();
$("btnAsk").onclick = () => askAgent("prompt");
$("btnIntakeGo").onclick = () => intakeGo();
$("btnPick").onclick = () => pickFiles("files");
$("btnPickDir").onclick = () => pickFiles("dir");
$("btnGood").onclick = () => receive(true);
$("btnBad").onclick = () => receive(false);
$("btnSubmitFb").onclick = () => submitFeedback();
$("btnSave").onclick = () => saveScore();
$("btnReload").onclick = async () => {
  const rv = await api("/api/review");
  fillReview(rv.review);
  toast("已重新载入上次评分");
};
$("btnHelp").onclick = () => openBoard();
$("btnNewProj").onclick = () => newProjectModal();
$("btnAddMat").onclick = () => pickFiles("files");
$("btnSaveNeed").onclick = () => saveNeed();
$("need").addEventListener("blur", () => {           // 失焦自动存（改了才存）
  const cur = (S.state && S.state.need) || "";
  if ($("need").value.trim() !== cur.trim()) saveNeed();
});
$("btnCheckRole").onclick = () => askAgent("intake");
$("btnLearn").onclick = () => askAgent("learn");
$("btnTheme").onclick = () => themeModal();
$("btnClassic").onclick = async () => {
  const r = await api("/api/classic", {});
  if (!r.ok) return toast(r.error || "切不过来");
  toast(r.note || "已切到经典界面");
  logLine("已切到经典界面（另开的窗口）；本窗口可以直接关掉", "ok");
  $("nextText").innerHTML = "<b>已切到经典界面</b>：另开了一个窗口，本窗口随时可以关掉。";
};
$("note").oninput = () => { S.review.备注 = $("note").value; };

bindDrop();
BOARD.wrap = $("boardWrap");
BOARD.frame = $("boardFrame");
paintIcons();
try { applyTheme(localStorage.getItem("heronbo.theme") || "原版"); } catch (e) { applyTheme("原版"); }
loadState();
logLine("工作台已就绪");
