const $ = (q, root = document) => root.querySelector(q);
const $$ = (q, root = document) => [...root.querySelectorAll(q)];

const groupsEl = $("#groups");
const statsEl = $("#stats");
const minScoreEl = $("#min-score");
const topPerCompanyEl = $("#top-per-company");
const qEl = $("#q");
const statusChecks = $$(".status-filter input[type=checkbox]");

function scoreClass(s) {
  if (s >= 90) return "s90";
  if (s >= 80) return "s80";
  if (s >= 70) return "s70";
  if (s >= 60) return "s60";
  return "slo";
}

function formatDate(iso) {
  if (!iso) return "—";
  return iso.slice(0, 10);
}

function ageStr(days) {
  if (days === null || days === undefined) return "";
  if (days <= 0) return "posted today";
  if (days === 1) return "posted 1 day ago";
  return `posted ${days} days ago`;
}

function breakdownStr(p) {
  if (p.skill_fit === null || p.skill_fit === undefined) return "";
  return `skill ${p.skill_fit} · seniority ${p.seniority_fit} · domain ${p.domain_fit} · loc-tier ${p.location_tier}`;
}

// Freshest-first within a score tie; unknown age sorts last.
function freshOf(p) {
  return (p.freshness_days === null || p.freshness_days === undefined) ? 1e9 : p.freshness_days;
}

function statusTag(status) {
  return `<span class="status-tag ${status}">${status}</span>`;
}

