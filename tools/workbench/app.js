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
function logLine(text, cls) {
  const el = $("log");
  el.insertAdjacentHTML("beforeend",
    `<div class="${cls || ""}">${esc(text)}</div>`);
  el.scrollTop = el.scrollHeight;
}

/* ── 状态 ─────────────────────────────────────────────────────────── */
const S = {state: null, pending: [], review: {}, dims: [], video: "", agentTimer: null};

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
  $("nextText").innerHTML = `<b>${STEPS[c.i].n}</b>：${esc(c.why)}`;
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
  $("rootPath").textContent = st.root || "（未配置样本库根）";
  const cur = st.project ? st.project.dir : "";
  $("sampleList").innerHTML = (st.samples || []).map((s) =>
    `<div class="row ${s.dir === cur ? "on" : ""}" data-dir="${esc(s.dir)}">
       <span class="g">${esc(s.name)}</span>
       <span class="m">素材 ${s.materials} · 成片 ${s.videos} · 评分 ${s.reviews}</span></div>`).join("")
    || '<div class="meta">样本库里还没项目。把素材拖进②栏就能建。</div>';
  $("sampleList").querySelectorAll(".row").forEach((el) => {
    el.onclick = () => selectProject(el.dataset.dir);
  });
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
  const ups = S.uploads || [];
  $("upList").innerHTML = ups.length ? ups.map((u) =>
    `<div class="row plain"><span class="g">${esc(u.name)}</span>
      <span class="m">${size(u.size)}</span></div>`).join("")
    : '<div class="meta">还没生成（agent 出提示词时会按引用编号把副本放进来）</div>';
  const st = S.state.project ? (S.state.project.state || {}) : {};
  $("stageLab").textContent = `阶段 ${st.stage || "—"} · 第 ${st.round || 0} 轮 · 待办 ${st.pending || 0}`;
}

