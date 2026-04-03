const STORAGE_KEY = "crucial_talk_scene_v3";

function $(id) {
  return document.getElementById(id);
}

function setPill(id, text, tone) {
  const el = $(id);
  el.textContent = text;
  el.classList.remove("ok", "warn", "danger");
  if (tone) el.classList.add(tone);
}

function escapeHtml(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function getFormData() {
  return {
    relationship: $("relationship").value,
    counterpart: $("counterpart").value.trim(),
    channel: $("channel").value,
    goal: $("goal").value.trim(),
    fear: $("fear").value.trim(),
    situation: $("situation").value.trim(),
    task: $("task").value.trim(),
    action: $("action").value.trim(),
    result: $("result").value.trim(),
    last_dialogue_you: $("last_dialogue_you").value.trim(),
    last_dialogue_them: $("last_dialogue_them").value.trim(),
    your_story: $("your_story").value.trim(),
    constraints: $("constraints").value.trim(),
  };
}

function setFormData(d) {
  $("relationship").value = d.relationship || "";
  $("counterpart").value = d.counterpart || "";
  $("channel").value = d.channel || "";
  $("goal").value = d.goal || "";
  $("fear").value = d.fear || "";
  $("situation").value = d.situation || "";
  $("task").value = d.task || "";
  $("action").value = d.action || "";
  $("result").value = d.result || "";
  $("last_dialogue_you").value = d.last_dialogue_you || "";
  $("last_dialogue_them").value = d.last_dialogue_them || "";
  $("your_story").value = d.your_story || "";
  $("constraints").value = d.constraints || "";
}

function persist() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(getFormData()));
    $("saveHint").textContent = "已自动保存在本机浏览器。";
  } catch {
    $("saveHint").textContent = "保存失败（可能是浏览器限制）。";
  }
}

function loadPersisted() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    const d = JSON.parse(raw);
    if (d && typeof d === "object") setFormData(d);
  } catch {}
}

function resetAll() {
  setFormData({
    relationship: "",
    counterpart: "",
    channel: "",
    goal: "",
    fear: "",
    situation: "",
    task: "",
    action: "",
    result: "",
    last_dialogue_you: "",
    last_dialogue_them: "",
    your_story: "",
    constraints: "",
  });
  $("diagBox").classList.add("empty");
  $("planBox").classList.add("empty");
  $("diagBox").textContent = "点击“生成分析与方案”后会先展示这一块。";
  $("planBox").textContent = "问题分析出来后，再展示这一块。";
  $("formError").textContent = "";
  setPill("statusPill", "未生成");
  setPill("diagPill", "未生成");
  setPill("planPill", "未生成");
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {}
  persist();
}

function demo() {
  setFormData({
    relationship: "同级",
    counterpart: "A（跨部门接口人）",
    channel: "会议",
    goal: "对齐本周发布范围与各自承担项，并确认资源支持",
    fear: "对方当场翻脸，项目推进更难",
    situation: "两部门共同负责版本发布，我负责排期对齐。近期对方多次临时变更需求。",
    task: "周二前锁定发布范围与资源分配，保证周五上线。",
    action: "会上我说“你们总是临时改，影响大家进度”。对方回“你们排期不合理，我们不背锅”。",
    result: "没有形成结论，后续仍在提变更。",
    last_dialogue_you: "你们总是临时改，不能这样搞。",
    last_dialogue_them: "你们排期不合理，我们不背锅。",
    your_story: "我觉得他在甩锅。",
    constraints: "会议只有15分钟；不能公开点名。",
  });
  persist();
}

function pathHasContent(p) {
  if (!p || typeof p !== "object") return false;
  return ["facts", "story", "emotion", "behavior", "emotion_why"].some((k) => {
    const v = p[k];
    return v && String(v).trim();
  });
}

function renderPathSide(title, p) {
  if (!pathHasContent(p)) return "";
  const rows = [
    ["facts", "事实"],
    ["story", "故事"],
    ["emotion", "情绪"],
    ["behavior", "行为"],
    ["emotion_why", "情绪成因"],
  ];
  const body = rows
    .map(([key, lab]) => {
      const v = p[key];
      if (!v || !String(v).trim()) return "";
      return `<div class="path-row"><span class="path-lab">${escapeHtml(lab)}</span><span class="path-val">${escapeHtml(
        String(v)
      )}</span></div>`;
    })
    .filter(Boolean)
    .join("");
  return `<div class="section"><h3>${escapeHtml(title)}</h3><div class="path-grid">${body}</div></div>`;
}