function escape(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

function renderPosting(p) {
  const tpl = $("#posting-template");
  const frag = tpl.content.cloneNode(true);
  const root = $(".posting", frag);
  root.dataset.fp = p.fingerprint;

  const score = $(".score", root);
  score.textContent = p.score;
  score.classList.add(scoreClass(p.score));

  $(".posting-title", root).textContent = p.title;
  const metaParts = [p.location || "(no location)"];
  const age = ageStr(p.freshness_days);
  if (age) metaParts.push(age);
  if (p.interview_style) metaParts.push(`🤝 ${p.interview_style}`);
  metaParts.push(`seen ${formatDate(p.first_seen)}`);
  $(".posting-meta", root).textContent = metaParts.join(" · ");

  const tag = $(".status-tag", root);
  tag.textContent = p.status || "pending";
  tag.classList.add(p.status || "pending");

  const bd = breakdownStr(p);
  $(".rationale", root).textContent =
    (p.rationale || "(no rationale)") + (bd ? `  —  [${bd}]` : "");

  const sUl = $(".strengths ul", root);
  const cUl = $(".concerns ul", root);
  (p.strengths || []).forEach(s => {
    const li = document.createElement("li");
    li.textContent = s;
    sUl.appendChild(li);
  });
  (p.concerns || []).forEach(c => {
    const li = document.createElement("li");
    li.textContent = c;
    cUl.appendChild(li);
  });
  if (!(p.strengths || []).length) sUl.innerHTML = '<li class="muted">(none recorded)</li>';
  if (!(p.concerns  || []).length) cUl.innerHTML = '<li class="muted">(none recorded)</li>';
  $(".description pre", root).textContent = p.description || "(description not stored — scored before schema upgrade)";

  const apply = $(".apply", root);
  apply.href = p.url;
  apply.addEventListener("click", () => setStatus(p.fingerprint, "applied"));
  $(".dismiss", root).addEventListener("click", () => setStatus(p.fingerprint, "dismissed"));
  $(".reset", root).addEventListener("click", () => setStatus(p.fingerprint, "pending"));

  const head = $(".posting-head", root);
  const detail = $(".detail", root);
  head.addEventListener("click", () => {
    const willOpen = detail.hidden;
    detail.hidden = !willOpen;
    root.classList.toggle("expanded", willOpen);
    if (willOpen) openFps.add(p.fingerprint); else openFps.delete(p.fingerprint);
  });
  if (openFps.has(p.fingerprint)) {
    detail.hidden = false;
    root.classList.add("expanded");
  }
  return frag;
}

function renderGroup(company, postings, topN) {
  const tpl = $("#group-template");
  const frag = tpl.content.cloneNode(true);
  const section = $(".group", frag);
  section.dataset.company = company;

  $(".group-name", frag).textContent = company;
  const bestScore = postings[0].score;
  const total = postings.length;
  const applied = postings.filter(p => p.status === "applied").length;
  const meta = $(".group-meta", frag);
  const parts = [
    `<span class="score-pill ${scoreClass(bestScore)}">best ${bestScore}</span>`,
    `<span class="muted">${total} match${total === 1 ? "" : "es"}</span>`,
  ];
  if (applied > 0) parts.push(`<span class="muted">· ${applied} applied</span>`);
  meta.innerHTML = parts.join(" ");

  const rowsEl = $(".group-rows", frag);
  const toggle = $(".group-toggle", frag);

  const visible = postings.slice(0, topN);
  const hidden = postings.slice(topN);

  visible.forEach(p => rowsEl.appendChild(renderPosting(p)));

  if (hidden.length > 0) {
    toggle.hidden = false;
    const expanded = expandedGroups.has(company);
    toggle.textContent = expanded ? "Hide extras" : `Show ${hidden.length} more`;
    if (expanded) {
      hidden.forEach(p => rowsEl.appendChild(renderPosting(p)));
    }
    toggle.addEventListener("click", () => {
      if (expandedGroups.has(company)) {
        expandedGroups.delete(company);
      } else {
        expandedGroups.add(company);
      }
      renderGroups();
    });
  }

  return frag;
}

async function setStatus(fp, status) {
  const r = await fetch(`/api/postings/${fp}/status`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  if (!r.ok) {
    alert("Failed to update status");
    return;
  }
  await load();
}

async function loadStats() {
  const r = await fetch("/api/stats");
  const s = await r.json();
  const bs = s.by_status || {};
  const bk = s.by_score_bucket || {};
  statsEl.innerHTML = [
    `<span class="pill">Total ${s.total ?? 0}</span>`,
    `<span class="pill">Pending ${bs.pending ?? 0}</span>`,
    `<span class="pill">Applied ${bs.applied ?? 0}</span>`,
    `<span class="pill">Dismissed ${bs.dismissed ?? 0}</span>`,
    `<span class="pill">90+: ${bk["90+"] ?? 0}</span>`,
    `<span class="pill">80–89: ${bk["80-89"] ?? 0}</span>`,
    `<span class="pill">70–79: ${bk["70-79"] ?? 0}</span>`,
    `<span class="pill">60–69: ${bk["60-69"] ?? 0}</span>`,
  ].join("");
}

let postings = [];
let openFps = new Set();
let expandedGroups = new Set();

async function load() {
  const params = new URLSearchParams();
  params.set("min_score", minScoreEl.value || "0");
  statusChecks.filter(c => c.checked).forEach(c => params.append("status", c.value));
  if (qEl.value.trim()) params.set("q", qEl.value.trim());
  const r = await fetch(`/api/postings?${params}`);
  postings = await r.json();
  renderGroups();
  loadStats();
}

function groupByCompany(ps) {
  const groups = new Map();
  for (const p of ps) {
    if (!groups.has(p.company)) groups.set(p.company, []);
    groups.get(p.company).push(p);
  }
  // Sort each group by score desc, then sort groups by best score desc
  const arr = [];
  for (const [company, items] of groups) {
    items.sort((a, b) => (b.score - a.score) || (freshOf(a) - freshOf(b)));
    arr.push({ company, items, best: items[0].score });
  }
  arr.sort((a, b) => b.best - a.best);
  return arr;
}

function renderGroups() {
  groupsEl.innerHTML = "";
  if (postings.length === 0) {
    groupsEl.innerHTML = '<p class="empty">No postings match the current filters.</p>';
    return;
  }
  const topN = Math.max(1, parseInt(topPerCompanyEl.value, 10) || 2);
  const groups = groupByCompany(postings);
  for (const { company, items } of groups) {
    groupsEl.appendChild(renderGroup(company, items, topN));
  }
}

minScoreEl.addEventListener("change", load);
topPerCompanyEl.addEventListener("change", renderGroups);
qEl.addEventListener("input", debounce(load, 200));
statusChecks.forEach(c => c.addEventListener("change", load));

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

load();
setInterval(load, 30000);
