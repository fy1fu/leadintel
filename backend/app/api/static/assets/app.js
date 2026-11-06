// LeadIntel interface (M1). Plain JavaScript, no build step. All data shown here comes from untrusted
// sources: every value goes through esc(), and only server-vetted links (href) are made clickable.
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const fmt = n => Number(n ?? 0).toLocaleString();
const human = s => String(s ?? "").replace(/_/g, " ").replace(/^./, c => c.toUpperCase());
const countyName = key => state.options?.counties?.find(c => c.key === key)?.name || human(key);
const TOKEN = $('meta[name="leadintel-token"]')?.content || "";
const PAGE_SIZE = 50;

const LABELS = {
  qualification: {provisionally_qualified: "Provisionally qualified", needs_research: "Needs research",
                  not_qualified: "Not qualified", unscored: "Not scored yet"},
  website: {listed: "Website listed", social_only: "Only social / link page", none_recorded: "No website recorded",
            unknown: "Unknown"},
  operating: {open: "Open", closed: "Closed", unknown: "Unknown"},
};
const DEFAULTS = {qualification: ["provisionally_qualified", "needs_research"], operating: ["open", "unknown"]};
const MULTI = ["county", "niche", "qualification", "website", "operating"];
const SINGLE = ["q", "phone", "email", "lps_min", "bos_min", "sort"];

const state = {filters: {}, page: 1, selected: null, options: null, view: "split", items: [], seq: 0};

