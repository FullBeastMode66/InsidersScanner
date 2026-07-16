/* Insider / Politician Scanner — client logic.
   Talks to the FastAPI backend (api.py) on the same origin. */

const API = {
  health: () => fetch("/api/health").then((r) => r.json()),
  signals: (p) => fetch("/api/signals?" + new URLSearchParams(p)).then((r) => r.json()),
  scan: () => fetch("/api/scan", { method: "POST" }).then((r) => r.json()),
  status: () => fetch("/api/scan/status").then((r) => r.json()),
};

const state = { source: "all", minScore: 25, ticker: "", scanning: false };

const el = (id) => document.getElementById(id);
const listEl = el("list");
const statusEl = el("statusline");
const radarEl = el("radar");

// ---------- rendering helpers ----------

function heatColor(score) {
  // signal-strength heat: cool -> warm -> hot as score climbs
  if (score >= 80) return "var(--hot)";
  if (score >= 60) return "var(--warm)";
  if (score >= 40) return "var(--cool)";
  return "var(--dim)";
}

function bucketOf(source) {
  return source === "SEC Form 4" ? "insider" : "congress";
}

function meterHTML(score) {
  const lit = Math.round((score / 100) * 10);
  const color = heatColor(score);
  let out = "";
  for (let i = 0; i < 10; i++) {
    out += `<i style="background:${i < lit ? color : "var(--line-soft)"}"></i>`;
  }
  return out;
}

function relDays(a, b) {
  // days between two ISO/date strings, or null
  const da = Date.parse(a), db = Date.parse(b);
  if (isNaN(da) || isNaN(db)) return null;
  return Math.round((db - da) / 86400000);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function cardHTML(s) {
  const bucket = bucketOf(s.source);
  const srcLabel = s.source;
  const disclosed = relDays(s.trade_date, s.filed_date);
  const isCongress = bucket === "congress";

  const tickerHTML = s.ticker
    ? esc(s.ticker)
    : '<span class="none">— see filing</span>';

  const metaBits = [];
  if (s.action) metaBits.push(`<span class="${/BUY/i.test(s.action) ? "buy" : ""}">${esc(s.action)}</span>`);
  if (s.value && s.value !== "See filing") metaBits.push(`<b>${esc(s.value)}</b>`);
  if (s.trade_date) metaBits.push(`Traded <b>${esc(s.trade_date.slice(0, 10))}</b>`);
  if (isCongress && disclosed != null) metaBits.push(`Disclosed <b>${disclosed}d later</b>`);
  else if (s.filed_date) metaBits.push(`Filed <b>${esc(String(s.filed_date).slice(0, 10))}</b>`);

  return `
  <article class="card ${bucket}">
    <div class="card-head">
      <div>
        <div class="ticker">${tickerHTML}</div>
      </div>
      <div>
        <div class="score-num" style="color:${heatColor(s.score)}">${s.score}</div>
        <div class="score-cap">SIGNAL</div>
      </div>
    </div>
    <div class="meter">${meterHTML(s.score)}</div>
    <div class="tags">
      <span class="tag src">${esc(srcLabel)}</span>
    </div>
    <div class="who">${esc(s.person)}${s.role ? ` · <span class="role">${esc(s.role)}</span>` : ""}</div>
    <div class="meta">${metaBits.join("")}</div>
    ${s.reasons ? `<div class="reasons">${esc(s.reasons)}</div>` : ""}
    ${s.url ? `<div style="margin-top:8px"><a class="filing" href="${esc(s.url)}" target="_blank" rel="noopener">Open filing</a></div>` : ""}
  </article>`;
}

function renderEmpty() {
  const tuned = state.minScore > 0 || state.ticker || state.source !== "all";
  listEl.innerHTML = `
    <div class="empty">
      <h2>${tuned ? "No signals match these filters" : "No signals yet"}</h2>
      <p>${tuned
        ? "Loosen the minimum signal or clear the ticker filter."
        : "Run a scan to pull the latest SEC Form 4 insider buys and congressional disclosures."}</p>
      <button id="emptyScan">Run scan now</button>
    </div>
    <div class="disclaimer">Congressional rows show when a trade became <b>public</b>, not when it happened — members have up to 45 days to disclose under the STOCK Act. This is not a real-time feed of politician trading.</div>`;
  const b = el("emptyScan");
  if (b) b.onclick = runScan;
}

// ---------- data flow ----------

let loadSeq = 0;
async function load() {
  const seq = ++loadSeq;
  try {
    const params = { min_score: state.minScore, source: state.source, ticker: state.ticker };
    const data = await API.signals(params);
    if (seq !== loadSeq) return; // a newer request already landed
    if (!data.signals.length) return renderEmpty();
    listEl.innerHTML = data.signals.map(cardHTML).join("");
    listEl.insertAdjacentHTML("beforeend",
      `<div class="disclaimer">Congressional rows show when a trade became <b>public</b>, not when it happened — up to 45 days after the trade under the STOCK Act.</div>`);
  } catch (e) {
    if (seq !== loadSeq) return;
    listEl.innerHTML = `<div class="empty"><h2>Can't reach the scanner</h2><p>Make sure the backend is running and your phone is on the same network.</p></div>`;
  }
}

async function refreshStatus() {
  try {
    const h = await API.health();
    setScanning(h.scan && h.scan.running);
    const parts = [`threshold ${h.min_alert_score}`];
    if (h.scan && h.scan.last_run) {
      const t = new Date(h.scan.last_run);
      parts.push(`scanned ${t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`);
    }
    if (!state.scanning) statusEl.textContent = parts.join(" · ");
  } catch { statusEl.textContent = "offline"; }
}

function setScanning(on) {
  state.scanning = on;
  radarEl.classList.toggle("scanning", on);
  const fab = el("scanFab");
  fab.classList.toggle("busy", on);
  fab.querySelector(".fab-label").textContent = on ? "Scanning…" : "Run scan";
  if (on) statusEl.textContent = "scanning SEC + congressional data…";
}

let pollTimer = null;
async function runScan() {
  if (state.scanning) return;
  setScanning(true);
  toast("Scan started");
  try {
    await API.scan();
  } catch {
    setScanning(false);
    return toast("Couldn't start scan");
  }
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const st = await API.status();
      if (!st.running) {
        clearInterval(pollTimer);
        setScanning(false);
        await load();
        await refreshStatus();
        toast(st.last_error ? "Scan finished with warnings" : "Scan complete");
      }
    } catch { /* keep polling */ }
  }, 1500);
}

