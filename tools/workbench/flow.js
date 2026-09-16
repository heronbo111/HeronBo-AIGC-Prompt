/* 流程指挥台（嵌在工作台里的独立页）
   分工：**这里只画 + 只发命令**，所有动作仍由外层 app.js 执行（单一入口，不重复实现业务）。
   外层 → 这里：{t:"init", …状态} / {t:"prog", …进度} / {t:"toast", msg}
   这里 → 外层：{t:"hello"} / {t:"cmd", cmd:"act|goto|close|agent", …} */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const send = (msg) => { try { parent.postMessage(msg, "*"); } catch (e) {} };

let S = null;                 // 外层的状态快照
let keep = {};                // 重画前把用户正在输入的内容存一下

/* ── 每一步：怎么做 + 能当场点的动作 ─────────────────────────────── */
const DOC = [
  {what: ["把 <b>文案 / 形象图 / 音频 / 原片</b> 拖进②栏；整个文件夹用「选文件夹」（浏览器拿不到文件夹路径）",
          "在②栏「文案 / 素材识别」里**写清你的简单需求**（要什么、几个人、几秒），agent 出提示词与核对归类都按它来",
          "点「建框架归类」：软件按文件名/扩展名先落位；**猜得不一定准**——不准就点「核对归类」让 agent 逐件复核",
          "素材能<b>拖动改顺序</b>：这个顺序就是 @图片1 / @音频1 的编号顺序"],
   acts: [{k: "pick", label: "选素材", main: 1}, {k: "pickdir", label: "选文件夹"},
          {k: "intake", label: "建框架归类", need: "pending"},
          {k: "intakeCheck", label: "核对归类", need: "mats"}]},
  {what: ["点「出提示词」：agent 读技能与框架 → 写提示词 → 按引用编号把素材副本放进 <code>即梦上传/</code> → 写回执",
          "本机有多个可用 agent 时，第一次会先问用哪个，选完记住（记在哪见右下角）",
          "用时按本项目历史中位数估，跑完自动记账"],
   acts: [{k: "ask", label: "出提示词", main: 1, need: "agent"},
          {k: "copy", label: "复制提示词", need: "prompts"},
          {k: "reload", label: "重新读取"}]},
  {what: ["复制提示词（连「上传清单」一起复制），照单把素材<b>传全</b>——提示词里没写的别传",
          "在平台上<b>手选画幅</b>：默认竖版 9:16，按需横屏/方屏（提示词里的画幅句不决定成片比例）",
          "生成是<b>你自己的动作</b>：工作台不代提交，积分自己看准"],
   acts: [{k: "copy", label: "复制提示词 + 上传清单", main: 1, need: "prompts"},
          {k: "reload", label: "刷新清单"}]},
  {what: ["成片拖回②栏，或点「收成片」选文件 → 落 <code>成片/</code>",
          "废片点「收废片」并写废因（必填）：失败片先让 agent 做机器诊断，别凭感觉重跑"],
   acts: [{k: "good", label: "收成片", main: 1},
          {k: "bad", label: "收废片", with: "why"}]},
  {what: ["④栏逐项打分（拖动滑块）→ 保存，写进 <code>评价/</code>；废片会连废因一起留在 <code>废片/</code>",
          "写清现象 → 提交反馈，agent 照它再出一版（反馈写<b>现象</b>，别写「不好看」）",
          "**评价与废因要还回 skill**：点「评价反哺 skill」让 agent 读 <code>评价/*.json</code> 与废因，"
          + "把可复用的经验沉淀成 <code>references/rules*.md</code> 里的规则——闭环才真正合上"],
   acts: [{k: "score", label: "去④栏打分", main: 1},
          {k: "feedback", label: "提交反馈 · 再出一版", with: "fb", need: "prompts"},
          {k: "learn", label: "评价反哺 skill", need: "review"}]},
];

function toast(msg) {
  const el = $("bToast"); el.textContent = msg; el.classList.add("on");
  clearTimeout(toast._t); toast._t = setTimeout(() => el.classList.remove("on"), 2600);
}

function saveKeep() {
  keep = {};
  document.querySelectorAll("[data-keep]").forEach((el) => { keep[el.dataset.keep] = el.value; });
}