// ---------------------------------------------------------------- helpers
async function api(path, opts = {}) {
  const r = await fetch(path, {...opts, headers: {...(opts.headers || {}), ...(opts.method && opts.method !== "GET" ? {"X-LeadIntel-Token": TOKEN} : {})}});
  if (!r.ok) {
    const detail = (await r.json().catch(() => ({}))).detail;
    throw new Error(typeof detail === "string" ? detail : `${r.status} ${r.statusText}`);
  }
  return r.json();
}
function storage(key, value) {          // per-viewer convenience only; works without storage
  try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch { return null; }
}
let toastTimer;
function toast(msg) {
  let t = $(".toast"); if (!t) { t = document.createElement("div"); t.className = "toast"; t.setAttribute("role", "status"); document.body.append(t); }
  t.textContent = msg; t.classList.add("show"); clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("show"), 1600);
}
async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied"); } catch { toast("Copy failed - select the text instead"); }
}
const safeWeb = h => (typeof h === "string" && /^https?:\/\//i.test(h)) ? h : null;
const safeContact = h => (typeof h === "string" && /^(tel:\+?[0-9]+|mailto:[^?#\s]+)$/i.test(h)) ? h : null;

// ---------------------------------------------------------------- filter state <-> URL
function query(extra = {}) {
  const p = new URLSearchParams();
  for (const k of MULTI) if (state.filters[k]?.length) p.set(k, state.filters[k].join(","));
  for (const k of SINGLE) if (state.filters[k] !== undefined && state.filters[k] !== "") p.set(k, state.filters[k]);
  for (const [k, v] of Object.entries(extra)) p.set(k, v);
  return p.toString();
}
function readHash() {
  const h = new URLSearchParams(location.hash.slice(1));
  const f = {};
  if (![...h.keys()].length) { Object.assign(f, structuredClone(DEFAULTS)); }
  for (const k of MULTI) if (h.has(k)) f[k] = h.get(k).split(",").filter(Boolean);
  for (const k of SINGLE) if (h.has(k)) f[k] = h.get(k);
  state.filters = f;
  state.page = Math.max(1, parseInt(h.get("page") || "1", 10) || 1);
  state.selected = h.has("id") ? parseInt(h.get("id"), 10) : null;
}
function writeHash() {
  const p = new URLSearchParams(query());
  if (state.page > 1) p.set("page", state.page);
  if (state.selected) p.set("id", state.selected);
  if (!p.toString()) p.set("all", "1");                         // an explicit "no filters" view
  history.replaceState(null, "", "#" + p.toString());
}

// ---------------------------------------------------------------- filter panel
function chip(key, value, label) {
  const on = (state.filters[key] || []).includes(value);
  return `<button type="button" class="chip" data-key="${esc(key)}" data-value="${esc(value)}" aria-pressed="${on}">${esc(label)}</button>`;
}
function renderFilters() {
  const o = state.options || {counties: [], niches: []};
  $("#f-county").innerHTML = o.counties.map(c => chip("county", c.key, c.name)).join("") || '<span class="muted">-</span>';
  $("#f-niche").innerHTML = o.niches.map(n => chip("niche", n, human(n))).join("") || '<span class="muted">-</span>';
  for (const k of ["qualification", "website", "operating"])
    $(`#f-${k}`).innerHTML = Object.entries(LABELS[k]).map(([v, l]) => chip(k, v, l)).join("");
  for (const k of ["phone", "email", "sort"]) $(`#${k}`).value = state.filters[k] || (k === "sort" ? "lps" : "");
  for (const k of ["lps_min", "bos_min"]) $(`#${k}`).value = state.filters[k] || "";
  $("#q").value = state.filters.q || "";
}
function onChip(e) {
  const b = e.target.closest(".chip"); if (!b) return;
  const {key, value} = b.dataset; const cur = new Set(state.filters[key] || []);
  cur.has(value) ? cur.delete(value) : cur.add(value);
  state.filters[key] = [...cur]; b.setAttribute("aria-pressed", cur.has(value));
  refresh(true);
}

// ---------------------------------------------------------------- data loading
let mapData = null, map = null, mapReady = false;
async function refresh(resetPage) {
  if (resetPage) state.page = 1;
  writeHash();
  const seq = ++state.seq;                                     // ignore answers to superseded requests
  $("#list").innerHTML = '<div class="state">Loading…</div>';
  try {
    const [d, s] = await Promise.all([
      api(`/api/prospects?${query({page: state.page, page_size: PAGE_SIZE})}`),
      api(`/api/prospects/summary?${query()}`)]);
    if (seq !== state.seq) return;
    state.items = d.items; state.loaded = true;
    renderList(d, s);
    if (state.view !== "list") loadPoints(seq);
    if (state.selected && !d.items.some(i => i.id === state.selected)) select(state.selected, false);
  } catch (e) {
    if (seq !== state.seq) return;
    $("#list").innerHTML = `<div class="state"><strong>Could not load prospects.</strong><br>${esc(e.message)}</div>`;
    $("#counts").textContent = ""; $("#pageinfo").textContent = "";
  }
}
async function loadPoints(seq) {
  try {
    const p = await api(`/api/prospects/points?${query()}`);
    if (seq !== state.seq) return;
    mapData = p; drawMap();
  } catch (e) { $("#mapcaption").textContent = "Map data unavailable: " + e.message; }
}

// ---------------------------------------------------------------- list
function itemHtml(i) {
  const q = i.qualification || "unscored";
  const web = LABELS.website[i.website_status] || "Unknown";
  return `<button type="button" class="item" role="option" data-id="${i.id}" aria-selected="${i.id === state.selected}">
    <div class="name"><span>${esc(i.name)}</span><span class="score">LPS <b>${esc(i.lps ?? "–")}</b></span></div>
    <div class="meta">${esc(human(i.industry))} · ${esc(countyName(i.county))} · BOS ${esc(i.bos ?? "–")}</div>
    <div class="tags"><span class="tag q-${esc(q)}">${esc(LABELS.qualification[q] || human(q))}</span>
      <span class="tag">${esc(web)}</span>${i.has_phone ? '<span class="tag">Phone listed</span>' : ""}${i.has_email ? '<span class="tag">Email listed</span>' : ""}
      ${i.operating_status && i.operating_status !== "open" ? `<span class="tag">${esc(human(i.operating_status))}</span>` : ""}</div>
  </button>`;
}
function renderList(d, s) {
  const start = d.total ? (state.page - 1) * PAGE_SIZE + 1 : 0, end = (state.page - 1) * PAGE_SIZE + d.items.length;
  $("#counts").textContent = d.total ? `${fmt(start)}–${fmt(end)} of ${fmt(d.total)} businesses` : "No matching businesses";
  $("#pageinfo").textContent = d.total ? `Page ${fmt(state.page)} of ${fmt(d.pages)}` : "";
  $("#prev").disabled = state.page <= 1; $("#next").disabled = !d.has_more;
  if (!d.items.length) {
    $("#list").innerHTML = noData
      ? '<div class="state"><strong>No prospect data yet.</strong><br>Run <strong>Load Data</strong> from the Start menu (LeadIntel folder), then refresh this page.</div>'
      : '<div class="state"><strong>No businesses match these filters.</strong><br><button class="link" id="clear2">Clear all filters</button></div>';
    $("#clear2")?.addEventListener("click", clearFilters);
  } else {
    $("#list").innerHTML = d.items.map(itemHtml).join("");
  }
  state.summary = s;
}
function clearFilters() { state.filters = {}; renderFilters(); refresh(true); }

// ---------------------------------------------------------------- map
function drawMap() {
  const p = mapData; if (!p) return;
  const missing = p.without_coordinates ? ` · ${fmt(p.without_coordinates)} have no location` : "";
  $("#mapcaption").textContent = `Map: ${fmt(p.returned)} of ${fmt(p.total)} matching businesses${missing}` +
    (p.truncated ? ` · limited to ${fmt(p.limit)} points, refine the filters` : "");
  if (!map || !mapReady) return;
  const fc = {type: "FeatureCollection", features: p.points.map(([id, lon, lat, q]) => ({type: "Feature",
    geometry: {type: "Point", coordinates: [lon, lat]}, properties: {id, q: q || "unscored"}}))};
  if (map.getSource("p")) { map.getSource("p").setData(fc); return; }
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  map.addSource("p", {type: "geojson", data: fc, cluster: true, clusterRadius: 40, clusterMaxZoom: 14});
  map.addLayer({id: "clusters", type: "circle", source: "p", filter: ["has", "point_count"],
    paint: {"circle-color": css("--accent"), "circle-opacity": .8, "circle-radius": ["step", ["get", "point_count"], 14, 50, 20, 500, 28]}});
  map.addLayer({id: "cluster-count", type: "symbol", source: "p", filter: ["has", "point_count"],
    layout: {"text-field": "{point_count_abbreviated}", "text-size": 12, "text-font": ["Noto Sans Regular"]}, paint: {"text-color": "#fff"}});
  map.addLayer({id: "pts", type: "circle", source: "p", filter: ["!", ["has", "point_count"]],
    paint: {"circle-radius": 6, "circle-stroke-width": 1.5, "circle-stroke-color": "#fff",
      "circle-color": ["match", ["get", "q"], "provisionally_qualified", css("--good"), "needs_research", css("--warn"), css("--none")]}});
  map.on("click", "pts", e => select(e.features[0].properties.id, false));
  map.on("click", "clusters", e => map.easeTo({center: e.features[0].geometry.coordinates, zoom: map.getZoom() + 2}));
  for (const l of ["pts", "clusters"]) {
    map.on("mouseenter", l, () => map.getCanvas().style.cursor = "pointer");
    map.on("mouseleave", l, () => map.getCanvas().style.cursor = "");
  }
}
function initMap() {
  try {
    map = new maplibregl.Map({container: "map", style: "https://tiles.openfreemap.org/styles/liberty",
      center: [-81.6, 30.2], zoom: 9, attributionControl: {compact: true}});
    map.addControl(new maplibregl.NavigationControl());
    map.on("load", () => { mapReady = true; drawMap(); });
    map.on("error", e => { if (!mapReady) $("#mapcaption").textContent = "Map could not load (it needs an internet connection). The list and records still work."; });
  } catch {
    map = null;
    $("#map").innerHTML = '<p class="mapnote">Map unavailable (it needs an internet connection). The list and business records still work.</p>';
  }
}

// ---------------------------------------------------------------- detail
const VERIFY = {ingested: "Listed in a source, not verified", verified: "Verified", conflict: "Sources disagree"};
function contactRows(list, kind, label) {
  const rows = list.filter(c => c.kind === kind);
  if (!rows.length) return `<div class="contact"><span class="muted">${esc(label)}: not found in the sources checked</span></div>`;
  return rows.map(c => {
    const href = safeContact(c.href);
    const val = href ? `<a href="${esc(href)}">${esc(c.display)}</a>` : esc(c.display);
    return `<div class="contact"><span class="muted">${esc(label)}</span> <span class="value">${val}</span>
      <button type="button" class="btn copy" data-copy="${esc(c.display)}">Copy</button>
      <span class="state-note">${esc(VERIFY[c.verification_state] || human(c.verification_state))} · ${esc(c.source_key)}</span></div>`;
  }).join("");
}
function websiteRows(profiles) {
  const sites = profiles.filter(p => p.platform_kind === "independent");
  const other = profiles.filter(p => p.platform_kind !== "independent");
  const row = (p, label) => {
    const href = safeWeb(p.href);
    const open = href ? `<a class="btn primary" href="${esc(href)}" target="_blank" rel="noopener noreferrer">${p.platform_kind === "independent" ? "Open website" : "Open page"}</a>` : "";
    return `<div class="contact"><span class="muted">${esc(label)}</span> <span class="value" title="${esc(p.url)}">${esc(p.url)}</span> ${open}
      <button type="button" class="btn copy" data-copy="${esc(p.url)}">Copy</button>
      <span class="state-note">${esc(VERIFY[p.verification_state] || human(p.verification_state))} · ${esc(p.source_key)}</span></div>`;
  };
  let html = sites.length ? sites.map(p => row(p, "Website")).join("")
    : '<div class="contact"><span class="muted">Website: none recorded in the sources checked (website search not done yet)</span></div>';
  html += other.map(p => row(p, human(p.platform))).join("");
  return html;
}
function table(rows, cols) {
  if (!rows || !rows.length) return '<p class="muted">None found in the sources checked.</p>';
  return `<table><tr>${cols.map(c => `<th>${esc(c[0])}</th>`).join("")}</tr>` + rows.map(r =>
    `<tr>${cols.map(c => `<td class="${c[2] || ""}">${esc(typeof c[1] === "function" ? c[1](r) : r[c[1]])}</td>`).join("")}</tr>`).join("") + "</table>";
}
function topFactors(score, n = 3) {
  if (!score?.factors?.length) return "";
  const f = score.factors.filter(x => x.component !== "cap" && x.effect).sort((a, b) => Math.abs(b.effect) - Math.abs(a.effect)).slice(0, n);
  return f.length ? `<ul class="factors">${f.map(x => `<li>${esc(human(x.factor))} <span class="muted">(${x.effect > 0 ? "+" : ""}${esc(Math.round(x.effect * 10) / 10)})</span></li>`).join("")}</ul>` : "";
}
function renderDetail(p) {
  const L = p.location, bos = p.scores.find(s => s.kind === "bos"), lps = p.scores.find(s => s.kind === "lps");
  const q = p.qualification, qs = q?.status || "unscored";
  const addr = (p.addresses || []).find(a => a.is_operating_location) || (p.addresses || [])[0];
  const county = state.items.find(i => i.id === L.id)?.county;
  const where = [addr?.city, county ? countyName(county) : ""].filter(Boolean).join(" · ");
  $("#detail").innerHTML = `
  <div class="dhead"><h2>${esc(L.display_name)}</h2>
    <div class="muted">${esc(human(L.industry_profile || "unclassified"))}${where ? " · " + esc(where) : ""}</div>
    <div class="tags"><span class="tag q-${esc(qs)}">${esc(LABELS.qualification[qs] || human(qs))}</span>
      <span class="tag">${esc(L.operating_status === "open" ? "Open" : ["permanently_closed", "temporarily_closed"].includes(L.operating_status) ? human(L.operating_status) : "Operating status unknown")}</span></div></div>
  <div class="section"><h3>Contact</h3>${contactRows(p.contacts, "phone", "Phone")}${contactRows(p.contacts, "email", "Email")}${websiteRows(p.online_profiles)}</div>
  <div class="section"><h3>Scores</h3><div class="scores">
    <div class="scorecard"><div class="label">Lead priority (LPS)</div><div class="big">${esc(lps?.value ?? "–")}</div>
      <p>How strongly to pursue this lead now: the opportunity, adjusted for how reachable and well-evidenced it is.</p></div>
    <div class="scorecard"><div class="label">Business opportunity (BOS)</div><div class="big">${esc(bos?.value ?? "–")}</div>
      <p>How attractive this business is as a website customer. ${bos?.stage === "provisional" ? "Provisional: based on listing data until website research runs." : ""}${bos?.caps_applied?.length ? " Capped by: " + esc(bos.caps_applied.join(", ")) + "." : ""}</p>${topFactors(bos)}</div>
    <div class="scorecard wqs"><div class="label">Website quality (WQS)</div><p>Not analysed yet - website analysis is a later stage. Unknown is not the same as poor.</p></div>
  </div></div>
  <div class="section"><h3>Why contact</h3>${(q?.reasons || []).length ? `<ul class="reasons">${q.reasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul>` : '<p class="muted">No qualification reasons recorded.</p>'}</div>
  <details class="advanced"><summary>Advanced details (sources, evidence, scoring)</summary>
    <div class="section"><h3>Addresses</h3>${table(p.addresses, [["Role", "role"], ["Address", r => [r.line1, r.line2, r.city, r.state, r.zip5].filter(Boolean).join(", ")], ["Geocode", "geocode_method"], ["Operating", r => r.is_operating_location ? "yes" : (r.not_operating_reason || "no")]])}</div>
    <div class="section"><h3>Registry filings</h3>${table(p.organizations, [["Kind", "filing_kind"], ["Document", "document_number"], ["Name", "name"], ["Filed", "registration_date"], ["Role", "role"], ["Status", "status"]])}</div>
    <div class="section"><h3>BOS factors</h3>${table(bos?.factors, [["Component", "component"], ["Factor", "factor"], ["Effect", "effect", "num"], ["Provenance", "provenance"]])}</div>
    <div class="section"><h3>LPS factors</h3>${table(lps?.factors, [["Component", "component"], ["Factor", "factor"], ["Effect", "effect", "num"]])}</div>
    <div class="section"><h3>Keyword hits</h3>${table(p.keyword_hits, [["Group", "group"], ["Matched", "matched_text"], ["Field", "field"], ["Effect", r => r.neutralized ? "neutralized" : r.effect]])}</div>
    <div class="section"><h3>Facts</h3>${table(p.facts, [["Field", "field"], ["Value", r => typeof r.value === "object" ? JSON.stringify(r.value) : r.value], ["Source", "source_key"], ["State", "verification_state"], ["Observed", "observed_at"]])}</div>
    <div class="section"><h3>Source records</h3>${table(p.sources, [["Source", "source_key"], ["Record", "external_id"], ["Match", r => `${r.match_method ?? ""} ${r.match_score ?? ""}`], ["File", "file"], ["Line", "line_no"]])}</div>
    <div class="section"><h3>Aliases</h3>${table(p.aliases, [["Alias", "alias"], ["Kind", "kind"]])}</div>
    <div class="section"><h3>Merges</h3>${table(p.merges, [["Action", "action"], ["Survivor", "survivor_id"], ["Merged", "merged_id"], ["Method", "method"], ["When", "at"]])}</div>
    <div class="section"><h3>Identifiers</h3>${table([L], [["Location id", "id"], ["Normalized name", "name_norm"], ["County FIPS", "county_fips"], ["Category", "category"], ["First seen", "first_seen_at"], ["Last seen", "last_seen_at"]])}</div>
  </details>`;
}
async function select(id, fromList) {
  state.selected = id; writeHash();
  $$("#list .item").forEach(el => el.setAttribute("aria-selected", +el.dataset.id === id));
  const it = state.items.find(i => i.id === id);
  if (map && mapReady && it?.lon != null && fromList) map.flyTo({center: [it.lon, it.lat], zoom: 15});
  $("#detail").innerHTML = '<p class="muted pad">Loading…</p>';
  try { renderDetail(await api(`/api/prospects/${id}`)); }
  catch (e) { $("#detail").innerHTML = `<p class="pad"><strong>Could not load this business.</strong><br>${esc(e.message)}</p>`; }
}

// ---------------------------------------------------------------- coverage banner
let noData = false;
async function loadCoverage() {
  const banner = $("#coverage");
  try {
    const c = await api("/api/coverage/latest");
    if (!c.run) {
      noData = true;
      banner.innerHTML = "<strong>No prospect data yet.</strong> Run <strong>Load Data</strong> (Start menu &rsaquo; LeadIntel &rsaquo; Load Data) to download public business data for the target counties, then refresh this page.";
      return;
    }
    const bad = c.coverage.filter(x => x.state !== "complete");
    const when = c.run.finished_at || c.run.started_at;
    const date = when ? new Date(when).toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}) : "";
    const label = {overture: "business listings", sunbiz: "new business registrations (Sunbiz)", dbpr: "licence records"};
    const missing = [...new Set(bad.map(x => label[x.source] || x.source))];
    banner.title = `Run #${c.run.id} (${c.run.status})` + (bad.length ? "\n" + bad.map(x => `${x.source}/${x.area}: ${x.state}${x.reason ? " - " + x.reason : ""}`).join("\n") : "");
    if (c.run.status === "running") banner.innerHTML = `<strong>Updating data…</strong> (started ${esc(date)}). Refresh this page when it has finished.`;
    else if (c.run.status === "failed" || c.run.status === "interrupted")
      banner.innerHTML = `The last data update (${esc(date)}) did not finish. Everything loaded before it is kept; it will be retried at the next data update.`;
    else if (!bad.length) banner.textContent = `Data updated ${date}.`;
    else banner.innerHTML = `Data updated ${esc(date)}. Some ${esc(missing.join(" and "))} could not be loaded (a data source was busy or unavailable). They will be retried at the next data update.`;
  } catch (e) { banner.textContent = "Data status unavailable: " + e.message; }
}

// ---------------------------------------------------------------- theme, layout, full screen, export
function applyTheme(t) {
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
  $("#theme").value = t;
}
function applyView(v) {
  state.view = v; $("#layout").dataset.view = v; $("#view").value = v;
  storage("leadintel.view", v);
  if (map) setTimeout(() => map.resize(), 50);
  if (v !== "list" && state.loaded) loadPoints(state.seq);
}
function syncFullscreen() {
  const on = !!document.fullscreenElement;
  $("#fullscreen").setAttribute("aria-pressed", on);
  $("#fullscreen").title = on ? "Leave full screen (Esc)" : "Full screen (F11 or Esc to leave)";
}
async function exportCsv() {
  const s = state.summary || {};
  const limit = 100000;
  if (s.total > limit && !confirm(`${fmt(s.total)} businesses match. The export is limited to the first ${fmt(limit)} (in the current sort order). Continue?`)) return;
  const a = document.createElement("a");
  a.href = `/api/prospects/export.csv?${query()}`; a.download = ""; document.body.append(a); a.click(); a.remove();
  toast(`Exporting ${fmt(Math.min(s.total ?? 0, limit))} businesses…`);
}

// ---------------------------------------------------------------- wiring
function wire() {
  $("#filters").addEventListener("click", onChip);
  for (const k of ["phone", "email", "sort"]) $(`#${k}`).addEventListener("change", e => { state.filters[k] = e.target.value; refresh(true); });
  for (const k of ["lps_min", "bos_min"]) $(`#${k}`).addEventListener("change", e => {
    const v = e.target.value === "" ? "" : Math.max(0, Math.min(100, Number(e.target.value)));
    e.target.value = v; state.filters[k] = v === "" ? "" : String(v); refresh(true);
  });
  let t; $("#q").addEventListener("input", e => { clearTimeout(t); t = setTimeout(() => { state.filters.q = e.target.value.trim(); refresh(true); }, 300); });
  $("#clear").addEventListener("click", clearFilters);
  $("#prev").addEventListener("click", () => { if (state.page > 1) { state.page--; refresh(false); $("#list").scrollTop = 0; } });
  $("#next").addEventListener("click", () => { state.page++; refresh(false); $("#list").scrollTop = 0; });
  $("#list").addEventListener("click", e => { const b = e.target.closest(".item"); if (b) select(+b.dataset.id, true); });
  $("#list").addEventListener("keydown", e => {
    const items = $$("#list .item"), i = items.indexOf(document.activeElement);
    if (e.key === "ArrowDown" && i < items.length - 1) { items[i + 1].focus(); e.preventDefault(); }
    if (e.key === "ArrowUp" && i > 0) { items[i - 1].focus(); e.preventDefault(); }
  });
  $("#detail").addEventListener("click", e => { const b = e.target.closest("[data-copy]"); if (b) copy(b.dataset.copy); });
  $("#theme").addEventListener("change", e => { applyTheme(e.target.value); storage("leadintel.theme", e.target.value); });
  $("#view").addEventListener("change", e => applyView(e.target.value));
  $("#export").addEventListener("click", exportCsv);
  $("#fullscreen").addEventListener("click", () => document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen().catch(() => toast("Full screen is not available here - try F11")));
  document.addEventListener("fullscreenchange", syncFullscreen);
  document.addEventListener("keydown", e => {
    if (e.key === "/" && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) { e.preventDefault(); $("#q").focus(); }
  });
}

async function main() {
  applyTheme(storage("leadintel.theme") || "system");
  readHash();
  wire();
  initMap();
  applyView(storage("leadintel.view") || (window.innerWidth < 900 ? "list" : "split"));
  try { state.options = await api("/api/filters/options"); } catch { state.options = null; }
  renderFilters();
  await loadCoverage();
  await refresh(false);
  if (state.selected) select(state.selected, false);
}
main();