function renderDiagnosis(resp) {
  const d = resp.diagnosis || {};
  setPill("diagPill", `AI · 已生成`, "ok");

  let cpr = d.cpr || {};
  if (typeof cpr === "string") {
    cpr = { content: cpr, pattern: "", relationship: "" };
  }
  const bp = d.behavior_path || {};
  const facts = Array.isArray(d.key_facts) ? d.key_facts : [];
  const py = d.path_you || {};
  const pt = d.path_them || {};
  const emotionBoth = (d.emotion_why_both || "").trim();
  const hasStructuredPath = pathHasContent(py) || pathHasContent(pt);

  const html = `
    ${
      resp.ai_failure
        ? `<div class="warnbox"><strong>AI 问题分析失败</strong><br/>${escapeHtml(
            resp.ai_failure.reason
          )} — ${escapeHtml(resp.ai_failure.detail || "")}</div>`
        : ""
    }
    <div class="section">
      <h3>CPR 拆解（内容 / 互动模式 / 关系）</h3>
      <div class="mono">C（内容）：${escapeHtml(cpr.content || "")}\nP（模式）：${escapeHtml(
    cpr.pattern || ""
  )}\nR（关系）：${escapeHtml(cpr.relationship || "")}</div>
    </div>
    ${
      emotionBoth
        ? `<div class="section highlight"><h3>双方情绪成因（重点）</h3><div class="mono">${escapeHtml(
            emotionBoth
          )}</div></div>`
        : ""
    }
    ${hasStructuredPath ? renderPathSide("你的行为路径（事实→故事→情绪→行为）", py) : ""}
    ${hasStructuredPath ? renderPathSide("对方的行为路径（事实→故事→情绪→行为）", pt) : ""}
    ${
      !hasStructuredPath
        ? `<div class="section">
      <h3>行为路径（沉默/暴力倾向 · 摘要）</h3>
      <div class="mono">你：${escapeHtml(bp.you || "")}\n对方：${escapeHtml(bp.them || "")}</div>
    </div>`
        : ""
    }
    <div class="section">
      <h3>共同目标（一句话）</h3>
      <div class="mono">${escapeHtml(d.common_goal || "")}</div>
    </div>
    <div class="section">
      <h3>关键事实（可验证）</h3>
      <ul class="list">${facts.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>
    </div>
    <div class="section">
      <h3>把“故事”降火后的表述</h3>
      <div class="mono">${escapeHtml(d.your_story_clean || "")}</div>
    </div>
  `;

  $("diagBox").classList.remove("empty");
  $("diagBox").innerHTML = html;
}

function renderPlan(resp) {
  const p = resp.plan || {};
  setPill("planPill", `AI · 已生成`, "ok");

  const state = Array.isArray(p.state) ? p.state : [];
  const ampp = Array.isArray(p.ampp) ? p.ampp : [];
  const close = Array.isArray(p.close) ? p.close : [];

  const html = `
    ${
      resp.ai_failure
        ? `<div class="warnbox"><strong>AI 沟通建议失败</strong><br/>${escapeHtml(
            resp.ai_failure.reason
          )} — ${escapeHtml(resp.ai_failure.detail || "")}</div>`
        : ""
    }
    <div class="section">
      <h3>开场（下一次沟通 · 共同目的/对比说明）</h3>
      <div class="mono">${escapeHtml(p.opening || "")}</div>
    </div>
    <div class="section">
      <h3>STATE（下一次沟通 · 分享你的路径）</h3>
      <ol class="list">${state.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ol>
    </div>
    <div class="section">
      <h3>AMPP（下一次沟通 · 征询对方）</h3>
      <ul class="list">${ampp.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>
    </div>
    <div class="section">
      <h3>收尾（WHAT/WHO/WHEN）</h3>
      <ol class="list">${close.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ol>
    </div>
  `;

  $("planBox").classList.remove("empty");
  $("planBox").innerHTML = html;
}

