"""Router dashboard server — a filterable control surface for the whole router.

Runs on 127.0.0.1:4032. Pulls live data from the router and serves:
  GET /                -> dark filterable dashboard page
  GET /api/summary     -> everything: services, models, keys, spend, lanes
  GET /api/health      -> ok

The dashboard filters client-side over /api/summary data:
  - search (model / agent / lane)
  - lane filter (laya / openrouter / ollama / your custom lanes)
  - status filter (up / down)
  - view tabs (models · keys · agents · services)

Stdlib only. Master key stays in memory, never returned.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BRIDGE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("DASHBOARD_PORT", "4032"))
BASE = "http://127.0.0.1:4000"

# ---------------------------------------------------------------- helpers ----

def _read(name: str, default: str = "") -> str:
    try:
        with open(os.path.join(BRIDGE, name), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return default


MASTER = _read("master_key")

_cache: dict = {"ts": 0.0, "data": None}
CACHE_SECONDS = 60  # /spend/logs is slow (~40s server-side); cache the summary

# Spend and model-list are fetched in BACKGROUND threads so the dashboard
# renders instantly: services/keys are fast; the slow blocks land after.
_spend: dict = {"ts": 0.0, "data": None, "busy": False}
SPEND_TTL = 300      # refresh spend at most every 5 min
_models: dict = {"ts": 0.0, "data": None, "busy": False}
MODELS_TTL = 300     # /model/info takes ~7s for 700+ models; refresh every 5 min
_keys: dict = {"ts": 0.0, "data": None, "busy": False}
KEYS_TTL = 120       # /key/list + N /key/info calls queue behind slow spend queries

# EDIT: your stack's services as (name, port, health path).
SERVICES = [
    ("litellm", 4000, "/health/liveliness"),
    ("laya-svc", 4030, "/health"),
]

# EDIT: add your custom lanes as (lane, (model_name_prefixes,)).
LANE_OF = [
    ("laya", ("laya",)),
    ("openrouter", ("openrouter/",)),
    ("ollama", ("ollama/",)),
]


def _probe(port: int, path: str, timeout: float = 2.5) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout):
            return True
    except urllib.error.HTTPError as e:
        return e.code < 500
    except Exception:
        return False


def _api(path: str, timeout: float = 25.0):
    req = urllib.request.Request(BASE + path)
    req.add_header("Authorization", "Bearer " + MASTER)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _lane_of(name: str) -> str:
    for lane, prefixes in LANE_OF:
        for p in prefixes:
            if name == p or name.startswith(p):
                return lane
    return "other"


def _kick(cache: dict, fetch, ttl: float) -> None:
    """Start a background refresh if the cache is empty or stale (never blocks)."""
    if cache["busy"]:
        return
    fresh = cache["data"] is not None and time.time() - cache["ts"] <= ttl
    if fresh:
        return
    cache["busy"] = True

    def _run():
        try:
            fetch()
        finally:
            cache["busy"] = False

    threading.Thread(target=_run, daemon=True).start()


def _collect_models() -> None:
    """Fetch the registered-model list in the background (slow: ~7s for 700+)."""
    try:
        info = _api("/model/info", timeout=120.0)
        rows = []
        for m in info.get("data", []):
            n = m.get("model_name")
            if not n or "*" in n:
                continue
            rows.append({
                "name": n,
                "lane": _lane_of(n),
                "upstream": (m.get("litellm_params") or {}).get("model", ""),
                "id": m.get("model_info", {}).get("id", ""),
            })
        _models["data"] = rows
        _models["ts"] = time.time()
    except Exception as e:
        _models["error"] = str(e)[:200]


def _collect_keys() -> None:
    """Fetch key list + per-key info in the background (queues behind slow calls)."""
    try:
        keys = _api("/key/list")
        rows = []
        for tok in keys.get("keys", [])[:20]:
            try:
                ki = _api(f"/key/info?key={tok}")["info"]
            except Exception:
                continue
            rows.append({
                "alias": ki.get("key_alias") or "(none)",
                "spend": round(float(ki.get("spend") or 0), 4),
                "n_models": len(ki.get("models") or []),
                "has_wildcards": any("*" in m for m in (ki.get("models") or [])),
                "models": ki.get("models") or [],
            })
        _keys["data"] = rows
        _keys["ts"] = time.time()
    except Exception as e:
        _keys["error"] = str(e)[:200]


def _collect_spend() -> None:
    """Fetch + aggregate spend in the background (slow call, ~40s server-side).

    Stores into the module-level _spend cache; never raises outward."""
    try:
        logs = _api("/spend/logs?limit=800", timeout=120.0)
        if isinstance(logs, dict):
            logs = logs.get("logs") or []
        by_agent: dict[str, int] = {}
        by_model: dict[str, int] = {}
        calls = 0
        for e in logs:
            calls += 1
            t = int(e.get("total_tokens") or 0)
            ag = ((e.get("metadata") or {}).get("user_api_key_alias")
                  or e.get("user") or "master")
            if ag in ("default_user_id", "litellm_proxy_master_key", ""):
                ag = "master"
            by_agent[ag] = by_agent.get(ag, 0) + t
            mod = e.get("model") or "?"
            by_model[mod] = by_model.get(mod, 0) + t
        n = sum(by_agent.values())
        _spend["data"] = {
            "calls": calls, "tokens": n,
            "by_agent": sorted(({"name": k, "tokens": v} for k, v in by_agent.items()),
                               key=lambda x: -x["tokens"]),
            "by_model": sorted(({"name": k, "tokens": v} for k, v in by_model.items()),
                               key=lambda x: -x["tokens"])[:40],
        }
        _spend["ts"] = time.time()
    except Exception as e:
        _spend["error"] = str(e)[:200]


def _summary() -> dict:
    out: dict = {"updated": time.strftime("%H:%M:%S"), "services": [], "models": [],
                 "keys": [], "spend": {}, "counts": {}}

    # --- services -----------------------------------------------------------
    for name, port, path in SERVICES:
        alive = _probe(port, path)
        out["services"].append({"name": name, "port": port, "up": alive})

    # --- models (registered on the proxy) — served from background cache ----
    out["models"] = _models["data"] or []
    _kick(_models, _collect_models, MODELS_TTL)
    if _models.get("error"):
        out["models_error"] = _models["error"]

    # --- key health: which models are NOT callable (background-cached) ------
    out["keys"] = _keys["data"] or []
    _kick(_keys, _collect_keys, KEYS_TTL)
    if _keys.get("error"):
        out["keys_error"] = _keys["error"]

    key_models: set[str] = set()
    for k in out["keys"]:
        key_models.update(k.get("models") or [])

    for m in out["models"]:
        m["callable"] = m["name"] in key_models or any(
            m["name"].startswith(p.rstrip("*")) for p in key_models if "*" in p)

    out["models"] = sorted(out["models"], key=lambda m: (m["lane"], m["name"]))

    # --- spend (last window) ------------------------------------------------
    # Served from the background-cached block; NEVER blocks the summary.
    out["spend"] = _spend["data"] or {}
    _kick(_spend, _collect_spend, SPEND_TTL)
    if _spend.get("error"):
        out["spend_error"] = _spend["error"]
    if _spend["data"]:
        out["spend_age_s"] = round(time.time() - _spend["ts"], 1)

    # --- counts -------------------------------------------------------------
    lanes: dict[str, int] = {}
    for m in out["models"]:
        lanes[m["lane"]] = lanes.get(m["lane"], 0) + 1
    out["counts"] = {"lanes": lanes, "models": len(out["models"]),
                     "services_up": sum(1 for s in out["services"] if s["up"])}

    return out


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Router Dashboard — live</title>
<style>
  :root{--paper:#17130f;--panel:#201a14;--panel2:#262019;--ink:#e8ddcc;--dim:#a4977f;
    --hair:#3a3127;--accent:#e0a458;--ok:#7fb069;--bad:#c96b5a;--mono:'Cascadia Code',ui-monospace,Consolas,monospace}
  *{box-sizing:border-box}
  body{margin:0;background:var(--paper);color:var(--ink);
    font:14px/1.5 -apple-system,'Segoe UI',system-ui,sans-serif;padding:26px 22px 70px}
  .wrap{max-width:1180px;margin:0 auto}
  h1{font:600 27px/1.15 Georgia,serif;margin:0 0 4px}
  h1 .sub{color:var(--accent)}
  .meta{color:var(--dim);font-size:12.5px;margin-bottom:18px}
  .meta b{color:var(--ok)}
  .toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:14px 0 8px}
  input[type=search],select{background:var(--panel2);color:var(--ink);border:1px solid var(--hair);
    border-radius:8px;padding:8px 12px;font:13px inherit;min-width:150px;outline:none}
  input[type=search]{min-width:240px}
  input:focus,select:focus{border-color:#5a4c3a}
  .tabs{display:flex;gap:4px;margin:16px 0 6px;border-bottom:1px solid var(--hair);padding-bottom:0}
  .tab{background:none;border:none;color:var(--dim);font:600 13px inherit;padding:9px 14px;
    cursor:pointer;border-bottom:2px solid transparent}
  .tab.on{color:var(--accent);border-bottom-color:var(--accent)}
  .tab .n{color:var(--dim);font-weight:400;margin-left:5px;font-family:var(--mono);font-size:12px}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:10px 0 4px}
  .card{background:var(--panel);border:1px solid var(--hair);border-radius:9px;padding:11px 14px}
  .card .k{font:600 10.5px var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--dim)}
  .card .v{font:700 21px var(--mono);margin-top:3px}
  table{width:100%;border-collapse:collapse;margin-top:8px;font-size:13px}
  th{text-align:left;font:600 11px var(--mono);letter-spacing:.08em;text-transform:uppercase;
    color:var(--dim);border-bottom:1px solid var(--hair);padding:8px 10px 8px 0;position:sticky;top:0;background:var(--paper)}
  td{border-bottom:1px solid #2a231c;padding:7px 10px 7px 0;vertical-align:middle}
  td.mono,th.mono{font-family:var(--mono);font-size:12.3px}
  tr.hide,.hide{display:none}
  .pill{display:inline-block;font:600 10.5px var(--mono);padding:2.5px 8px;border-radius:20px;border:1px solid}
  .pill.up{color:#8fce74;border-color:#3d5334;background:#20301d}
  .pill.down{color:#e0917f;border-color:#57362e;background:#33211d}
  .pill.lane-custom{color:#e0a458;border-color:#4d3f28;background:#2c2419}
  .pill.lane-laya{color:#7fb8d4;border-color:#33505d;background:#1e2a30}
  .pill.lane-openrouter{color:#c9a0dc;border-color:#4d3a57;background:#2a2030}
  .pill.lane-ollama{color:#d0c07a;border-color:#4f4728;background:#2c2919}
  .pill.lane-other{color:var(--dim);border-color:var(--hair);background:var(--panel2)}
  .bar{height:7px;border-radius:5px;background:#33291f;overflow:hidden;min-width:60px}
  .bar>i{display:block;height:100%;background:var(--accent)}
  .empty{color:var(--dim);padding:26px 4px;font-style:italic}
  a{color:var(--accent)}
  .foot{margin-top:34px;color:var(--dim);font-size:12px;border-top:1px solid var(--hair);padding-top:12px}
  @media(max-width:760px){body{padding:16px 12px 50px}.toolbar{gap:7px}input[type=search]{min-width:100%}
    .cards{grid-template-columns:repeat(auto-fit,minmax(120px,1fr))}}
</style></head><body><div class="wrap">

<h1>Router <span class="sub">Dashboard</span></h1>
<div class="meta" id="meta">connecting…</div>

<div class="cards" id="cards"></div>

<div class="toolbar">
  <input type="search" id="q" placeholder="Search models, agents, services…">
  <select id="lane"><option value="">All lanes</option></select>
  <select id="status"><option value="">Any status</option>
    <option value="up">Up / callable only</option>
    <option value="down">Problem only</option></select>
  <select id="sort"><option value="lane">Sort: lane</option>
    <option value="name">Sort: name</option>
    <option value="tokens">Sort: tokens</option></select>
</div>

<div class="tabs" id="tabs"></div>
<div id="body"></div>

<div class="foot">LLM Router · <span id="updated"></span> · auto-refresh 20s ·
sources: /model/info · /key/list · /spend/logs · port probes</div>
</div>
<script>
const $ = s => document.querySelector(s);
let DATA = null, TAB = 'models';

const fmt = n => n == null ? '–' : n >= 1e9 ? (n/1e9).toFixed(2)+'B'
  : n >= 1e6 ? (n/1e6).toFixed(2)+'M' : n >= 1e3 ? (n/1e3).toFixed(1)+'K' : String(n);

function lanePill(l){ return `<span class="pill lane-${l}">${l}</span>`; }
function upPill(b){ return b ? '<span class="pill up">UP</span>' : '<span class="pill down">DOWN</span>'; }

function renderCards(){
  const c = DATA.counts || {}, s = DATA.services || [];
  const up = s.filter(x=>x.up).length;
  const tokens = (DATA.spend||{}).tokens ?? 0, calls = (DATA.spend||{}).calls ?? 0;
  const lanes = Object.entries(c.lanes||{}).map(([k,v])=>`${k} ${v}`).join(' · ');
  $('#cards').innerHTML = `
    <div class="card"><div class="k">Services</div><div class="v">${up}/${s.length}</div></div>
    <div class="card"><div class="k">Models</div><div class="v">${c.models ?? 0}</div></div>
    <div class="card"><div class="k">Tokens (window)</div><div class="v">${fmt(tokens)}</div></div>
    <div class="card"><div class="k">Calls (window)</div><div class="v">${fmt(calls)}</div></div>
    <div class="card"><div class="k">Lanes</div><div class="v" style="font-size:13px;line-height:1.5">${lanes||'–'}</div></div>`;
}

function mkTabs(){
  const t = [
    ['models','Models', (DATA.models||[]).length],
    ['keys','Keys', (DATA.keys||[]).length],
    ['agents','Agents', ((DATA.spend||{}).by_agent||[]).length],
    ['services','Services', (DATA.services||[]).length],
  ];
  $('#tabs').innerHTML = t.map(([id,label,n]) =>
    `<button class="tab ${TAB===id?'on':''}" data-tab="${id}">${label}<span class="n">${n}</span></button>`).join('');
  $('#tabs').querySelectorAll('.tab').forEach(b =>
    b.onclick = () => { TAB = b.dataset.tab; render(); });
}

function fillLanes(){
  const sel = $('#lane');
  if (sel.dataset.filled) return;
  const lanes = Object.keys((DATA.counts||{}).lanes||{});
  sel.innerHTML = '<option value="">All lanes</option>' +
    lanes.map(l=>`<option value="${l}">${l}</option>`).join('');
  sel.dataset.filled = '1';
}

function applyFilters(rows, getText, getLane, getOk){
  const q = $('#q').value.trim().toLowerCase();
  const lane = $('#lane').value, st = $('#status').value, sort = $('#sort').value;
  let out = rows.filter(r => {
    if (q && !getText(r).toLowerCase().includes(q)) return false;
    if (lane && getLane(r) !== lane) return false;
    if (st === 'up' && !getOk(r)) return false;
    if (st === 'down' && getOk(r)) return false;
    return true;
  });
  return out;
}

function render(){
  if (!DATA) return;
  fillLanes(); mkTabs();
  const q = $('#q').value.trim().toLowerCase();
  const lane = $('#lane').value, st = $('#status').value, sort = $('#sort').value;
  const b = $('#body');

  if (TAB === 'models'){
    let rows = (DATA.models||[]);
    rows = applyFilters(rows, m=>m.name+' '+m.upstream, m=>m.lane, m=>m.callable);
    if (sort==='name') rows.sort((a,b)=>a.name.localeCompare(b.name));
    if (sort==='tokens'){ const tm={}; ((DATA.spend||{}).by_model||[]).forEach(x=>tm[x.name]=x.tokens);
      rows.sort((a,b)=>(tm[b.name]||0)-(tm[a.name]||0)); }
    if (sort==='lane') rows.sort((a,b)=>a.lane.localeCompare(b.lane)||a.name.localeCompare(b.name));
    const tm={}; ((DATA.spend||{}).by_model||[]).forEach(x=>tm[x.name]=x.tokens);
    b.innerHTML = rows.length ? `<table><tr><th>Model</th><th>Lane</th><th>Upstream</th>
      <th>Callable</th><th class="mono">Tokens</th></tr>
      ${rows.map(m=>`<tr><td class="mono">${m.name}</td><td>${lanePill(m.lane)}</td>
        <td class="mono" style="color:var(--dim)">${m.upstream||'–'}</td>
        <td>${m.callable?'<span class="pill up">YES</span>':'<span class="pill down">NO</span>'}</td>
        <td class="mono">${fmt(tm[m.name]||0)}</td></tr>`).join('')}</table>`
      : '<div class="empty">No models match the filters.</div>';
  }

  else if (TAB === 'keys'){
    let rows = (DATA.keys||[]);
    rows = applyFilters(rows, k=>k.alias, ()=>'', ()=>true);
    if (sort==='tokens') rows = [...rows].sort((a,b)=>b.spend-a.spend);
    b.innerHTML = rows.length ? `<table><tr><th>Key alias</th><th class="mono">Spend $</th>
      <th class="mono">Models</th><th>Wildcards</th></tr>
      ${rows.map(k=>`<tr><td>${k.alias}</td><td class="mono">${k.spend}</td>
      <td class="mono">${k.n_models}</td>
      <td>${k.has_wildcards?'<span class="pill up">YES</span>':'<span class="pill lane-other">no</span>'}</td></tr>`).join('')}</table>`
      : '<div class="empty">No keys match.</div>';
  }

  else if (TAB === 'agents'){
    let rows = ((DATA.spend||{}).by_agent||[]);
    const max = rows.length ? rows[0].tokens : 1;
    rows = applyFilters(rows, a=>a.name, ()=>'', ()=>true);
    b.innerHTML = rows.length ? `<table><tr><th>Agent (key alias)</th><th class="mono">Tokens</th><th>Share</th></tr>
      ${rows.map(a=>`<tr><td>${a.name}</td><td class="mono">${fmt(a.tokens)}</td>
      <td><div class="bar"><i style="width:${Math.max(2,Math.round(100*a.tokens/max))}%"></i></div></td></tr>`).join('')}</table>`
      : '<div class="empty">No agent spend in the window.</div>';
  }

  else if (TAB === 'services'){
    let rows = (DATA.services||[]);
    rows = applyFilters(rows, s=>s.name+' :'+s.port, ()=>'', s=>s.up);
    b.innerHTML = rows.length ? `<table><tr><th>Service</th><th class="mono">Port</th><th>Status</th></tr>
      ${rows.map(s=>`<tr><td>${s.name}</td><td class="mono">${s.port}</td>
      <td>${upPill(s.up)}</td></tr>`).join('')}</table>`
      : '<div class="empty">No services match.</div>';
  }
}

async function pull(){
  try{
    const r = await fetch('/api/summary', {cache:'no-store'});
    DATA = await r.json();
    $('#meta').innerHTML = `Live · refreshed <b>${DATA.updated}</b> ·
      ${DATA.counts.models||0} models across ${Object.keys(DATA.counts.lanes||{}).length} lanes`;
    $('#updated').textContent = 'updated ' + DATA.updated;
    renderCards(); render();
  }catch(e){
    $('#meta').textContent = 'router unreachable — retrying…';
  }
}

['q','lane','status','sort'].forEach(id => {
  const el = document.getElementById(id);
  el.addEventListener('input', render);
  el.addEventListener('change', render);
});
pull(); setInterval(pull, 20000);
</script></body></html>
"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, body: str, ctype: str):
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except Exception:
            pass

    def do_GET(self):
        p = self.path.split("?", 1)[0]
        if p in ("/", "/index.html", "/dashboard"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif p == "/api/summary":
            now = time.time()
            if _cache["data"] is not None and now - _cache["ts"] < CACHE_SECONDS:
                d = dict(_cache["data"])
                d["cached_age_s"] = round(now - _cache["ts"], 1)
                self._send(200, json.dumps(d), "application/json")
                return
            d = _summary()
            _cache.update(ts=now, data=d)
            self._send(200, json.dumps(d), "application/json")
        elif p == "/health":
            self._send(200, '{"ok": true}', "application/json")
        else:
            self._send(404, "not found", "text/plain")


def main() -> int:
    print(f"router-dashboard on http://127.0.0.1:{PORT}/")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