// ---------- toast ----------
let toastTimer = null;
function toast(msg) {
  const t = el("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2200);
}

// ---------- controls wiring ----------
el("sourceSeg").addEventListener("click", (e) => {
  const btn = e.target.closest(".seg");
  if (!btn) return;
  el("sourceSeg").querySelectorAll(".seg").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  state.source = btn.dataset.source;
  load();
});

const scoreRange = el("scoreRange");
scoreRange.addEventListener("input", () => {
  state.minScore = +scoreRange.value;
  el("scoreVal").textContent = scoreRange.value;
});
scoreRange.addEventListener("change", load); // fire the fetch when the finger lifts

let tickerTimer = null;
const tickerInput = el("tickerInput");
tickerInput.addEventListener("input", () => {
  state.ticker = tickerInput.value.trim();
  el("tickerClear").hidden = !state.ticker;
  clearTimeout(tickerTimer);
  tickerTimer = setTimeout(load, 300);
});
el("tickerClear").addEventListener("click", () => {
  tickerInput.value = ""; state.ticker = ""; el("tickerClear").hidden = true; load();
});

el("scanFab").addEventListener("click", runScan);

// ---------- pull to refresh ----------
(function pullToRefresh() {
  let startY = 0, pulling = false;
  const ptr = el("ptr");
  window.addEventListener("touchstart", (e) => {
    if (window.scrollY <= 0) { startY = e.touches[0].clientY; pulling = true; }
  }, { passive: true });
  window.addEventListener("touchmove", (e) => {
    if (!pulling) return;
    const dy = e.touches[0].clientY - startY;
    if (dy > 70) ptr.classList.add("show");
  }, { passive: true });
  window.addEventListener("touchend", () => {
    if (ptr.classList.contains("show")) {
      ptr.classList.remove("show");
      load(); refreshStatus();
    }
    pulling = false;
  });
})();

// ---------- web push (alerts while the app is closed) ----------

const alertsBtn = el("alertsBtn");
const pushSupported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

function urlB64ToUint8Array(b64) {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

function setBell(stateClass) {
  alertsBtn.classList.remove("on", "pending");
  if (stateClass) alertsBtn.classList.add(stateClass);
  alertsBtn.title = stateClass === "on" ? "Alerts on — tap to turn off" : "Enable alerts";
}

// POST a subscription to the server. Idempotent server-side (INSERT OR REPLACE keyed
// on endpoint), so it's safe to call on every load as well as on opt-in.
const syncSubscription = (sub) =>
  fetch("/api/push/subscribe", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(sub),
  });

async function reflectPushState() {
  if (!pushSupported) { alertsBtn.hidden = true; return; }
  try {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    setBell(sub ? "on" : null);
    // Self-heal: the browser keeps its subscription across server restarts, but the
    // server stores subscriptions in a DB that can be reset (e.g. a redeploy on an
    // ephemeral disk). Without this, the bell shows "on" while the server no longer
    // knows the endpoint, so no pushes arrive. Re-register any existing subscription
    // on load so a wiped server table silently recovers. Fire-and-forget: a failed
    // sync (offline, etc.) must not break the app, which works offline otherwise.
    if (sub) syncSubscription(sub).catch(() => {});
  } catch { /* leave neutral */ }
}

async function enableAlerts() {
  const info = await API_pushKey();
  if (!info.configured || !info.public_key) {
    return toast("Alerts aren't set up on the server yet");
  }
  const perm = await Notification.requestPermission();
  if (perm !== "granted") return toast("Notifications are blocked in settings");

  setBell("pending");
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlB64ToUint8Array(info.public_key),
  });
  await syncSubscription(sub);
  setBell("on");
  toast("Alerts on — you'll be pinged on new high-score signals");
}

async function disableAlerts() {
  setBell("pending");
  try {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (sub) {
      await fetch("/api/push/unsubscribe", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ endpoint: sub.endpoint }),
      });
      await sub.unsubscribe();
    }
  } catch { /* fall through */ }
  setBell(null);
  toast("Alerts off");
}

const API_pushKey = () => fetch("/api/push/key").then((r) => r.json());

alertsBtn.addEventListener("click", async () => {
  try {
    if (alertsBtn.classList.contains("on")) await disableAlerts();
    else await enableAlerts();
  } catch (e) {
    setBell(null);
    toast("Couldn't change alerts");
  }
});

// ---------- boot ----------
async function boot() {
  await refreshStatus();
  await load();
  // keep the "scanned at / scanning" line honest if a --loop scan runs elsewhere
  setInterval(refreshStatus, 8000);
}
boot();

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () =>
    navigator.serviceWorker.register("sw.js").then(reflectPushState).catch(() => {})
  );
}