function renderScoreForm() {
  const st = S.state;
  $("dimBox").innerHTML = (st.dims || []).map((d) => {
    const colors = (d.options || []).map((o, i, a) =>
      i === 0 ? "g" : (i === a.length - 1 ? "r" : "y"));
    return `<div class="fld"><label title="${esc(d.hint || "")}">${esc(d.key)}</label>
      <span class="seg" data-key="${esc(d.key)}">` +
      (d.options || []).map((o, i) =>
        `<button data-v="${esc(o)}" data-c="${colors[i]}">${esc(o)}</button>`).join("") +
      "</span></div>";
  }).join("");
  $("forbidBox").innerHTML = (st.forbid || []).map((k) =>
    `<span class="sw" data-key="${esc(k)}"><i></i>${esc(k)}</span>`).join("");
  $("conclBox").innerHTML = (st.concl || []).map((o, i) =>
    `<button data-v="${esc(o)}" data-c="${["g", "y", "r"][i] || ""}">${esc(o)}</button>`).join("");
  $("stars").innerHTML = [1, 2, 3, 4, 5].map((i) =>
    `<span data-v="${i}">★</span>`).join("");
  // 事件
  $("dimBox").querySelectorAll(".seg").forEach((seg) => {
    seg.querySelectorAll("button").forEach((b) => {
      b.onclick = () => {
        seg.querySelectorAll("button").forEach((x) => x.classList.remove("on"));
        b.classList.add("on", b.dataset.c);
        S.review.六维[seg.dataset.key] = b.dataset.v;
      };
    });
  });
  $("forbidBox").querySelectorAll(".sw").forEach((sw) => {
    sw.onclick = () => {
      sw.classList.toggle("on");
      S.review.违禁项[sw.dataset.key] = sw.classList.contains("on")
        ? st.forbid_on || "有" : st.forbid_default || "无";
    };
  });
  $("conclBox").querySelectorAll("button").forEach((b) => {
    b.onclick = () => {
      $("conclBox").querySelectorAll("button").forEach((x) => x.classList.remove("on"));
      b.classList.add("on", b.dataset.c);
      S.review.结论 = b.dataset.v;
    };
  });
  $("stars").querySelectorAll("span").forEach((sp) => {
    sp.onclick = () => { S.review.整体评分 = +sp.dataset.v; paintStars(); };
  });
  paintStars();
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
  $("dimBox").querySelectorAll(".seg").forEach((seg) => {
    const v = S.review.六维[seg.dataset.key];
    seg.querySelectorAll("button").forEach((b) => {
      b.classList.toggle("on", b.dataset.v === v);
      if (b.dataset.v === v) b.classList.add(b.dataset.c);
      else b.classList.remove("g", "y", "r");
    });
  });
  $("forbidBox").querySelectorAll(".sw").forEach((sw) => {
    const on = (S.review.违禁项[sw.dataset.key] || "无") === "有";
    sw.classList.toggle("on", on);
  });
  $("conclBox").querySelectorAll("button").forEach((b) => {
    b.classList.toggle("on", b.dataset.v === S.review.结论);
    if (b.dataset.v === S.review.结论) b.classList.add(b.dataset.c);
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
  renderFlow(); renderPending();
  $("buildStamp").textContent = `构建 ${st.build.stamp}` + (st.agent.ok ? "" : " · 通道不可用");
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
  try {
    const r = await fetch("/api/upload", {
      method: "POST",
      headers: {"X-File-Name": encodeURIComponent(file.name)},
      body: file.stream ? file.stream() : file,
    });
    const j = await r.json();
    if (j.ok) { S.pending = j.pending || []; renderPending(); renderFlow(); }
    return j.ok;
  } catch (e) { return false; }
}

async function askAgent(what, feedback) {
  const r = await api("/api/agent", {what, feedback});
  if (r.error) return toast(r.error);
  $("prog").hidden = false;
  logLine(what === "feedback" ? "→ 正在按反馈重出一版…" : "→ 正在叫 agent 出提示词…");
  toast(`已叫 agent（预计 ${Math.round((r.eta || 180) / 60)} 分钟`
        + (r.eta_n ? "，按本项目 ${r.eta_n} 次历史" : "") + "）");
  watchAgent();
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
  const r = await api("/api/feedback", {text, callAgent: true});
  if (!r.ok) return toast(r.error || "提交失败");
  $("fb").value = "";
  logLine(`已记入第 ${r.round} 轮反馈`);
  if (r.agent && r.agent.error) { logLine(r.agent.error, "bad"); return; }
  $("prog").hidden = false;
  watchAgent();
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
  const set = (on) => {
    dz.classList.toggle("hot", on); col.classList.toggle("hot", on);
    $("dz").querySelector("b").textContent = on ? "松开即投放 → 自动归类" : "把素材拖到这里";
  };
  window.addEventListener("dragenter", (e) => { e.preventDefault(); depth++; set(true); });
  window.addEventListener("dragover", (e) => { e.preventDefault(); });
  window.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; set(false); } });
  window.addEventListener("drop", async (e) => {
    e.preventDefault(); depth = 0; set(false);
    const files = [...(e.dataTransfer ? e.dataTransfer.files : [])];
    if (!files.length) return;
    toast(`正在投递 ${files.length} 个文件…`);
    let ok = 0;
    for (const f of files) { if (await uploadFile(f)) ok++; }
    toast(ok ? `收下 ${ok} 个文件，点「建框架归类」落位` : "投递失败，改用「选择素材」");
  });
}

/* ── 心跳：页面关了让服务自己退 ────────────────────────────────── */
setInterval(() => { fetch("/api/ping", {method: "POST"}).catch(() => {}); }, 3000);

/* ── 绑定 ─────────────────────────────────────────────────────────── */
$("btnRefresh").onclick = () => loadState();
$("btnLoadPrompts").onclick = async () => {
  const pr = await api("/api/prompts");
  S.prompts = pr.prompts || []; S.uploads = pr.uploads || [];
  renderPrompts(); renderFlow(); toast("已读取提示词");
};
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
  toast("已重新载入评分");
};
$("btnHelp").onclick = () => helpModal();
$("btnClassic").onclick = () => {
  fetch("/api/classic", {method: "POST"}).catch(() => {});
  toast("经典界面需要重新启动 exe（下个版本给这个按钮接上）");
};
$("note").oninput = () => { S.review.备注 = $("note").value; };

bindDrop();
loadState();
logLine("工作台已就绪");
