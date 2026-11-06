// LeadIntel interface (M1, local CRM from M2). Plain JavaScript, no build step. All data shown here comes from untrusted
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
  sales: {new: "New", researching: "Researching", contacted: "Contacted", follow_up: "Follow up", interested: "Interested",
          meeting: "Meeting", proposal: "Proposal", won: "Won", lost: "Lost", not_a_fit: "Not a fit"},
};
const DEFAULTS = {qualification: ["provisionally_qualified", "needs_research"], operating: ["open", "unknown"]};
const MULTI = ["county", "niche", "qualification", "website", "operating", "sales"];
const SINGLE = ["q", "phone", "email", "lps_min", "bos_min", "sort", "claim", "follow_up", "list_id"];
const SALES_SELECTS = ["claim", "follow_up", "list_id"];

const state = {filters: {}, page: 1, selected: null, options: null, view: "split", items: [], seq: 0,
               crm: {available: false, configured: false, me: null}, lists: [], sales: null};

// ---------------------------------------------------------------- helpers
async function api(path, opts = {}) {
  if (opts.json !== undefined) opts = {...opts, body: JSON.stringify(opts.json), headers: {...(opts.headers || {}), "Content-Type": "application/json"}};
  const r = await fetch(path, {...opts, headers: {...(opts.headers || {}), ...(opts.method && opts.method !== "GET" ? {"X-LeadIntel-Token": TOKEN} : {})}});
  if (!r.ok) {
    const detail = (await r.json().catch(() => ({}))).detail;
    const err = new Error(typeof detail === "string" ? detail : `${r.status} ${r.statusText}`);
    err.status = r.status; throw err;
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
function query(extra = {}, {area = true} = {}) {
  const p = new URLSearchParams();
  for (const k of MULTI) if (state.filters[k]?.length) p.set(k, state.filters[k].join(","));
  for (const k of SINGLE) if (state.filters[k] !== undefined && state.filters[k] !== "") p.set(k, state.filters[k]);
  if (area) for (const [k, v] of Object.entries(areaParams())) p.set(k, v);   // every API request carries the area
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
  const near = (h.get("near") || "").split(",").map(Number), a = {lat: near[0], lon: near[1], r: roundM(Number(h.get("r"))),
    unit: h.get("ru") === "km" ? "km" : "mi", label: (h.get("nl") || "").slice(0, 120)};
  state.area = h.has("near") && near.length === 2 && areaValid(a) ? a : null;   // an invalid area in a URL is ignored
  if (!state.area && f.sort === "distance") delete f.sort;
}
function writeHash(push = false) {
  const p = new URLSearchParams(query({}, {area: false}));
  const a = state.area;
  if (a) {
    p.set("near", `${a.lat},${a.lon}`); p.set("r", String(a.r)); p.set("ru", a.unit);   // r is already exact to 0.01 m
    if (a.label) p.set("nl", a.label);
  }
  if (state.page > 1) p.set("page", state.page);
  if (state.selected) p.set("id", state.selected);
  if (!p.toString()) p.set("all", "1");                         // an explicit "no filters" view
  const next = "#" + p.toString();
  if (next === location.hash) return;
  if (push) history.pushState(null, "", next); else history.replaceState(null, "", next);   // push: Back undoes it
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
  for (const k of ["qualification", "website", "operating", "sales"])
    $(`#f-${k}`).innerHTML = Object.entries(LABELS[k]).map(([v, l]) => chip(k, v, l)).join("");
  const crmOn = state.crm.available;
  $("#g-sales").hidden = !crmOn;
  $$("#sort [data-crm]").forEach(o => o.hidden = !crmOn);
  $("#list_id").innerHTML = '<option value="">Any</option>' + state.lists.filter(l => !l.archived || String(l.id) === state.filters.list_id)
    .map(l => `<option value="${esc(l.id)}">${esc(l.name)}${l.kind === "campaign" ? " (campaign)" : ""}</option>`).join("");
  for (const k of ["phone", "email", "sort", ...SALES_SELECTS]) $(`#${k}`).value = state.filters[k] || (k === "sort" ? "lps" : "");
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
    state.items = d.items; state.loaded = true; state.lastList = {d, s};
    renderList(d, s); renderAreaCounts();
    if (state.view !== "list") loadPoints(seq);
    if (state.selected && !d.items.some(i => i.id === state.selected)) select(state.selected, false);
  } catch (e) {
    if (seq !== state.seq) return;
    $("#list").innerHTML = `<div class="state"><strong>Could not load prospects.</strong><br>${esc(e.message)}</div>`;
    $("#counts").textContent = ""; $("#pageinfo").textContent = "";
    if (state.area) $("#area-count").textContent = "Could not count the businesses in this area.";
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
      ${i.operating_status && i.operating_status !== "open" ? `<span class="tag">${esc(human(i.operating_status))}</span>` : ""}${salesTags(i)}${
      i.distance_m != null ? `<span class="tag dist" title="Distance from the search-area center">${esc(fmtDist(i.distance_m))}</span>` : ""}${
      i.approximate_location ? '<span class="tag" title="Location known only to ZIP-code level">Approximate location</span>' : ""}</div>
  </button>`;
}
function renderList(d, s) {
  const start = d.total ? (state.page - 1) * PAGE_SIZE + 1 : 0, end = (state.page - 1) * PAGE_SIZE + d.items.length;
  $("#counts").textContent = d.total ? `${fmt(start)}–${fmt(end)} of ${fmt(d.total)} businesses` : "No matching businesses";
  $("#pageinfo").textContent = d.total ? `Page ${fmt(state.page)} of ${fmt(d.pages)}` : "";
  $("#prev").disabled = state.page <= 1; $("#next").disabled = !d.has_more;
  if (!d.items.length) {
    const areaMsg = state.area && `<div class="state"><strong>No businesses match these filters inside this search area.</strong><br>Try a larger radius, or <button class="link" id="area-x2">remove the search area</button>.${s?.area?.excluded_no_location ? `<br>${fmt(s.area.excluded_no_location)} matching businesses have no location and cannot be placed in an area.` : ""}</div>`;
    $("#list").innerHTML = areaMsg || (noData
      ? '<div class="state"><strong>No prospect data yet.</strong><br>Run <strong>Load Data</strong> from the Start menu (LeadIntel folder), then refresh this page.</div>'
      : '<div class="state"><strong>No businesses match these filters.</strong><br><button class="link" id="clear2">Clear all filters</button></div>');
    $("#clear2")?.addEventListener("click", clearFilters);
    $("#area-x2")?.addEventListener("click", () => setArea(null));
  } else {
    $("#list").innerHTML = d.items.map(itemHtml).join("");
  }
  state.summary = s;
}
function clearFilters() {                                     // Clear all also removes the search area
  state.filters = {};
  if (state.area) { state.area = null; syncAreaOnMap(); renderArea(); }
  renderFilters(); writeHash(true); refresh(true);
}

// ---------------------------------------------------------------- map
function drawMap() {
  const p = mapData; if (!p) return;
  const missing = p.without_coordinates ? ` · ${fmt(p.without_coordinates)} have no location` : "";
  const inside = state.area ? " inside the search area" : "";
  const excl = p.area?.excluded_no_location ? ` · ${fmt(p.area.excluded_no_location)} without a location not included` : "";
  $("#mapcaption").textContent = `Map: ${fmt(p.returned)} of ${fmt(p.total)} matching businesses${inside}${state.area ? excl : missing}` +
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
  map.on("click", "pts", e => { if (!areaPicking) select(e.features[0].properties.id, false); });
  map.on("click", "clusters", e => { if (!areaPicking) map.easeTo({center: e.features[0].geometry.coordinates, zoom: map.getZoom() + 2}); });
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
    map.on("load", () => { mapReady = true; drawMap(); syncAreaOnMap({fit: true}); });
    map.on("click", onMapClickForArea);                       // only acts while "Pick on map" is on; panning never moves the center
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
  ${state.crm.available ? '<div class="section sales" id="sales-sec"><h3>Sales</h3><p class="muted">Loading…</p></div>' : ""}
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
  try { renderDetail(await api(`/api/prospects/${id}`)); if (state.crm.available) loadSales(id); }
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

// ---------------------------------------------------------------- local CRM (M2)
// Everything typed here (notes, next actions, list names) is shown back through esc() like any other data.
const ACT_LABELS = {note: "Note", call: "Call", email: "Email", meeting: "Meeting", visit: "Visit", claim: "Claimed",
  release: "Released", reassign: "Reassigned", status: "Status", next_action: "Next action", follow_up: "Follow-up",
  list_add: "List", list_remove: "List"};
const OUTCOMES = ["Reached", "No answer", "Left a message", "Interested", "Not interested", "Asked to call back"];
const pad2 = n => String(n).padStart(2, "0");
const fmtDate = iso => iso ? new Date(iso).toLocaleDateString([], {weekday: "short", month: "short", day: "numeric"}) : "";
const fmtWhen = iso => iso ? new Date(iso).toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}) : "";
const dateInput = iso => { if (!iso) return ""; const d = new Date(iso); return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`; };
const fromDateInput = v => { if (!v) return null; const [y, m, d] = v.split("-").map(Number); return new Date(y, m - 1, d, 9).toISOString(); };
function dueKind(iso) {
  if (!iso) return null;
  const t = new Date(); t.setHours(0, 0, 0, 0);
  const d = new Date(iso);
  return d < t ? "overdue" : d < new Date(t.getTime() + 864e5) ? "today" : "upcoming";
}
const canManage = () => ["owner", "admin"].includes(state.crm.me?.role);

function salesTags(i) {
  if (!("sales_status" in i)) return "";
  let h = "";
  if (i.sales_status && i.sales_status !== "new") h += `<span class="tag s-${esc(i.sales_status)}">${esc(LABELS.sales[i.sales_status] || human(i.sales_status))}</span>`;
  if (i.owned_by_me) h += '<span class="tag mine">Mine</span>';
  else if (i.owner_initials) h += `<span class="tag">Claimed · ${esc(i.owner_initials)}</span>`;
  const due = dueKind(i.follow_up_at);
  if (due) h += `<span class="tag due-${due}">${due === "overdue" ? "Follow-up overdue" : due === "today" ? "Follow up today" : "Follow up " + esc(fmtDate(i.follow_up_at))}</span>`;
  return h;
}

async function loadCrm() {
  try { state.crm = await api("/api/crm/me"); } catch { state.crm = {available: false, configured: false, me: null}; }
  $("#sales").hidden = $("#profile-btn").hidden = !state.crm.available;
  if (!state.crm.available) return;
  $("#profile-btn").textContent = state.crm.me?.initials || "👤";
  try { state.lists = await api("/api/crm/lists"); } catch { state.lists = []; }
  updateSalesButton();
}
async function updateSalesButton() {
  if (!state.crm.configured) { $("#sales").textContent = "Sales"; return; }
  try {
    const d = await api("/api/crm/dashboard");
    const due = (d.overdue || 0) + (d.due_today || 0);
    $("#sales").textContent = due ? `Sales · ${due} due` : "Sales";
    $("#sales").classList.toggle("attention", !!d.overdue);
  } catch { /* the button still opens the dashboard, which reports the error */ }
}

// profile
let profileWaiters = [];
function openProfile() {
  const f = $("#profile-form"), me = state.crm.me || {};
  f.display_name.value = me.display_name || ""; f.initials.value = me.initials || ""; f.email.value = me.email || "";
  $("#write-mode").textContent = state.crm.write_mode === "independent"
    ? "Sales changes: also saved while Load Data runs (separate, restricted database account)."
    : "Sales changes: paused while Load Data runs (shared database lock).";
  $("#profile").showModal(); f.display_name.focus();
}
function ensureProfile() {
  if (state.crm.configured) return Promise.resolve(true);
  openProfile();
  return new Promise(res => profileWaiters.push(res));
}
async function saveProfile(e) {
  e.preventDefault();
  const f = e.target;
  try {
    state.crm = await api("/api/crm/me", {method: "POST", json: {display_name: f.display_name.value.trim(),
      initials: f.initials.value.trim(), email: f.email.value.trim() || null}});
    $("#profile").close(); $("#profile-btn").textContent = state.crm.me.initials;
    toast("Profile saved");
    profileWaiters.splice(0).forEach(r => r(true));
    if (state.selected) loadSales(state.selected);
    updateSalesButton();
  } catch (err) { toast(err.message); }
}

// sales section of a business
async function loadSales(id) {
  try {
    const st = await api(`/api/crm/prospects/${id}`);
    if (state.selected !== id) return;
    state.sales = {id, ...st}; renderSales();
  } catch (e) {
    const sec = $("#sales-sec"); if (sec) sec.innerHTML = `<h3>Sales</h3><p class="muted">Sales information unavailable: ${esc(e.message)}</p>`;
  }
}
function actText(a) {
  const d = a.data || {}, lab = v => LABELS.sales[v] || human(v);
  switch (a.kind) {
    case "claim": return "Claimed this business";
    case "release": return "Released the claim";
    case "reassign": return "Reassigned the business";
    case "status": return `Status: ${lab(d.from)} → ${lab(d.to)}`;
    case "next_action": return a.body ? `Next action: ${a.body}` : "Next action cleared";
    case "follow_up": return d.at ? `Follow-up set for ${fmtDate(d.at)}` : "Follow-up date cleared";
    case "list_add": return `Added to "${d.list}"`;
    case "list_remove": return `Removed from "${d.list}"`;
    default: return (ACT_LABELS[a.kind] || human(a.kind)) + (d.outcome ? ` · ${d.outcome}` : "");
  }
}
const USER_KINDS = ["note", "call", "email", "meeting", "visit"];
function timelineHtml(acts) {
  if (!acts.length) return '<p class="muted">No activity yet.</p>';
  return `<ol class="timeline">${acts.map(a => `<li class="${USER_KINDS.includes(a.kind) ? "user" : "sys"}">
    <div class="tl-head"><b>${esc(actText(a))}</b><span class="muted">${esc(fmtWhen(a.occurred_at))} · ${esc(a.initials)}</span></div>
    ${USER_KINDS.includes(a.kind) && a.body ? `<div class="tl-body">${esc(a.body)}</div>` : ""}</li>`).join("")}</ol>`;
}
function renderSales() {
  const sec = $("#sales-sec"), st = state.sales;
  if (!sec || !st || st.id !== state.selected) return;
  if (!st.configured) {
    sec.innerHTML = `<h3>Sales</h3><p>Set up your salesperson profile to claim businesses, track the pipeline and keep notes.</p>
      <button type="button" class="btn primary" data-act="profile">Set up profile</button>`;
    return;
  }
  const o = st.owner;
  const who = !o ? '<span class="muted">Not claimed</span>'
    : st.owned_by_me ? `<b>Claimed by you</b> <span class="muted">since ${esc(fmtDate(st.claimed_at))}</span>`
    : `<b>Claimed by ${esc(o.display_name)} (${esc(o.initials)})</b>`;
  const buttons = !o ? '<button type="button" class="btn primary" data-act="claim">Claim</button>'
    : st.owned_by_me ? '<button type="button" class="btn" data-act="release">Release</button>'
    : canManage() ? '<button type="button" class="btn" data-act="takeover">Take over</button> <button type="button" class="btn" data-act="release">Release</button>' : "";
  const readOnly = o && !st.owned_by_me && !canManage();
  const inLists = new Set(st.lists.map(l => l.id));
  const addable = state.lists.filter(l => !l.archived && !inLists.has(l.id));
  sec.innerHTML = `<h3>Sales</h3>
    <div class="claimrow">${who}<span class="grow"></span>${buttons}</div>
    <form id="sales-form" class="salesform" ${readOnly ? 'inert' : ""}>
      <label class="field">Status <select name="status">${Object.entries(LABELS.sales).map(([v, l]) => `<option value="${v}"${v === st.status ? " selected" : ""}>${esc(l)}</option>`).join("")}</select></label>
      <label class="field">Follow-up date <input type="date" name="follow_up" value="${esc(dateInput(st.follow_up_at))}"></label>
      <label class="field wide">Next action <input name="next_action" maxlength="500" value="${esc(st.next_action || "")}" placeholder="e.g. Call the owner about a new website"></label>
      <div class="formend"><button class="btn primary">Save</button></div>
    </form>
    <div class="listsrow"><span class="muted">Lists:</span>
      ${st.lists.map(l => `<span class="lchip">${esc(l.name)}<button type="button" class="link" data-act="unlist" data-list="${esc(l.id)}" aria-label="Remove from ${esc(l.name)}">✕</button></span>`).join("") || '<span class="muted">none</span>'}
      <select data-act="addlist" aria-label="Add to a list"><option value="">Add to list…</option>${addable.map(l => `<option value="${esc(l.id)}">${esc(l.name)}</option>`).join("")}<option value="new">New list…</option></select></div>
    <form id="act-form" class="actform">
      <div class="actrow"><select name="kind" aria-label="Kind">${USER_KINDS.map(k => `<option value="${k}">${esc(ACT_LABELS[k])}</option>`).join("")}</select>
        <select name="outcome" aria-label="Outcome"><option value="">Outcome…</option>${OUTCOMES.map(x => `<option>${esc(x)}</option>`).join("")}</select></div>
      <textarea name="body" maxlength="10000" rows="2" placeholder="What happened? Notes stay in your own database."></textarea>
      <div class="formend"><button class="btn">Add to history</button></div>
    </form>
    ${timelineHtml(st.activities)}`;
}
function patchItem(id, st) {
  const it = state.items.find(i => i.id === id);
  if (!it || !("sales_status" in it)) return;
  Object.assign(it, {sales_status: st.status, follow_up_at: st.follow_up_at, next_action: st.next_action,
    owned_by_me: st.owned_by_me, owner_initials: st.owner?.initials || null});
  const el = $(`#list .item[data-id="${id}"]`);
  if (el) el.outerHTML = itemHtml(it);
}
async function salesWrite(path, opts, okMsg) {
  const id = state.selected;
  try {
    const st = await api(path, opts);
    if (state.selected === id) { state.sales = {id, ...st}; renderSales(); }
    patchItem(id, st); updateSalesButton();
    if (okMsg) toast(okMsg);
    return true;
  } catch (e) {
    toast(e.message);
    // After a conflict show the current state; otherwise (e.g. paused during Load Data) keep what was typed.
    if (e.status === 409 && state.selected === id) loadSales(id);
    return false;
  }
}
async function onSalesClick(e) {
  const b = e.target.closest("[data-act]"); if (!b || b.tagName === "SELECT") return;
  const id = state.selected, act = b.dataset.act;
  if (act === "profile") return openProfile();
  if (!(await ensureProfile())) return;
  if (act === "claim") return salesWrite(`/api/crm/prospects/${id}/claim`, {method: "POST"}, "Claimed");
  if (act === "release") return salesWrite(`/api/crm/prospects/${id}/release`, {method: "POST"}, "Released");
  if (act === "takeover") return salesWrite(`/api/crm/prospects/${id}/reassign`, {method: "POST", json: {user_id: state.crm.me.user_id}}, "Now yours");
  if (act === "unlist") return salesWrite(`/api/crm/lists/${b.dataset.list}/items/${id}`, {method: "DELETE"}, "Removed from list");
}
async function onSalesChange(e) {
  const sel = e.target.closest('select[data-act="addlist"]'); if (!sel) return;
  let listId = sel.value; sel.value = "";
  if (!listId || !(await ensureProfile())) return;
  if (listId === "new") {
    const name = (prompt("Name of the new list (e.g. \"Lawn care - week 1\"):") || "").trim();
    if (!name) return;
    try { listId = (await api("/api/crm/lists", {method: "POST", json: {name, kind: "list"}})).id; }
    catch (err) { return toast(err.message); }
    await reloadLists();
  }
  salesWrite(`/api/crm/lists/${listId}/items`, {method: "POST", json: {location_id: state.selected}}, "Added to list");
}
async function onSalesSubmit(e) {
  const f = e.target.closest("form"); if (!f || !["sales-form", "act-form"].includes(f.id)) return;
  e.preventDefault();
  if (!(await ensureProfile())) return;
  const id = state.selected, st = state.sales;
  if (f.id === "sales-form") {
    const body = {version: st.version};
    if (f.status.value !== st.status) body.status = f.status.value;
    if (f.next_action.value.trim() !== (st.next_action || "")) body.next_action = f.next_action.value.trim();
    if (f.follow_up.value !== dateInput(st.follow_up_at)) body.follow_up_at = fromDateInput(f.follow_up.value);
    if (Object.keys(body).length === 1) return toast("Nothing to save");
    return salesWrite(`/api/crm/prospects/${id}`, {method: "PATCH", json: body}, "Saved");
  }
  const kind = f.kind.value, text = f.body.value.trim();
  if (kind === "note" && !text) return toast("Write the note first");
  const ok = await salesWrite(`/api/crm/prospects/${id}/activities`, {method: "POST",
    json: {kind, body: text || null, outcome: f.outcome.value || null}}, "Added to history");
  if (ok && $("#act-form")) $("#act-form").body.value = "";
}
async function reloadLists() {
  try { state.lists = await api("/api/crm/lists"); } catch { /* keep the old list */ }
  const cur = $("#list_id").value; renderFilters(); $("#list_id").value = cur;
}

// dashboard
async function openDash() {
  $("#dash").showModal();
  if (!state.crm.configured) {
    $("#dash-body").innerHTML = '<p>Set up your salesperson profile to start claiming businesses.</p><button type="button" class="btn primary" data-dash="profile">Set up profile</button>';
    return;
  }
  $("#dash-body").innerHTML = '<p class="muted">Loading…</p>';
  try {
    const [d] = await Promise.all([api("/api/crm/dashboard"), reloadLists()]);
    renderDash(d);
  } catch (e) { $("#dash-body").innerHTML = `<p><strong>Could not load the sales overview.</strong><br>${esc(e.message)}</p>`; }
}
function renderDash(d) {
  const due = {overdue: "Overdue", today: "Today", upcoming: "Soon"};
  $("#dash-body").innerHTML = `
    <div class="stats"><div><b>${fmt(d.claimed)}</b><span>claimed by you</span></div>
      <div class="${d.overdue ? "bad" : ""}"><b>${fmt(d.overdue)}</b><span>follow-ups overdue</span></div>
      <div><b>${fmt(d.due_today)}</b><span>due today</span></div></div>
    <h3>Your pipeline</h3>
    <div class="pipeline">${Object.entries(LABELS.sales).map(([s, l]) => `<button type="button" class="pstage s-${s}" data-dash="status" data-status="${s}"${d.pipeline[s] ? "" : " disabled"}><b>${fmt(d.pipeline[s])}</b><span>${esc(l)}</span></button>`).join("")}</div>
    <h3>Follow-ups (next 7 days)</h3>
    ${d.follow_ups.length ? `<ul class="dlist">${d.follow_ups.map(f => `<li><button type="button" class="drow" data-dash="open" data-id="${esc(f.location_id)}">
      <span class="tag due-${esc(f.due)}">${esc(due[f.due])} · ${esc(fmtDate(f.follow_up_at))}</span> <b>${esc(f.name)}</b>
      <span class="muted">${esc(LABELS.sales[f.status] || f.status)}${f.next_action ? " · " + esc(f.next_action) : ""}</span></button></li>`).join("")}</ul>`
      : '<p class="muted">Nothing due. Set a follow-up date on a business to see it here.</p>'}
    <h3>Recent activity</h3>
    ${d.recent.length ? `<ul class="dlist">${d.recent.map(a => `<li><button type="button" class="drow" data-dash="open" data-id="${esc(a.location_id)}">
      <span class="muted">${esc(fmtWhen(a.occurred_at))} · ${esc(a.initials)}</span> <b>${esc(a.name)}</b> <span>${esc(actText(a))}</span></button></li>`).join("")}</ul>`
      : '<p class="muted">No activity yet.</p>'}
    <h3>Lists and campaigns</h3>
    ${state.lists.length ? `<ul class="dlist">${state.lists.map(l => `<li class="lrow${l.archived ? " archived" : ""}"><b>${esc(l.name)}</b>
      <span class="muted">${l.kind === "campaign" ? "Campaign" : "List"} · ${fmt(l.size)} business${l.size === 1 ? "" : "es"}${l.archived ? " · archived" : ""}</span><span class="grow"></span>
      <button type="button" class="btn" data-dash="show" data-list="${esc(l.id)}">Show</button>
      <button type="button" class="btn" data-dash="archive" data-list="${esc(l.id)}" data-archived="${l.archived}">${l.archived ? "Restore" : "Archive"}</button></li>`).join("")}</ul>`
      : '<p class="muted">No lists yet.</p>'}
    <form id="newlist" class="newlist"><input name="name" maxlength="120" required placeholder="New list or campaign name" aria-label="New list name">
      <select name="kind" aria-label="Kind"><option value="list">List</option><option value="campaign">Campaign</option></select>
      <button class="btn">Create</button></form>`;
}
function showFiltered(filters) {
  $("#dash").close();
  state.filters = filters; renderFilters(); refresh(true);
}
async function onDashClick(e) {
  const b = e.target.closest("[data-dash]"); if (!b) return;
  const k = b.dataset.dash;
  if (k === "profile") { $("#dash").close(); return openProfile(); }
  if (k === "status") return showFiltered({claim: "mine", sales: [b.dataset.status], sort: "follow_up"});
  if (k === "show") return showFiltered({list_id: b.dataset.list});
  if (k === "open") { $("#dash").close(); return select(+b.dataset.id, true); }
  if (k === "archive") {
    try { await api(`/api/crm/lists/${b.dataset.list}`, {method: "PATCH", json: {archived: b.dataset.archived !== "true"}}); }
    catch (err) { return toast(err.message); }
    return openDash();
  }
}
async function onDashSubmit(e) {
  if (e.target.id !== "newlist") return;
  e.preventDefault();
  const f = e.target;
  try { await api("/api/crm/lists", {method: "POST", json: {name: f.name.value.trim(), kind: f.kind.value}}); toast("List created"); }
  catch (err) { return toast(err.message); }
  openDash();
}

function wireCrm() {
  $("#detail").addEventListener("click", onSalesClick);
  $("#detail").addEventListener("change", onSalesChange);
  $("#detail").addEventListener("submit", onSalesSubmit);
  $("#profile-form").addEventListener("submit", saveProfile);
  $("#profile").addEventListener("close", () => profileWaiters.splice(0).forEach(r => r(state.crm.configured)));
  $("#profile-btn").addEventListener("click", openProfile);
  $("#sales").addEventListener("click", openDash);
  $("#dash").addEventListener("click", onDashClick);
  $("#dash").addEventListener("submit", onDashSubmit);
  $$("dialog [data-close]").forEach(b => b.addEventListener("click", () => b.closest("dialog").close()));
}

// ---------------------------------------------------------------- search area (M3, PLAN.md §14.2)
// The area is kept apart from the other filters: changing it never resets them. It is part of every request
// (query()), so the list, counts, summary, map points and export all use it. The circle is ALWAYS the server's
// (/api/geo/circle): nothing here computes a boundary of its own.
const AREA_UNIT_M = {mi: 1609.344, km: 1000};
// One canonical range in metres (same as the server: 400 m to 100 mi); each unit's limits are derived from it.
const AREA_MIN_M = 400, AREA_MAX_M = 100 * 1609.344, AREA_DEFAULT_M = 10 * 1609.344;
const areaLimits = u => [AREA_MIN_M / AREA_UNIT_M[u], AREA_MAX_M / AREA_UNIT_M[u]];
const fmtLimit = v => String(+v.toFixed(2));          // display only: 0.25–100 mi, 0.4–160.93 km
const MAP_START = [-81.6, 30.2];
let areaMarker = null, areaPicking = false, areaSeq = 0, areaCircle = null, areaCircleError = "", areaCommitTimer = null;

function areaValid(a) {
  return a && [a.lat, a.lon, a.r].every(Number.isFinite) && Math.abs(a.lat) <= 85 && Math.abs(a.lon) <= 180 &&
    a.r >= AREA_MIN_M && a.r <= AREA_MAX_M + 1e-6 && (a.unit === "mi" || a.unit === "km");
}
function areaParams() {
  const a = state.area;
  return a ? {near_lat: a.lat, near_lon: a.lon, radius: a.r / AREA_UNIT_M[a.unit], unit: a.unit} : {};
}
function fmtRadius(m, unit) {
  const v = m / AREA_UNIT_M[unit];
  return `${v >= 10 ? Math.round(v * 10) / 10 : Math.round(v * 100) / 100} ${unit}`;
}
function fmtDist(m) {
  const unit = state.area?.unit || "mi", v = m / AREA_UNIT_M[unit];
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${unit}`;
}
const roundM = m => Math.round(m * 100) / 100;   // radius kept to 0.01 m: state, URL and server always agree exactly
const sliderToM = v => AREA_MIN_M * Math.pow(AREA_MAX_M / AREA_MIN_M, v / 1000);
const mToSlider = m => Math.round(1000 * Math.log(m / AREA_MIN_M) / Math.log(AREA_MAX_M / AREA_MIN_M));

function renderArea() {
  const a = state.area, on = !!a;
  $("#area-off").hidden = on; $("#area-panel").hidden = !on; $("#areachip").hidden = !on;
  $$("#sort [data-area]").forEach(o => o.hidden = !on);
  if (!on) { setPicking(false); return; }
  $("#area-label").textContent = `Center: ${a.label || `${a.lat.toFixed(4)}, ${a.lon.toFixed(4)}`}`;
  $("#area-unit").value = a.unit;
  const [lo, hi] = areaLimits(a.unit);
  const num = $("#area-num");
  num.min = lo; num.max = hi; num.step = "any";      // exact derived limits: a valid radius is never shown as invalid
  if (document.activeElement !== num) num.value = +(a.r / AREA_UNIT_M[a.unit]).toFixed(2);
  $("#area-slider").value = mToSlider(a.r);
  $("#area-limits").textContent = `${fmtLimit(lo)}–${fmtLimit(hi)} ${a.unit}`;
  renderAreaCounts();
}
function renderAreaCounts() {
  const a = state.area; if (!a) return;
  const s = state.summary, total = s?.total;
  const excluded = s?.area?.excluded_no_location || 0;
  $("#area-count").textContent = total == null ? "Counting…" :
    `${fmt(total)} business${total === 1 ? "" : "es"} inside` + (excluded ? ` · ${fmt(excluded)} matching without a location are not included` : "");
  $("#areachip-text").textContent = `Within ${fmtRadius(a.r, a.unit)} of ${a.label || "the chosen point"}` +
    (total == null ? "" : ` · ${fmt(total)}`);
  $("#areachip").title = $("#areachip-text").textContent;
  const notes = [];
  if (areaCircleError) notes.push(areaCircleError);
  if (areaCircle?.beyond_target_counties) notes.push("Part of this area is outside the counties LeadIntel has data for.");
  $("#area-note").textContent = notes.join(" ");
}

// One entry point for every area change. push: a new browser-history entry (Back returns to the previous area).
function setArea(next, {push = true, fit = false, reload = true} = {}) {
  if (next) next = {...next, r: roundM(next.r)};
  if (next && !areaValid(next)) { toast("That search area is not valid"); return; }
  state.area = next;
  if (!next && state.filters.sort === "distance") state.filters.sort = "";
  writeHash(push);
  renderArea(); renderFilters();
  syncAreaOnMap({fit});
  if (reload) refresh(true);
}
function enableArea() {
  let lon = MAP_START[0], lat = MAP_START[1], label = "Map center";
  if (map && mapReady) { const c = map.getCenter(); lon = c.lng; lat = c.lat; }
  else { const pos = selectedPosition(); if (pos) { [lon, lat] = pos.ll; label = pos.name; } else label = "Starting point"; }
  setArea({lat: +lat.toFixed(6), lon: +wrapLon(lon).toFixed(6), r: AREA_DEFAULT_M, unit: "mi", label}, {fit: true});
}
const wrapLon = lon => ((lon + 540) % 360) - 180;
function selectedPosition() {
  const id = state.selected; if (!id) return null;
  const it = state.items.find(i => i.id === id && i.lon != null);
  if (it) return {ll: [it.lon, it.lat], name: it.name};
  const pt = mapData?.points?.find(p => p[0] === id);
  return pt ? {ll: [pt[1], pt[2]], name: `business #${id}`} : null;
}
function moveCenter(lon, lat, label) {
  if (!state.area) return;
  setArea({...state.area, lat: +lat.toFixed(6), lon: +wrapLon(lon).toFixed(6), label}, {fit: false});
}
function commitRadius(m, delay = 0) {
  if (!state.area) return;
  const r = Math.min(AREA_MAX_M, Math.max(AREA_MIN_M, m));
  clearTimeout(areaCommitTimer);
  areaCommitTimer = setTimeout(() => setArea({...state.area, r}), delay);
}
function setPicking(on) {
  areaPicking = !!on && !!state.area;
  $("#area-pick").setAttribute("aria-pressed", areaPicking);
  $("#area-pick").textContent = areaPicking ? "Click the map… (Esc)" : "Pick on map";
  document.querySelector(".mapwrap")?.classList.toggle("picking", areaPicking);
}

// ---- map side: marker and server circle
function syncAreaOnMap({fit = false} = {}) {
  if (!map || !mapReady) return;
  const a = state.area;
  if (!a) {
    areaMarker?.remove(); areaMarker = null; areaCircle = null; areaCircleError = ""; ++areaSeq;
    if (map.getLayer("area-line")) { map.removeLayer("area-line"); map.removeLayer("area-fill"); }
    if (map.getSource("area")) map.removeSource("area");
    return;
  }
  if (!areaMarker) {
    const el = document.createElement("div");
    el.className = "area-center-marker"; el.title = "Search-area center (drag to move)";
    areaMarker = new maplibregl.Marker({element: el, draggable: true}).setLngLat([a.lon, a.lat]).addTo(map);
    areaMarker.on("dragstart", () => { if (map.getLayer("area-fill")) { map.setPaintProperty("area-fill", "fill-opacity", 0.03); map.setPaintProperty("area-line", "line-opacity", 0.35); } });
    areaMarker.on("dragend", () => { const p = areaMarker.getLngLat(); moveCenter(p.lng, p.lat, "Dropped pin"); });
  } else areaMarker.setLngLat([a.lon, a.lat]);
  loadCircle(fit);
}
async function loadCircle(fit) {
  const seq = ++areaSeq, a = state.area;
  try {
    const c = await api(`/api/geo/circle?${new URLSearchParams(areaParams())}`);
    if (seq !== areaSeq || !state.area) return;                 // a newer center or radius superseded this answer
    areaCircle = c; areaCircleError = ""; renderAreaCounts();
    const data = {type: "Feature", geometry: c.geometry, properties: {}};
    const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
    if (map.getSource("area")) map.getSource("area").setData(data);
    else {
      map.addSource("area", {type: "geojson", data});
      const before = map.getLayer("clusters") ? "clusters" : undefined;   // points stay above the circle
      map.addLayer({id: "area-fill", type: "fill", source: "area", paint: {"fill-color": css("--sel"), "fill-opacity": 0.07}}, before);
      map.addLayer({id: "area-line", type: "line", source: "area", paint: {"line-color": css("--sel"), "line-width": 2, "line-opacity": 0.9}}, before);
    }
    map.setPaintProperty("area-fill", "fill-opacity", 0.07); map.setPaintProperty("area-line", "line-opacity", 0.9);
    if (fit) {
      const xs = c.geometry.coordinates[0].map(p => p[0]), ys = c.geometry.coordinates[0].map(p => p[1]);
      map.fitBounds([[Math.min(...xs), Math.min(...ys)], [Math.max(...xs), Math.max(...ys)]], {padding: 40, duration: 0});
    }
  } catch (e) {
    if (seq !== areaSeq) return;
    areaCircle = null;
    areaCircleError = "The area outline could not be drawn (" + e.message + "); the results still use the area.";
    if (map.getLayer("area-fill")) { map.setPaintProperty("area-fill", "fill-opacity", 0); map.setPaintProperty("area-line", "line-opacity", 0); }
    renderAreaCounts();
  }
}
function onMapClickForArea(e) {
  if (!areaPicking || !state.area) return;
  setPicking(false);
  moveCenter(e.lngLat.lng, e.lngLat.lat, "Picked on map");
}

// ---- finding a center: businesses, ZIP codes, cities (local) and street addresses (on Enter, online)
const FIND_KIND = {business: "Business", zip: "ZIP code", city: "City", address: "Street address"};
let findSeq = 0, findTimer = null, findResults = [];
async function runFind(online) {
  const q = $("#area-find").value.trim(), box = $("#area-results"), seq = ++findSeq;
  clearTimeout(findTimer);
  if (q.length < 2) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  box.innerHTML = `<p class="note">${online ? "Looking up the address…" : "Searching…"}</p>`;
  try {
    const r = await api(`/api/geo/search?${new URLSearchParams({q, online: online ? "true" : "false"})}`);
    if (seq !== findSeq) return;                              // a newer search superseded this answer
    findResults = r.results;
    box.innerHTML = r.results.map((x, i) => `<button type="button" class="area-result" role="option" data-i="${i}">
        <b>${esc(x.label)}</b><span class="sub">${esc(FIND_KIND[x.kind] || x.kind)} · ${esc(x.precision)}${
        x.in_target_counties === false ? ' · <span class="warn">outside the counties LeadIntel has data for</span>' : ""}</span></button>`).join("") +
      r.notes.map(n => `<p class="note">${esc(n)}</p>`).join("") +
      (!r.results.length && !r.notes.length ? '<p class="note">Nothing found. Try a ZIP code, a city, or press Enter for a street address.</p>' : "");
  } catch (e) {
    if (seq !== findSeq) return;
    box.innerHTML = `<p class="note">Search failed: ${esc(e.message)}</p>`;
  }
}
function useFound(i) {
  const x = findResults[i]; if (!x) return;
  const base = state.area || {r: AREA_DEFAULT_M, unit: "mi"};
  setArea({...base, lat: +x.lat.toFixed(6), lon: +wrapLon(x.lon).toFixed(6), label: x.label}, {fit: true});
  $("#area-results").hidden = true; ++findSeq;
  $("#area-find").value = "";
}

function wireArea() {
  const find = $("#area-find"), results = $("#area-results");
  find.addEventListener("input", () => { clearTimeout(findTimer); findTimer = setTimeout(() => runFind(false), 300); });
  find.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); runFind(true); }
    if (e.key === "Escape") { results.hidden = true; ++findSeq; }
    if (e.key === "ArrowDown") { results.querySelector(".area-result")?.focus(); e.preventDefault(); }
  });
  results.addEventListener("click", e => { const b = e.target.closest(".area-result"); if (b) useFound(+b.dataset.i); });
  results.addEventListener("keydown", e => {
    const items = [...results.querySelectorAll(".area-result")], i = items.indexOf(document.activeElement);
    if (e.key === "ArrowDown" && i < items.length - 1) { items[i + 1].focus(); e.preventDefault(); }
    if (e.key === "ArrowUp") { (i > 0 ? items[i - 1] : find).focus(); e.preventDefault(); }
    if (e.key === "Escape") { results.hidden = true; find.focus(); }
  });
  $("#area-on").addEventListener("click", enableArea);
  $("#area-remove").addEventListener("click", () => setArea(null));
  $("#areachip-x").addEventListener("click", () => setArea(null));
  $("#area-pick").addEventListener("click", () => {
    if (!map || !mapReady) { toast("The map is not available - use “Selected business” instead"); return; }
    setPicking(!areaPicking);
  });
  $("#area-here").addEventListener("click", () => {
    const pos = selectedPosition();
    if (!pos) { toast(state.selected ? "This business has no location on the map" : "Open a business first"); return; }
    moveCenter(pos.ll[0], pos.ll[1], pos.name);
  });
  const slider = $("#area-slider"), num = $("#area-num");
  slider.addEventListener("input", () => {                   // live preview of the number; the search waits for release
    const m = sliderToM(+slider.value), u = state.area.unit;
    num.value = +(m / AREA_UNIT_M[u]).toFixed(2);
    $("#areachip-text").textContent = `Within ${fmtRadius(m, u)} of ${state.area.label || "the chosen point"}`;
  });
  slider.addEventListener("change", () => commitRadius(sliderToM(+slider.value), 150));
  num.addEventListener("change", () => {
    const u = state.area.unit, v = parseFloat(num.value);
    if (!Number.isFinite(v)) { renderArea(); return; }
    let m = v * AREA_UNIT_M[u];                           // clamped only in metres, only when typed outside the range
    if (m < AREA_MIN_M - 1e-6 || m > AREA_MAX_M + 1e-6) {
      m = Math.min(AREA_MAX_M, Math.max(AREA_MIN_M, m));
      const [lo, hi] = areaLimits(u);
      toast(`Radius limited to ${fmtLimit(lo)}–${fmtLimit(hi)} ${u}`);
      num.value = +(m / AREA_UNIT_M[u]).toFixed(2);
    }
    commitRadius(m);
  });
  $("#area-unit").addEventListener("change", e => {          // display only: the radius in metres does not change
    state.area = {...state.area, unit: e.target.value};
    writeHash(false); renderArea();
    if (state.lastList) renderList(state.lastList.d, state.lastList.s);
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && areaPicking) setPicking(false); });
  window.addEventListener("popstate", () => {                // Back / Forward: restore exactly what the URL says
    readHash(); renderFilters(); renderArea(); syncAreaOnMap({fit: false}); refresh(false);
    if (state.selected) select(state.selected, false); else $("#detail").innerHTML = '<p class="muted pad">Select a business to see its details.</p>';
  });
}

// ---------------------------------------------------------------- wiring
function wire() {
  $("#filters").addEventListener("click", onChip);
  for (const k of ["phone", "email", "sort", ...SALES_SELECTS]) $(`#${k}`).addEventListener("change", e => { state.filters[k] = e.target.value; refresh(true); });
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
  wireCrm();
  wireArea();
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
  await loadCrm();
  renderFilters(); renderArea();
  await loadCoverage();
  await refresh(false);
  if (state.selected) select(state.selected, false);
}
main();