function renderProgress() {
  const box = document.querySelector('.scard[data-i="1"] .sprog');
  if (!box) return;
  const p = S.prog;
  box.hidden = !p || !p.on;
  if (!p || !p.on) return;
  box.querySelector(".s").innerHTML = (p.done ? "" : '<span class="spin"></span>') + esc(p.stageName || "")
    + (p.quiet ? '<span class="quiet">· 这条通道不吐过程输出，按时间估</span>' : "");
  box.querySelector(".bar > i").style.width = (p.pct || 0) + "%";
  box.querySelector(".tm").textContent = p.done
    ? ("用时 " + fmt(p.elapsed)) : ("已用 " + fmt(p.elapsed) + " · 预计还要 " + fmt(Math.max(0, (p.eta || 0) - p.elapsed)));
  box.querySelectorAll(".stages span").forEach((sp) => {
    const i = +sp.dataset.i;
    sp.className = i < p.stage ? "done" : (i === p.stage ? "on" : "");
  });
  const ln = box.querySelector(".lines");
  ln.innerHTML = (p.lines || []).map((t) =>
    `<div class="${/^!!/.test(t) ? "bad" : (/✅/.test(t) ? "ok" : "")}">${esc(t)}</div>`).join("");
  ln.scrollTop = ln.scrollHeight;
}

const fmt = (s) => { s = Math.max(0, Math.round(s || 0));
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); };

function actBtn(i, a) {
  const dis = a.need && !has(a.need);
  return `<button class="btn ${a.main ? "" : "ghost"}" data-act="${esc(a.k)}" data-i="${i}"
    ${dis ? "disabled" : ""} title="${dis ? esc(whyNot(a.need)) : ""}">${esc(a.label)}</button>`;
}

function has(need) {
  if (need === "pending") return (S.pending || []).length > 0;
  if (need === "prompts") return (S.prompts || {}).n > 0;
  if (need === "agent") return !!(S.agent || {}).ok;
  if (need === "mats") return (S.mats || []).length > 0;
  if (need === "review") return !!S.hasReview;
  return true;
}

function whyNot(need) {
  if (need === "pending") return "②栏还没有待归类的素材";
  if (need === "prompts") return "还没有提示词，先在第②步出提示词";
  if (need === "agent") return "agent 通道不可用：" + ((S.agent || {}).why || "");
  if (need === "mats") return "②栏还没有素材，先丢素材并建框架归类";
  if (need === "review") return "还没有评分或废片，先收成片/废片并在④栏打分";
  return "";
}

function stepCard(i) {
  const st = S.steps[i], doc = DOC[i];
  const cls = i < S.cur ? "done" : (i === S.cur ? "now" : "todo");
  const status = i < S.cur ? "已完成" : (i === S.cur ? (S.manual ? "手动切到这一步" : "当前这一步") : "还没到");
  const facts = (S.facts || [])[i] || "";
  const inputs = doc.acts.map((a) => {
    if (a.with === "why") return `<input data-keep="why" data-i="${i}" placeholder="废因（必填）" value="${esc(keep.why || "")}">`;
    if (a.with === "fb") return `<textarea data-keep="fb" data-i="${i}" placeholder="哪里好、哪里不对？写清现象，agent 好照着改">${esc(keep.fb || "")}</textarea>`;
    return "";
  }).join("");
  const prog = i === 1
    ? `<div class="sprog" hidden><div class="top"><span class="s"></span><span class="tm"></span></div>
         <div class="bar"><i></i></div>
         <div class="stages">${["读技能 / 框架", "写提示词", "落即梦上传", "写回执"]
           .map((t, k) => `<span data-i="${k}">${t}</span>`).join("")}</div>
         <div class="lines"></div></div>`
    : "";
  // 第①张卡把用户的需求和素材清单摆出来（需求是硬约束，得看得见）
  const need = i === 0
    ? `<div class="sneed">${S.need ? "需求：" + esc(S.need)
        : "（还没写需求——②栏可以写一句，agent 会按它来）"}</div>`
      + ((S.mats || []).length
         ? `<div class="smats">${(S.mats || []).map((m) =>
             `<span class="mtag${m.role ? "" : " nr"}">${esc(m.role || "待识别")}·${esc(m.name)}</span>`)
             .join("")}</div>`
         : "")
    : "";
  return `<section class="scard ${cls}" data-i="${i}">
    <div class="sident">
      <div class="sline"><span class="sno">${i + 1}</span>
        <span class="sname">${esc(st.n)}</span>
        <span class="who ${st.who === "你" ? "me" : (st.who === "agent" ? "agent" : "soft")}">${esc(st.who)} 在做</span></div>
      <div class="sstatus">${esc(status)}</div>
      <div class="sfacts">${facts}</div>
    </div>
    <div class="sbody">
      <div class="stask">${esc(st.man)}</div>
      <ul class="slist">${doc.what.map((x) => `<li>${x}</li>`).join("")}</ul>
      ${need}
      ${prog}
      <div class="sacts">${doc.acts.map((a) => actBtn(i, a)).join("")}${inputs}</div>
    </div>
  </section>`;
}

