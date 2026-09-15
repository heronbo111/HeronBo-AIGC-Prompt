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
}

/* 图标按钮：栏标题那排"小动作"用图标（主按钮仍带文字——一个按钮区最多一个主按钮） */
const ICON = {
  refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 3v6h-6"/></svg>',
  reload: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 12a8 8 0 0 1 13.7-5.7L20 8"/><path d="M20 3v5h-5"/><path d="M20 12a8 8 0 0 1-13.7 5.7L4 16"/><path d="M4 21v-5h5"/></svg>',
  copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>',
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
           rh: {}, agent: null};

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

function renderFlow() {
  const cur = currentStep().i;
  $("flow").innerHTML = STEPS.map((s, i) => {
    const cls = i < cur ? "done" : (i === cur ? "now" : "");
    const no = i < cur ? "✓" : String(i + 1);
    return `<div class="step ${cls}"><span class="no">${no}</span>${s.n}` +
           `<span class="who">${s.who}</span></div>` +
           (i < STEPS.length - 1 ? '<span class="sep">›</span>' : "");
  }).join("");
  const c = currentStep();
  const who = (c.i === 1 && S.agent && S.agent.label) ? `（用 ${esc(S.agent.label)}）` : "";
  $("nextText").innerHTML = `<b>${STEPS[c.i].n}</b>：${esc(c.why)}${who}`;
  const b = $("nextBtn");
  b.disabled = false;
  b.textContent = ["建框架归类", "出提示词", "复制提示词", "收成片", "去打分"][c.i];
  b.onclick = () => {
    if (c.i === 0) return intakeGo();
    if (c.i === 1) return askAgent("prompt");
    if (c.i === 2) return copyPrompt();
    if (c.i === 3) return receive(true);
    document.querySelector("#scoreCol").scrollIntoView({behavior: "smooth"});
  };
}

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
    const tag = r.host ? '<span class="who" style="border-color:#cfe0ff;background:var(--accent-soft);color:var(--accent)">装了本技能</span>' : "";
    const st = r.ok ? '<span style="color:var(--ok)">可用</span>'
                    : `<span class="meta">${esc(r.why)}</span>`;
    const now = r.key === a.picked;
    const btn = (r.ok && !now)
      ? `<button class="btn ghost sm" data-k="${esc(r.key)}">换成这个</button>`
      : (now ? '<span class="meta">当前</span>' : "");
    return `<div class="step"><b style="flex:0 0 96px">${esc(r.label)}</b>
      <span style="flex:1">${tag} ${st}</span>${btn}</div>`;
  }).join("");
  const m = document.createElement("div");
  m.className = "modal";
  m.innerHTML = `<div class="box"><h3>agent 通道</h3>
    <p class="meta">这个工作台跟着 skill 走：谁把本技能装在自己名下、且命令行可用，就用谁。</p>
    <div class="step"><b style="flex:0 0 96px">当前</b><span>${esc(a.label || "无")}
      —— ${esc(a.ok ? (a.why || "") : (a.why || "没找到可用的 agent"))}</span></div>
    ${rows}
    <p class="note" style="margin-top:12px">
      选择记在 <code>tools\agent_bridge.local.json</code>。要接没适配的 CLI（比如以后
      ZCode、DSH 提供了无头入口），在那份 json 里写
      <code>{"cmd": ["你的命令", "{prompt}"], "cmd_mode": "text"}</code>（{prompt}/{cwd} 是占位符）。</p>
    <div style="text-align:right;margin-top:12px">
      ${a.chosen ? '<button class="btn ghost" id="mauto">恢复自动挑选</button>' : ""}
      <button class="btn" id="mclose">知道了</button></div>
    </div>`;
  document.body.appendChild(m);
  m.querySelector("#mclose").onclick = () => m.remove();
  m.onclick = (e) => { if (e.target === m) m.remove(); };
  m.querySelectorAll("button[data-k]").forEach((b) => {
    b.onclick = async () => {
      const r = await api("/api/agent/set", {key: b.dataset.k});
      if (!r.ok) return toast(r.why || "设置失败");
      S.agent = r.agent; renderAgent(S.agent); m.remove();
      toast("已换成 " + (S.agent.label || ""));
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
  // ②栏已归类清单
  const f = p ? (p.folders || {}) : {};
  const mats = []
    .concat((f["文案"] || []).map((x) => ["文案", x]))
    .concat((f["素材"] || []).map((x) => ["素材", x]))
    .concat((f["即梦上传"] || []).map((x) => ["即梦上传", x]));
  $("matList").innerHTML = mats.length ? mats.map(([k, x]) =>
    `<div class="row plain"><span class="m" style="flex:0 0 62px">${k}</span>
      <span class="g">${esc(x.name)}</span><span class="m">${size(x.size)}</span></div>`).join("")
    : '<div class="meta">还没有归类好的素材</div>';
  // ④栏成片下拉
  const vids = (f["成片"] || []).map((x) => x.name);
  $("videoSel").innerHTML = (vids.length ? vids : ["（无成片）"])
    .map((v) => `<option>${esc(v)}</option>`).join("");
}

function renderPending() {
  $("pendLab").textContent = S.pending.length
    ? `待投放 · 角色识别（${S.pending.length}）` : "待投放 · 角色识别";
  $("pendList").innerHTML = S.pending.length ? S.pending.map((p, i) =>
    `<div class="row plain"><span class="g">${esc(p.split(/[\\/]/).pop())}</span>
      <span class="m">${esc((p.split(/[\\/]/).slice(-2, -1)[0] || ""))}</span>
      <button class="btn ghost sm" data-i="${i}">移除</button></div>`).join("")
    : '<div class="meta">把素材拖进上面的框，或点「选择素材」</div>';
  $("pendList").querySelectorAll("button").forEach((b) => {
    b.onclick = () => { S.pending.splice(+b.dataset.i, 1); renderPending(); renderFlow(); };
  });
}

function renderPrompts() {
  const ps = S.prompts || [];
  $("promptBox").textContent = ps.length
    ? ps.map((p) => `【${p.ver || "v"}】\n${p.text || ""}`).join("\n\n")
    : "（还没有提示词。先在②栏丢素材 → 建框架归类 → 点右上「出提示词」。）";
  $("promptBox").scrollTop = 0;                 // 重新读取后回到开头，别停在半截
  const ups = S.uploads || [];
  $("upList").innerHTML = ups.length ? ups.map((u) =>
    `<div class="row plain"><span class="g">${esc(u.name)}</span>
      <span class="m">${size(u.size)}</span></div>`).join("")
    : '<div class="meta">还没生成（agent 出提示词时会按引用编号把副本放进来）</div>';
  const st = S.state.project ? (S.state.project.state || {}) : {};
  $("stageLab").textContent = `· 阶段 ${st.stage || "—"} · 第 ${st.round || 0} 轮 · 待办 ${st.pending || 0}`;
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
  const pr = await api("/api/prompts");
  S.prompts = pr.prompts || []; S.uploads = pr.uploads || [];
  const rv = await api("/api/review");
  renderSamples(); renderProject(); renderPrompts();
  renderScoreForm(); fillReview(rv.review);
  renderFlow(); renderPending(); renderAgent(st.agent);
  paintIcons();
  $("buildStamp").textContent = `构建 ${st.build.stamp}`;
  if (!st.agent.ok) logLine("agent 通道不可用：" + st.agent.why, "bad");
}

async function selectProject(dir) {
  const r = await api("/api/project", {dir});
  if (!r.ok) return toast(r.error || "切项目失败");
  S.pending = [];
  logLine("已切到项目 " + dir.split(/[\\/]/).pop());
  await loadState();
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

function askAgent(what, feedback) {
  ensureAgent(async () => {
    const r = await api("/api/agent", {what, feedback});
    if (r.error) return toast(r.error);
    $("prog").hidden = false;
    const who = (S.agent && S.agent.label) ? "（" + S.agent.label + "）" : "";
    logLine(what === "feedback" ? "→ 正在按反馈重出一版…" + who
                               : "→ 正在叫 agent 出提示词…" + who);
    toast(`已叫 ${S.agent && S.agent.label || "agent"} 出提示词，预计 `
          + Math.round((r.eta || 180) / 60) + " 分钟"
          + (r.eta_n ? `（按本项目 ${r.eta_n} 次历史）` : ""));
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

async function submitFeedback() {
  const text = $("fb").value.trim();
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

function helpModal() {
  const rows = [
    ["① 丢素材", "把文案/形象图/音频/原片拖进②栏 → 「建框架归类」；软件会判角色并回报去向。"],
    ["② 出提示词", "点③栏右上「出提示词」，agent 读技能与框架 → 写提示词与即梦上传副本 → 写回执。"],
    ["③ 去平台生成", "复制提示词 → 上传清单里列的素材（没写进提示词的别传）→ 手选 9:16 → 生成。"],
    ["④ 收成片", "成片拖回②栏或点「收成片」；废片点「收废片」并写废因。"],
    ["⑤ 打分反馈", "④栏逐项打分 → 保存。想改就写「本轮反馈」→ 提交，agent 按它再出一版。"],
  ];
  const m = document.createElement("div");
  m.className = "modal";
  m.innerHTML = `<div class="box"><h3>这个工作台怎么用</h3>
    <p class="meta">绿色=agent 干，蓝色=你干，灰色=软件干。四栏从左到右就是流程顺序。</p>
    ${rows.map(([a, b]) => `<div class="step"><b style="flex:0 0 96px">${a}</b>
      <span>${b}</span></div>`).join("")}
    <p class="note" style="margin-top:12px">最常翻车的两件事：画幅没手选 9:16、素材没传全。</p>
    <div style="text-align:right;margin-top:12px"><button class="btn" id="mclose">知道了</button></div>
    </div>`;
  document.body.appendChild(m);
  m.querySelector("#mclose").onclick = () => m.remove();
  m.onclick = (e) => { if (e.target === m) m.remove(); };
}

/* ── 拖拽：整栏虚线 + 投放区强化 + 落点文案（StoryVia 四态）──────────── */
function bindDrop() {
  const dz = $("dz"), col2 = $("col2"), col = col2.closest(".col");
  let depth = 0;
  const set = (on, extra) => {
    dz.classList.toggle("hot", on); col.classList.toggle("hot", on);
    dz.querySelector("b").textContent = on ? "松开即投放 → 自动归类" : "把素材拖到这里";
    if (extra !== undefined) $("dzHint").textContent = extra;
  };
  window.addEventListener("dragenter", (e) => { e.preventDefault(); depth++; set(true); });
  window.addEventListener("dragover", (e) => { e.preventDefault(); });
  window.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; set(false); } });
  window.addEventListener("drop", async (e) => {
    e.preventDefault(); depth = 0; set(false);
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
$("btnLoadPrompts").onclick = async () => {
  const pr = await api("/api/prompts");
  S.prompts = pr.prompts || []; S.uploads = pr.uploads || [];
  renderPrompts(); renderFlow(); toast("已重新读取提示词");
};
$("btnCopy").onclick = () => copyPrompt();
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
$("btnHelp").onclick = () => helpModal();
$("btnClassic").onclick = () => {
  fetch("/api/classic", {method: "POST"}).catch(() => {});
  toast("经典界面要重新启动：用 score-tool.exe --classic");
};
$("note").oninput = () => { S.review.备注 = $("note").value; };

bindDrop();
paintIcons();
loadState();
logLine("工作台已就绪");
