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
  const M = openModal(m, "主题");
  m.querySelectorAll(".arow").forEach((r) => {
    r.onclick = () => { applyTheme(r.dataset.t); M.close(); toast("主题已换成 " + r.dataset.t); };
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
  const sort = (S.ui || {}).projSort || "default";
  const shortTime = (v) => (v || "").replace(/^\d\d(\d\d)-(\d\d)-(\d\d) (\d\d:\d\d)$/, "$2-$3 $4");
  $("sampleList").innerHTML = sortedSamples().map((s) => {
    // 按时间排时把时间显出来（不然用户看不出"到底排没排"）；其它排序显示素材/成片数
    const meta = (sort === "time" || sort === "mtime")
      ? `${shortTime(s.createdAt) || "—"} · 素材 ${s.materials}`
      : `素材 ${s.materials} · 成片 ${s.videos}`;
    return `<div class="row ${s.dir === cur ? "on" : ""}" data-dir="${esc(s.dir)}"
        title="创建 ${esc(s.createdAt || "（未知）")} · 素材 ${s.materials} · 成片 ${s.videos}">
       <span class="grip" title="拖动改名次（会自动切成「自定义」排序）">⋮⋮</span>
       <span class="g">${esc(s.name)}</span>
       <span class="m">${meta}</span>
       <button class="ibtn sm" data-delproj="1" data-icon="x"
         title="删除这个项目（整个目录移到样本库根的 _已删除/，能搬回来）"></button></div>`;
  }).join("")
    || '<div class="meta">样本库里还没项目。把素材拖进②栏就能建，或点上面的「＋ 新建项目」。</div>';
  $("sampleList").querySelectorAll(".row").forEach((el) => {
    el.onclick = () => selectProject(el.dataset.dir);
  });
  bindSampleDrag();
  renderSampleSortBtn();
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
    <p class="meta">选法（从上到下，先满足先用）：<b>①</b> 你在界面里点过哪个就用哪个（下面的「✓ 当前使用」）；
      <b>②</b> 没点过 → <b>跟随你正开着的客户端</b>（WorkBuddy / ZCode…）；
      <b>③</b> 都没开 → 用"把本技能装在自己名下且命令行可用"的那个。
      <span style="color:var(--ok)">● 可用</span>　<span style="color:var(--bad)">● 不可用</span>　点一行即固定用它。</p>
    <div class="amod">${rows}</div>
    <div class="sessbox">
      <b>当前会话</b>
      ${a.session ? `<code>${esc((a.session || "").slice(0, 22))}…</code>
        ${a.sessionKB ? `<span class="meta">上下文约 ${a.sessionKB >= 1024
            ? (a.sessionKB / 1024).toFixed(1) + " MB" : a.sessionKB + " KB"}</span>` : ""}
        <button class="btn ghost sm" id="mnewsess"
          title="清掉这个会话记录：下一轮会开新对话（技能要重读一次，但上下文干净）">开新会话</button>`
      : '<span class="meta">还没有会话（下一轮会新建一个）</span>'}
      <div class="meta">续同一个会话 = 技能与项目上下文留在上下文里、带着缓存，更快；太长了就点「开新会话」。</div>
    </div>
    <p class="note" style="margin-top:4px">选择记在 <code>${esc(a.cfg || "—")}</code>（关掉工作台也在）。
      想直接写文件：<code>{"agent": "codex"}</code>；要接没适配的命令行：
      <code>{"cmd": ["命令", "{prompt}"], "cmd_mode": "text"}</code>。</p>
    <div style="text-align:right;margin-top:12px">
      ${a.chosen ? '<button class="btn ghost" id="mauto">恢复自动（跟随我开着的软件）</button>' : ""}
      <button class="btn" id="mclose">知道了</button></div>
    </div>`;
  const M = openModal(m, "agent 通道");
  m.querySelectorAll(".arow").forEach((row) => {
    row.onclick = async () => {
      if (row.dataset.ok !== "1") return toast("这个 agent 现在不可用");
      const r = await api("/api/agent/set", {key: row.dataset.k});
      if (!r.ok) return toast(r.why || "设置失败");
      S.agent = r.agent; renderAgent(S.agent); M.close();
      toast("已切换为 " + (S.agent.label || ""));
    };
  });
  const ns = m.querySelector("#mnewsess");
  if (ns) ns.onclick = async () => {
    const r = await api("/api/agent/session", {action: "new"});
    if (!r.ok) return toast(r.error || "清不了");
    M.close();
    await loadState();
    logLine("已清掉会话记录：" + (r.note || ""), "warn");
    toast(r.note || "下一轮开新对话");
  };
  const auto = m.querySelector("#mauto");
  if (auto) auto.onclick = async () => {
    const r = await api("/api/agent/set", {key: ""});
    if (!r.ok) return toast(r.why || "设置失败");
    S.agent = r.agent; renderAgent(S.agent); M.close();
    toast("已恢复自动：以后跟着你打开的那个软件走（当前 " + (S.agent.label || "") + "）");
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
  spinRefresh(600);                  // 点一次刷新，两个箭头转起来（至少 600ms 才看得见）
  const st = await api("/api/state");
  S.state = st;
  S.dims = st.dims || [];
  S.mats = st.materials || [];
  S.ui = st.ui || S.ui || {};
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
  if (r.healed) logLine("这个项目缺 框架.json（大概是建到一半被打断），已自动补齐骨架", "ok");
  await loadState();
}

/* ── ①栏项目列表：排序 + 拖动 ────────────────────────────────────────
   排序方式：默认（样本库扫描顺序）/ 名称 / 素材数 / 成片数 / 创建时间 / 自定义。
   **拖动即切到"自定义"**并把名次存进本机偏好（tools/workbench.local.json，gitignored）。 */
const SORTS = [["default", "默认（扫描顺序）"], ["time", "按创建时间（新→旧）"],
               ["mtime", "按最近改动（新→旧）"],
               ["mats", "按素材数（多→少）"], ["videos", "按成片数（多→少）"],
               ["name", "按名称（A→Z）"]];

function sortedSamples() {
  const list = (S.state.samples || []).slice();
  const ui = S.ui || {};
  const sort = ui.projSort || "default";
  if (sort === "custom") {
    const ord = ui.projOrder || [];
    const idx = new Map(ord.map((d, i) => [d, i]));
    list.sort((a, b) => (idx.has(a.dir) ? idx.get(a.dir) : 1e9)
                        - (idx.has(b.dir) ? idx.get(b.dir) : 1e9));
    return list;
  }
  if (sort === "name") list.sort((a, b) => a.name.localeCompare(b.name, "zh"));
  if (sort === "mats") list.sort((a, b) => (b.materials || 0) - (a.materials || 0));
  if (sort === "videos") list.sort((a, b) => (b.videos || 0) - (a.videos || 0));
  // 时间类：用服务端算好的 epoch（两种来源格式不同，比字符串会排错），空的沉底
  if (sort === "time") list.sort((a, b) => (b.createdTs || 0) - (a.createdTs || 0)
                                          || a.name.localeCompare(b.name, "zh"));
  if (sort === "mtime") list.sort((a, b) => (b.mtime || 0) - (a.mtime || 0));
  return list;
}

function renderSampleSortBtn() {
  const ui = S.ui || {};
  const sort = ui.projSort || "default";
  const hit = SORTS.find((x) => x[0] === sort);
  const b = $("btnSort");
  b.textContent = "排序 · " + (sort === "custom" ? "自定义" : (hit ? hit[1].replace(/（.*）/, "") : "默认"));
  b.title = "项目列表排序；拖动项目行也会自动切成「自定义」";
}

function sortMenu() {
  const ui = S.ui || {};
  const cur = ui.projSort || "default";
  const rows = SORTS.map(([k, label]) =>
    `<div class="arow ${k === cur ? "on" : ""}" data-s="${esc(k)}">
       <span class="an">${esc(label)}</span>
       <span class="aw">${k === "default" ? "样本库扫描出来的顺序" : ""}</span>
       <span class="ar">${k === cur ? "✓ 当前" : "用它"}</span></div>`).join("")
    + `<div class="arow ${cur === "custom" ? "on" : ""}" data-s="custom">
         <span class="an">自定义（拖动排的）</span>
         <span class="aw">${(ui.projOrder || []).length} 个已排名次</span>
         <span class="ar">${cur === "custom" ? "✓ 当前" : "用它"}</span></div>`;
  const m = document.createElement("div");
  m.className = "modal";
  m.innerHTML = `<div class="box"><h3>项目列表排序</h3>
    <p class="meta">选一种排序，或者直接在列表里拖动项目行（拖动会自动切到「自定义」并记住名次）。</p>
    <div class="amod">${rows}</div>
    <div style="text-align:right;margin-top:12px"><button class="btn ghost" id="mclose">关掉</button></div>
    </div>`;
  const M = openModal(m, "项目列表排序", () => $("btnSort").classList.remove("on"));
  $("btnSort").classList.add("on");          // 排序按钮上的小三脚跟着转（打开态）
  m.querySelectorAll(".arow").forEach((r) => {
    r.onclick = async () => {
      const res = await api("/api/ui", {projSort: r.dataset.s});
      if (!res.ok) return toast(res.error || "存不上");
      S.ui = res.ui; M.close();
      renderSamples(); renderSampleSortBtn();
      toast("排序已换成：" + r.querySelector(".an").textContent);
    };
  });
}

/* 项目行拖动：拖完把整个名次存下来（顺手切成自定义） */
function bindSampleDrag() {
  const box = $("sampleList");
  box.querySelectorAll(".row").forEach((row) => {
    if (!row.dataset.dir) return;
    const del = row.querySelector("[data-delproj]");
    if (del) del.onclick = (e) => { e.stopPropagation(); deleteProjectModal(row.dataset.dir); };
    row.setAttribute("draggable", "true");
    row.addEventListener("dragstart", (e) => {
      row.classList.add("drag");
      try {
        e.dataTransfer.setData("text/heronbo-proj", row.dataset.dir);
        e.dataTransfer.effectAllowed = "move";
      } catch (err) {}
    });
    row.addEventListener("dragend", () => {
      row.classList.remove("drag");
      box.querySelectorAll(".over,.over-after").forEach((x) =>
        x.classList.remove("over", "over-after"));
    });
    row.addEventListener("dragover", (e) => {
      if (row.classList.contains("drag")) return;
      e.preventDefault();
      const r = row.getBoundingClientRect();
      const after = (e.clientY || 0) > r.top + r.height / 2;
      row.classList.toggle("over", !after);
      row.classList.toggle("over-after", after);
    });
    row.addEventListener("dragleave", () => row.classList.remove("over", "over-after"));
    row.addEventListener("drop", async (e) => {
      const dragged = box.querySelector(".row.drag");
      e.preventDefault(); e.stopPropagation();
      const r = row.getBoundingClientRect();
      const after = (e.clientY || 0) > r.top + r.height / 2;
      row.classList.remove("over", "over-after");
      if (!dragged || dragged === row) return;
      if (after) box.insertBefore(dragged, row.nextSibling);
      else box.insertBefore(dragged, row);
      const order = [...box.querySelectorAll(".row[data-dir]")].map((x) => x.dataset.dir);
      const res = await api("/api/ui", {projSort: "custom", projOrder: order});
      if (!res.ok) return toast(res.error || "名次没存上");
      S.ui = res.ui;
      renderSampleSortBtn();
      toast("名次已存（排序切成「自定义」）");
    });
  });
}

/* ── 面板调宽窄 + 框调高矮（2026-09-16 用户要求："这些面板调节大小还有面板里面的对话框"）
   面板＝四栏：三条分隔条各自控制左边那一栏的宽度（第三条控制④栏，方向相反）。
   框＝面板里的内容框（提示词正文 / 即梦上传 / 素材清单 / 执行记录）：底部抓手拖高矮。
   尺寸都存 localStorage（本机偏好），双击抓手＝恢复默认。 */
const COL_MIN = {1: 170, 2: 200, 4: 240};      // 各栏最小宽度
const MID_MIN = 320;                            // ③栏是弹性列，给它留够

function applyCols(v) {
  const r = document.documentElement.style;
  if (v && v.length === 3) {
    r.setProperty("--col1", v[0] + "px");
    r.setProperty("--col2", v[1] + "px");
    r.setProperty("--col4", v[2] + "px");
  }
}

function colsNow() {
  const cs = getComputedStyle(document.documentElement);
  return [1, 2, 4].map((i) => Math.round(parseFloat(cs.getPropertyValue("--col" + i)) || 0));
}

function saveCols(v) {
  try { localStorage.setItem("heronbo.cols", JSON.stringify(v)); } catch (e) {}
}

function makeColumnsResizable() {
  try {
    const saved = JSON.parse(localStorage.getItem("heronbo.cols") || "null");
    if (saved && saved.length === 3) applyCols(saved);
  } catch (e) {}
  document.querySelectorAll(".split").forEach((sp) => {
    const which = +sp.dataset.col;               // 1 / 2 / 4
    let dragging = false, sx = 0, v0 = [];
    sp.addEventListener("pointerdown", (e) => {
      dragging = true; sp.classList.add("on");
      sx = e.clientX; v0 = colsNow();
      document.body.style.cursor = "col-resize";
      try { sp.setPointerCapture(e.pointerId); } catch (err) {}
      e.preventDefault();
    });
    sp.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      const dx = e.clientX - sx;
      const v = v0.slice();
      if (which === 1) v[0] = v0[0] + dx;
      if (which === 2) v[1] = v0[1] + dx;
      if (which === 4) v[2] = v0[2] - dx;         // ③栏右侧那条：往左拖＝④栏变宽
      v[0] = Math.max(COL_MIN[1], v[0]);
      v[1] = Math.max(COL_MIN[2], v[1]);
      v[2] = Math.max(COL_MIN[4], v[2]);
      // 别把③栏（弹性列）挤没了
      const work = document.getElementById("work");
      const avail = (work ? work.clientWidth : innerWidth) - 15;
      const over = v[0] + v[1] + v[2] - (avail - MID_MIN);
      if (over > 0) {                             // 超了就压缩正在拖的那一栏
        if (which === 1) v[0] -= over;
        else if (which === 2) v[1] -= over;
        else v[2] -= over;
        if (which === 1) v[0] = Math.max(COL_MIN[1], v[0]);
        if (which === 2) v[1] = Math.max(COL_MIN[2], v[1]);
        if (which === 4) v[2] = Math.max(COL_MIN[4], v[2]);
      }
      applyCols(v);
    });
    const stop = () => {
      if (!dragging) return;
      dragging = false; sp.classList.remove("on"); document.body.style.cursor = "";
      saveCols(colsNow());
      Object.values(S.rh || {}).forEach((r) => r && r.paint && r.paint());   // 滑块要跟着重算
    };
    sp.addEventListener("pointerup", stop);
    sp.addEventListener("pointercancel", stop);
    sp.addEventListener("dblclick", () => {
      document.documentElement.style.removeProperty("--col1");
      document.documentElement.style.removeProperty("--col2");
      document.documentElement.style.removeProperty("--col4");
      try { localStorage.removeItem("heronbo.cols"); } catch (e) {}
      toast("栏宽已恢复默认");
      Object.values(S.rh || {}).forEach((r) => r && r.paint && r.paint());
    });
  });
}

/* 面板里的框：底部抓手拖高矮 */
function makeVResizable(el, key, minH, maxH) {
  if (!el) return;
  const k = "heronbo.vh." + key;
  const defH = el.getBoundingClientRect().height;
  const setH = (h) => { el.style.height = h + "px"; el.classList.add("sized"); };
  try {
    const h = parseFloat(localStorage.getItem(k) || "0");
    if (h) setH(h);            // 拖过才把内层填满（没拖过保持内层自己的 max-height）
  } catch (e) {}
  const bar = document.createElement("span");
  bar.className = "vrz";
  bar.title = "拖动改高度（双击恢复默认）";
  el.appendChild(bar);
  let dragging = false, sy = 0, h0 = 0;
  bar.addEventListener("pointerdown", (e) => {
    dragging = true; bar.classList.add("on");
    h0 = el.getBoundingClientRect().height; sy = e.clientY;
    el.style.maxHeight = "none"; el.classList.add("sized");
    try { bar.setPointerCapture(e.pointerId); } catch (err) {}
    e.preventDefault(); e.stopPropagation();
  });
  bar.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const h = Math.max(minH || 60, Math.min(maxH || innerHeight - 200, h0 + e.clientY - sy));
    el.style.height = h + "px";
  });
  const stop = () => {
    if (!dragging) return;
    dragging = false; bar.classList.remove("on");
    try { localStorage.setItem(k, String(Math.round(el.getBoundingClientRect().height))); } catch (e) {}
  };
  bar.addEventListener("pointerup", stop);
  bar.addEventListener("pointercancel", stop);
  bar.addEventListener("dblclick", (e) => {
    e.stopPropagation();
    el.style.height = ""; el.style.maxHeight = ""; el.classList.remove("sized");
    try { localStorage.removeItem(k); } catch (err) {}
    void defH;
    toast("这个框恢复了默认高度");
  });
}

/* 刷新按钮：**指上去不变**（用户要求），点一下才让两个箭头转起来（至少转 600ms 看得见） */
function spinRefresh(ms) {
  const b = $("btnRefresh");
  if (!b) return;
  b.classList.add("spinning");
  clearTimeout(spinRefresh._t);
  spinRefresh._t = setTimeout(() => b.classList.remove("spinning"), ms || 600);
}

/* ── 弹窗通用：可调大小 + 位置记住 ────────────────────────────────────
   用户要求"每个对话框窗口都能调大小"。做法统一在一个函数里，所有弹窗都调它：
   右下角给个抓手，拖它就改宽高（有下限/上限），尺寸按弹窗标题存进 localStorage；
   双击抓手＝恢复默认大小。 */
function modalKey(box) {
  const h = box.querySelector("h3");
  return (h ? h.textContent : "modal").trim().slice(0, 20);
}

function makeResizable(m, key) {
  const box = m.querySelector(".box");
  if (!box) return;
  const k = "heronbo.box." + (key || modalKey(box));
  try {
    const saved = JSON.parse(localStorage.getItem(k) || "null");
    if (saved && saved.w) {
      box.style.width = Math.min(saved.w, innerWidth - 40) + "px";
      box.style.height = Math.min(saved.h || 0, innerHeight - 40) + "px";
    }
  } catch (e) {}
  const rz = document.createElement("span");
  rz.className = "rz";
  rz.title = "拖我调大小（双击恢复默认）";
  box.appendChild(rz);
  let dragging = false, sx = 0, sy = 0, w0 = 0, h0 = 0;
  const rect0 = () => box.getBoundingClientRect();
  rz.addEventListener("pointerdown", (e) => {
    dragging = true; box.classList.add("rz-drag");
    const r = rect0(); w0 = r.width; h0 = r.height; sx = e.clientX; sy = e.clientY;
    box.style.maxWidth = "none"; box.style.maxHeight = "none";
    try { rz.setPointerCapture(e.pointerId); } catch (err) {}
    e.preventDefault(); e.stopPropagation();
  });
  rz.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const w = Math.max(320, Math.min(innerWidth - 24, w0 + e.clientX - sx));
    const h = Math.max(160, Math.min(innerHeight - 24, h0 + e.clientY - sy));
    box.style.width = w + "px"; box.style.height = h + "px";
  });
  const stop = () => {
    if (!dragging) return;
    dragging = false; box.classList.remove("rz-drag");
    const r = rect0();
    try { localStorage.setItem(k, JSON.stringify({w: Math.round(r.width), h: Math.round(r.height)})); } catch (e) {}
  };
  rz.addEventListener("pointerup", stop);
  rz.addEventListener("pointercancel", stop);
  rz.addEventListener("dblclick", () => {
    box.style.width = ""; box.style.height = "";
    try { localStorage.removeItem(k); } catch (e) {}
    toast("弹窗大小已恢复默认");
  });
}

/* 所有弹窗都走这里：加动画类 + 可调大小 */
function openModal(m, key, onClose) {
  document.body.appendChild(m);
  makeResizable(m, key);
  requestAnimationFrame(() => m.classList.add("in"));       // 淡入 + 轻微放大（动效）
  const close = () => { m.classList.add("out");
    setTimeout(() => { m.remove(); if (onClose) onClose(); }, 140); };
  const btn = m.querySelector("#mclose");
  if (btn) btn.onclick = close;
  m.onclick = (e) => { if (e.target === m) close(); };
  return {close, m};
}
async function recordsModal() {
  const r = await api("/api/records");
  if (!r.ok) return toast(r.error || "读不到记录");
  const rows = (r.records || []).map((x) =>
    `<div class="arow" data-n="${esc(x.name)}">
       <span class="an">${esc(x.name.replace(/\.jsonl$/, "").replace(/-/, " ").replace(/-/, "  "))}</span>
       <span class="aw">${size(x.size)}</span>
       <span class="ar">点开看</span></div>`).join("")
    || '<div class="meta">这个项目还没跑过 agent（跑一轮就会出现在这里）</div>';
  const m = document.createElement("div");
  m.className = "modal";
  m.innerHTML = `<div class="box" style="max-width:860px">
    <h3>agent 对话记录</h3>
    <p class="meta">每一轮 agent 干活，工作台都自己记一份（时间线：它在读什么、写了什么、最后说了什么）。
      <b>桌面端里看不到这些</b>，所以这里存一份。</p>
    ${r.transcript ? `<p class="note" style="margin:8px 0"><b>agent 自己的会话正文：</b>
      <code>${esc(r.transcript)}</code>
      <button class="btn ghost sm" id="rcCopy">复制路径</button>
      <button class="btn ghost sm" id="rcOpenT">打开它</button></p>`
    : `<p class="note" style="margin:8px 0">没找到 agent 自己那份会话正文文件（<b>WorkBuddy 的无头跑本机不一定落盘</b>，
      我全盘搜过；ZCode 会落在 <code>~/.zcode/cli/rollout/</code> 下）。所以这里这份「工作台自己记的」
      通常就是最全的——再加上项目里 <code>_会话/回执.jsonl</code>（agent 自己写的回执）。</p>`}
    <div class="amod">${rows}</div>
    <div class="recbox" id="recBox" hidden></div>
    <div style="text-align:right;margin-top:12px">
      <button class="btn ghost" id="rcOpenDir">打开记录文件夹</button>
      <button class="btn" id="mclose">知道了</button></div></div>`;
  const {close: closeRec} = openModal(m, "agent 对话记录");
  const cpx = m.querySelector("#rcCopy");
  if (cp) cp.onclick = () => navigator.clipboard.writeText(r.transcript).then(
    () => toast("路径已复制"), () => toast("复制失败"));
  const ot = m.querySelector("#rcOpenT");
  if (ot) ot.onclick = () => api("/api/records/open", {path: r.transcript})
    .then((x) => !x.ok && toast("打不开这份；用「打开记录文件夹」"));
  m.querySelector("#rcOpenDir").onclick = async () => {
    const x = await api("/api/records/open", {});
    toast(x.ok ? "已在资源管理器里打开" : (x.error || "打不开"));
  };
  m.querySelectorAll(".arow").forEach((row) => {
    row.onclick = async () => {
      const d = await api("/api/records/read?name=" + encodeURIComponent(row.dataset.n));
      const box = m.querySelector("#recBox");
      box.hidden = false;
      if (!d.ok) { box.innerHTML = `<div class="meta">${esc(d.error || "读不了")}</div>`; return; }
      box.innerHTML = `<div class="seclab">${esc(d.name)}</div>`
        + (d.lines || []).map((o) => {
          const k = o.kind || "";
          const t = esc(o.text || (k === "start" ? "（开始：" + (o.agent || "") + "）"
                                 : k === "end" ? "（结束：ok=" + o.ok + "，用时 " + o.seconds + "s）" : ""));
          return `<div class="rl ${k}">${o.at ? '<i>' + esc(o.at) + '</i> ' : ""}${t}</div>`;
        }).join("");
    };
  });
}

/* 删除项目：**不真删**——整个目录移到 <样本库根>/_已删除/，确认弹窗把这点写清楚 */
function deleteProjectModal(dir) {
  const s = (S.state.samples || []).find((x) => x.dir === dir) || {};
  const name = s.name || dir.split(/[\/]/).pop();
  const root = (S.state.root || "").replace(/[\/]+$/, "");
  const m = document.createElement("div");
  m.className = "modal";
  m.innerHTML = `<div class="box"><h3>删除项目</h3>
    <p class="meta">把「<b>${esc(name)}</b>」从样本库里拿掉（里面：素材 ${s.materials || 0} ·
      成片 ${s.videos || 0} · 评分 ${s.reviews || 0}）。</p>
    <p class="note"><b>不是真删</b>：整个项目目录会被搬到
      <code>${esc(root)}${root ? "\\" : ""}_已删除\${esc(name)}-时间戳\</code>，
      在资源管理器里搬回来就恢复。</p>
    <div style="text-align:right;margin-top:12px">
      <button class="btn ghost" id="dpCancel">取消</button>
      <button class="btn danger" id="dpOk">移到 _已删除</button></div></div>`;
  const {close} = openModal(m, "删除项目");
  m.querySelector("#dpCancel").onclick = close;
  m.querySelector("#dpOk").onclick = async () => {
    const r = await api("/api/project/delete", {dir});
    if (!r.ok) return toast(r.error || "删除失败");
    logLine("已把项目移走：" + name + " → " + r.movedTo, "warn");
    toast("已移到 _已删除/（能搬回来）");
    close();
    await loadState();
  };
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
  const {close} = openModal(m, "新建项目");
  m.querySelector("#npCancel").onclick = close;
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
  // 没素材时也照发：项目还没有 框架.json 的话，服务端会把框架建出来（用户："新建项目了点击框架"）
  if (!S.pending.length && !(S.state.project && S.state.project.dir)) {
    return toast("先把素材拖进②栏，或先在①栏建个项目");
  }
  $("btnIntakeGo").disabled = true;
  const r = await api("/api/intake/go", {});
  $("btnIntakeGo").disabled = false;
  if (!r.ok) return toast(r.error || "建框架失败");
  S.pending = [];
  if (r.built) {                       // 没素材，但把框架补出来了
    logLine("已建框架（还没有素材）：" + (r.project ? r.project.name : ""), "ok");
    toast(r.note || "框架已建好，把素材拖进②栏再点一次");
  } else {
    logLine("已建框架并归类：" + (r.project ? r.project.name : ""), "ok");
    toast("框架已建好，素材归类完成");
  }
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
  if (a.chosen) return cb();                                  // 你在界面里选过 → 就按你选的
  // 你正开着某个客户端 → 自动跟随它，不打断你（2026-09-16 用户要的"跟着软件自动切"）
  if ((a.running || []).length && a.picked) {
    logLine("agent 自动跟随你正开着的 " + (a.label || a.picked));
    return cb();
  }
  if (use.length === 1) return cb();
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
  const M = openModal(m, "这次用哪个 agent");
  m.querySelectorAll("button[data-k]").forEach((b) => {
    b.onclick = async () => {
      const r = await api("/api/agent/set", {key: b.dataset.k});
      if (!r.ok) return toast(r.why || "设置失败");
      S.agent = r.agent;
      renderAgent(S.agent);
      M.close();
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
    $("pstage").innerHTML = (d.running ? '<span class="spin"></span>' : "") + esc(d.stageName)
      + (d.quiet ? '<span class="quiet">· 这条 agent 通道不吐过程输出，进度按时间估</span>' : "");
    $("pfill").style.width = (d.pct || 0) + "%";
    // 时间行："到点了"也不能显示"预计还要 0:00"（看着像卡死）——如实说超出预计、还在跑
    const left = (d.eta || 0) - (d.elapsed || 0);
    $("ptime").textContent = d.done
      ? `用时 ${fmt(d.elapsed)}`
      : `已用 ${fmt(d.elapsed)} · ` + (left > 0 ? `预计还要 ${fmt(left)}` : `已超出预计（还在跑）`);
    const over = !d.done && left <= 0;
    $("pfill").classList.toggle("alive", !d.done && (over || d.quiet));
    $("btnStop").hidden = !!d.done;
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
$("btnSort").onclick = () => sortMenu();
$("btnRecords").onclick = () => recordsModal();
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
$("btnStop").onclick = async () => {
  const r = await api("/api/agent/stop", {});
  if (!r.ok) return toast(r.error || "现在没有在跑的任务");
  logLine("已请求停止这一轮 agent", "warn");
  toast("已停止（这一轮的结果不算数，可以重跑）");
};
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
makeColumnsResizable();
makeVResizable($("promptWrap"), "prompt", 120, 760);     // ③栏 提示词正文（抓手挂外层，重画不冲掉）
makeVResizable($("logWrap"), "log", 80, 520);            // ③栏 执行记录
makeVResizable($("upBox"), "up", 50, 400);               // ③栏 即梦上传清单
makeVResizable($("matBox"), "mat", 80, 700);             // ②栏 素材清单
BOARD.wrap = $("boardWrap");
BOARD.frame = $("boardFrame");
paintIcons();
try { applyTheme(localStorage.getItem("heronbo.theme") || "原版"); } catch (e) { applyTheme("原版"); }
loadState();
logLine("工作台已就绪");