function render() {
  if (!S) return;
  saveKeep();
  document.body.dataset.theme = (S.theme === "原版") ? "" : S.theme;

  $("bProj").textContent = (S.project && S.project.name) || "未选项目";
  $("bMeta").textContent = "构建 " + (S.build || "—");
  const a = S.agent || {};
  const chip = $("bAgent");
  chip.textContent = a.ok ? ("agent " + a.label) : "agent 不可用";
  chip.title = (a.ok ? ((a.chosen ? "已选定：" : "自动挑选：") + (a.why || "")) : (a.why || ""))
    + "\n配置：" + (a.cfg || "—");

  const who = (S.cur === 1 && a.ok) ? `（用 ${a.label}）` : "";
  $("bCmdText").innerHTML = `<b>${esc(S.steps[S.cur].n)}</b>：`
    + (S.cur === S.auto ? esc(S.why || "") + who
                        : "你手动切到了这一步；点「回到自动」恢复按状态判断");
  $("bPrev").disabled = S.cur <= 0;
  $("bNext").disabled = S.cur >= S.steps.length - 1;
  $("bAuto").hidden = !S.manual;

  $("bBoard").innerHTML = S.steps.map((_, i) => stepCard(i)).join("");
  bindBoard();
  renderProgress();

  $("bFoot").innerHTML = [
    "项目 " + ((S.project && S.project.createdAt) || "—"),
    "样本库根 " + esc(S.root || "—"),
    "agent 记在 <code>" + esc(a.cfg || "—") + "</code>",
  ].join("<br>");
}

function bindBoard() {
  const board = $("bBoard");
  board.querySelectorAll("[data-act]").forEach((b) => {
    b.onclick = () => {
      const sec = b.closest(".scard");
      const payload = {t: "cmd", cmd: "act", i: +b.dataset.i, k: b.dataset.act};
      const why = sec.querySelector('input[data-keep="why"]');
      const fb = sec.querySelector('textarea[data-keep="fb"]');
      if (why) payload.why = why.value;
      if (fb) payload.fb = fb.value;
      send(payload);
      // 发出去就把框清空：不然重画后旧文字又回来了（外层也会清它自己那份）
      if (why) { why.value = ""; keep.why = ""; }
      if (fb) { fb.value = ""; keep.fb = ""; }
    };
  });
  // 点卡片的空白处 = 把流程切到这一步
  board.querySelectorAll(".scard").forEach((sec) => {
    sec.querySelector(".sident").onclick = () => send({t: "cmd", cmd: "goto", i: +sec.dataset.i});
  });
}

$("bClose").onclick = () => send({t: "cmd", cmd: "close"});
$("bAgent").onclick = () => send({t: "cmd", cmd: "agent"});
$("bPrev").onclick = () => send({t: "cmd", cmd: "step", d: -1});
$("bNext").onclick = () => send({t: "cmd", cmd: "step", d: 1});
$("bAuto").onclick = () => send({t: "cmd", cmd: "auto"});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") send({t: "cmd", cmd: "close"}); });

window.addEventListener("message", (ev) => {
  const d = ev.data || {};
  if (d.t === "init") { S = d; render(); }
  else if (d.t === "prog") { if (S) { S.prog = d.prog; renderProgress(); } }
  else if (d.t === "toast") toast(d.msg);
});

send({t: "hello"});