async function run() {
  $("formError").textContent = "";
  setPill("statusPill", "生成中…", "warn");
  setPill("diagPill", "生成中…", "warn");
  setPill("planPill", "等待问题分析…", "warn");
  $("diagBox").classList.remove("empty");
  $("diagBox").textContent =
    "正在生成【问题分析】（模型排队或内容较长时可能需要 1～3 分钟，请稍候）…";
  $("planBox").classList.add("empty");
  $("planBox").textContent = "等待问题分析完成后生成【沟通方式建议】…";

  const input = getFormData();
  persist();

  // Step 1: diagnosis (AI-only; poll job until done/failed)
  const r1 = await fetch("/api/diagnose", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!r1.ok) {
    $("formError").textContent = "请补全必填项后再生成。";
    setPill("statusPill", "未生成");
    return;
  }
  const diagStart = Date.now();
  const diagInit = await r1.json();
  const diagJobId = diagInit.job_id;
  if (!diagJobId) {
    $("formError").textContent = "诊断任务创建失败。";
    setPill("statusPill", "失败", "danger");
    return;
  }
  let diagnosis = null;
  while (Date.now() - diagStart < 30 * 60 * 1000) {
    if (Date.now() - diagStart > 45000) setPill("diagPill", "AI仍在生成…", "warn");
    await new Promise((r) => setTimeout(r, 900));
    const jr = await fetch(`/api/jobs/${diagJobId}`);
    if (!jr.ok) break;
    const job = await jr.json();
    // Progress hint
    if (job.status === "running") {
      const att = job.attempt ? `第${job.attempt}次尝试` : "尝试中";
      const lf = job.last_failure && job.last_failure.reason ? `，上次失败：${job.last_failure.reason}` : "";
      $("diagBox").textContent = `AI问题分析生成中（通常 1～3 分钟）…（${att}${lf}）`;
    }
    if (job.status === "done" && job.diagnosis) {
      diagnosis = job.diagnosis;
      renderDiagnosis({ diagnosis });
      break;
    }
    if (job.status === "failed") {
      const fail = job.ai_failure || {};
      $("diagBox").classList.remove("empty");
      $("diagBox").innerHTML = `<div class="warnbox"><strong>AI 问题分析失败</strong><br/>${escapeHtml(
        fail.reason || ""
      )} — ${escapeHtml(fail.detail || "")}</div>`;
      setPill("diagPill", "失败", "danger");
      setPill("statusPill", "失败", "danger");
      break;
    }
  }

  // Step 2: plan (AI-only; poll job until done/failed)
  setPill("planPill", "生成中…", "warn");
  $("planBox").classList.remove("empty");
  $("planBox").textContent =
    "正在生成【沟通方式建议】（约 1～2 分钟）…";

  const r2 = await fetch("/api/advise", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...input, diagnosis }),
  });
  if (!r2.ok) {
    const j = await r2.json().catch(() => ({}));
    $("formError").textContent = j.error === "missing_diagnosis" ? "缺少问题分析结果，无法生成建议。" : "生成建议失败。";
    setPill("planPill", "失败", "danger");
    setPill("statusPill", "失败", "danger");
    return;
  }
  const planInit = await r2.json();
  const planJobId = planInit.job_id;
  if (!planJobId) {
    $("formError").textContent = "建议任务创建失败。";
    setPill("planPill", "失败", "danger");
    setPill("statusPill", "失败", "danger");
    return;
  }
  const planStart = Date.now();
  let plan = null;
  while (Date.now() - planStart < 30 * 60 * 1000) {
    if (Date.now() - planStart > 45000) setPill("planPill", "AI仍在生成…", "warn");
    await new Promise((r) => setTimeout(r, 900));
    const jr = await fetch(`/api/jobs/${planJobId}`);
    if (!jr.ok) break;
    const job = await jr.json();
    if (job.status === "running") {
      const att = job.attempt ? `第${job.attempt}次尝试` : "尝试中";
      const lf = job.last_failure && job.last_failure.reason ? `，上次失败：${job.last_failure.reason}` : "";
      $("planBox").textContent = `AI沟通建议生成中（约 1～2 分钟）…（${att}${lf}）`;
    }
    if (job.status === "done" && job.plan) {
      plan = job.plan;
      renderPlan({ plan });
      break;
    }
    if (job.status === "failed") {
      const fail = job.ai_failure || {};
      $("planBox").classList.remove("empty");
      $("planBox").innerHTML = `<div class="warnbox"><strong>AI 沟通建议失败</strong><br/>${escapeHtml(
        fail.reason || ""
      )} — ${escapeHtml(fail.detail || "")}</div>`;
      setPill("planPill", "失败", "danger");
      setPill("statusPill", "失败", "danger");
      break;
    }
  }
  if (!plan) {
    $("planBox").classList.remove("empty");
    $("planBox").innerHTML = `<div class="warnbox"><strong>AI 沟通建议未生成</strong><br/>可能仍在排队或已失败，请稍后再试。</div>`;
    setPill("planPill", "失败", "danger");
    setPill("statusPill", "失败", "danger");
    return;
  }

  setPill("statusPill", "已生成", "ok");
}

function bindAutosave() {
  const ids = [
    "relationship",
    "counterpart",
    "channel",
    "goal",
    "fear",
    "situation",
    "task",
    "action",
    "result",
    "last_dialogue_you",
    "last_dialogue_them",
    "your_story",
    "constraints",
  ];
  ids.forEach((id) => {
    const el = $(id);
    if (!el) return;
    el.addEventListener("input", persist);
    el.addEventListener("change", persist);
  });
}

window.addEventListener("DOMContentLoaded", () => {
  loadPersisted();
  bindAutosave();

  $("sceneForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await run();
    } catch (err) {
      $("formError").textContent = "生成失败：请确认本地服务已启动。";
      setPill("statusPill", "失败", "danger");
    }
  });

  $("btnReset").addEventListener("click", resetAll);
  $("btnLoadDemo").addEventListener("click", demo);
});

