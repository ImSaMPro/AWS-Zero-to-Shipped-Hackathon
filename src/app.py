"""
ReliefRoute - last-mile aid dispatcher that proves every delivery decision.

Single Lambda behind a Function URL. Routes:
  GET  /          -> serves the SPA (HTML)
  POST /plan      -> Bedrock Nova Lite ranked dispatch plan + "why" proof
  POST /confirm   -> record a delivery outcome (closes the loop)
  GET  /metrics   -> plan-accuracy readout

Services: AWS Lambda (arm64) + Amazon Bedrock (Nova Lite) + DynamoDB (on-demand, TTL).
No API Gateway, S3, CloudFront, Cognito, RDS, VPC.
"""

import json
import os
import time
import uuid
import datetime
from decimal import Decimal

import boto3
from botocore.config import Config

TABLE_NAME = os.environ["TABLE_NAME"]
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
DAILY_PLAN_CAP = int(os.environ.get("DAILY_PLAN_CAP", "2000"))

_ddb = boto3.resource("dynamodb")
_table = _ddb.Table(TABLE_NAME)
_bedrock = boto3.client("bedrock-runtime", config=Config(retries={"max_attempts": 2}))

TTL_DAYS = 35  # auto-expire records a few days after the 30-day run


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _resp(status, body, content_type="application/json"):
    is_json = content_type == "application/json"
    return {
        "statusCode": status,
        "headers": {
            "content-type": content_type,
            "access-control-allow-origin": "*",
            "access-control-allow-methods": "GET,POST,OPTIONS",
            "access-control-allow-headers": "content-type",
        },
        "body": json.dumps(body, default=_json_default) if is_json else body,
    }


def _json_default(o):
    if isinstance(o, Decimal):
        # int if whole, else float - for clean JSON on the wire
        return int(o) if o == o.to_integral_value() else float(o)
    raise TypeError(f"not serializable: {type(o)}")


def _to_decimal(obj):
    """DynamoDB rejects Python floats; convert recursively to Decimal."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, list):
        return [_to_decimal(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_decimal(v) for k, v in obj.items()}
    return obj


def _ttl():
    return int(time.time()) + TTL_DAYS * 86400


def _today_key():
    return "COUNTER#plan#" + datetime.date.today().isoformat()


def _bump_daily_counter():
    """Atomic per-day plan counter for the abuse cap. Returns new count."""
    r = _table.update_item(
        Key={"pk": _today_key()},
        UpdateExpression="SET #c = if_not_exists(#c, :z) + :one, #t = :ttl",
        ExpressionAttributeNames={"#c": "count", "#t": "ttl"},
        ExpressionAttributeValues={":z": 0, ":one": 1, ":ttl": _ttl()},
        ReturnValues="UPDATED_NEW",
    )
    return int(r["Attributes"]["count"])


# --------------------------------------------------------------------------- #
# Bedrock plan generation
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = (
    "You are ReliefRoute, a disaster-relief dispatch planner. "
    "Given a list of NEED REPORTS (people/places needing aid) and available RESOURCES "
    "(supplies and volunteers), produce a ranked dispatch plan. "
    "Rank by urgency and feasibility. Each plan item assigns specific items to one recipient "
    "via one volunteer, with a priority (1=highest) and a one-sentence reason. "
    "Respect resource quantities; never allocate more than is available. "
    "Return ONLY valid JSON matching the schema. No prose outside the JSON."
)

SCHEMA_HINT = {
    "plan": [
        {
            "recipient": "string",
            "items": "string (what + quantity)",
            "assigned_to": "string (volunteer)",
            "priority": "integer (1=highest)",
            "reason": "string (one sentence)",
        }
    ],
    "rationale": "string (how the plan was decided overall)",
    "assumptions": ["string"],
    "confidence": "number 0.0-1.0",
}


def generate_plan(needs, resources):
    user_msg = (
        "NEED REPORTS:\n" + json.dumps(needs, ensure_ascii=False) + "\n\n"
        "AVAILABLE RESOURCES:\n" + json.dumps(resources, ensure_ascii=False) + "\n\n"
        "Return JSON exactly matching this schema (values are types/instructions):\n"
        + json.dumps(SCHEMA_HINT, ensure_ascii=False)
    )

    # Bedrock Converse API - model-agnostic across Nova / Claude.
    out = _bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": user_msg}]}],
        inferenceConfig={"maxTokens": 1200, "temperature": 0.2, "topP": 0.9},
    )
    text = out["output"]["message"]["content"][0]["text"].strip()

    # Models sometimes wrap JSON in ```json fences - strip them.
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        plan = json.loads(text)
    except json.JSONDecodeError:
        # Last-resort: slice from first { to last }.
        s, e = text.find("{"), text.rfind("}")
        plan = json.loads(text[s : e + 1])
    return plan


# --------------------------------------------------------------------------- #
# Route handlers
# --------------------------------------------------------------------------- #
def handle_plan(payload):
    count = _bump_daily_counter()
    if count > DAILY_PLAN_CAP:
        return _resp(429, {"error": "Daily plan limit reached. Try again tomorrow."})

    needs = payload.get("needs", [])
    resources = payload.get("resources", [])
    if not needs or not resources:
        return _resp(400, {"error": "Provide both 'needs' and 'resources'."})

    plan = generate_plan(needs, resources)
    plan_id = str(uuid.uuid4())[:8]

    _table.put_item(
        Item={
            "pk": f"PLAN#{plan_id}",
            "created": int(time.time()),
            "needs": _to_decimal(needs),
            "resources": _to_decimal(resources),
            "plan": _to_decimal(plan),
            "ttl": _ttl(),
        }
    )
    plan["plan_id"] = plan_id
    return _resp(200, plan)


def handle_confirm(payload):
    plan_id = payload.get("plan_id")
    delivered = bool(payload.get("delivered", False))
    matched = bool(payload.get("matched_plan", False))
    note = str(payload.get("note", ""))[:500]
    if not plan_id:
        return _resp(400, {"error": "plan_id required"})

    _table.put_item(
        Item={
            "pk": f"CONFIRM#{plan_id}#{uuid.uuid4().hex[:6]}",
            "plan_id": plan_id,
            "delivered": delivered,
            "matched_plan": matched,
            "note": note,
            "created": int(time.time()),
            "ttl": _ttl(),
        }
    )
    return _resp(200, {"ok": True, "plan_id": plan_id})


def handle_metrics():
    # Small-scale scan is fine for a demo table; switch to a GSI if it grows.
    items = _table.scan(
        FilterExpression="begins_with(pk, :p)",
        ExpressionAttributeValues={":p": "CONFIRM#"},
    ).get("Items", [])
    total = len(items)
    delivered = sum(1 for i in items if i.get("delivered"))
    matched = sum(1 for i in items if i.get("matched_plan"))
    return _resp(
        200,
        {
            "confirmations": total,
            "delivered": delivered,
            "matched_plan": matched,
            "delivery_rate": round(delivered / total, 3) if total else None,
            "plan_accuracy": round(matched / total, 3) if total else None,
        },
    )


# --------------------------------------------------------------------------- #
# Lambda entry point (Function URL event)
# --------------------------------------------------------------------------- #
def handler(event, context):
    rc = event.get("requestContext", {}).get("http", {})
    method = rc.get("method", "GET")
    path = rc.get("path", "/")

    if method == "OPTIONS":
        return _resp(200, {"ok": True})

    try:
        if method == "GET" and path == "/":
            return _resp(200, SPA_HTML, content_type="text/html")
        if method == "GET" and path == "/metrics":
            return handle_metrics()

        if method == "POST":
            body = json.loads(event.get("body") or "{}")
            if path == "/plan":
                return handle_plan(body)
            if path == "/confirm":
                return handle_confirm(body)

        return _resp(404, {"error": "not found"})
    except Exception as exc:  # noqa: BLE001 - surface a clean error to the SPA
        return _resp(500, {"error": str(exc)})


# --------------------------------------------------------------------------- #
# SPA (served inline - no S3/CloudFront). Voice input via browser Web Speech API.
# --------------------------------------------------------------------------- #
SPA_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>ReliefRoute - Verified Aid Dispatcher</title>
<style>
  :root{--bg:#0f1220;--card:#1a1f36;--ink:#e8ebf5;--muted:#9aa3c0;--acc:#5b8cff;--good:#39d98a;--warn:#ffb02e;--line:#2a3152}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
  header{padding:18px 20px;border-bottom:1px solid var(--line)}
  h1{margin:0;font-size:20px} .sub{color:var(--muted);font-size:13px}
  main{max-width:980px;margin:0 auto;padding:18px;display:grid;gap:16px;grid-template-columns:1fr 1fr}
  @media(max-width:800px){main{grid-template-columns:1fr}}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
  .card h2{margin:0 0 10px;font-size:15px}
  textarea{width:100%;min-height:120px;background:#0c1020;color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:10px;font:13px/1.4 ui-monospace,Consolas,monospace;resize:vertical}
  button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:10px 14px;font-weight:600;cursor:pointer}
  button.ghost{background:transparent;border:1px solid var(--line);color:var(--ink)}
  button:disabled{opacity:.5;cursor:default}
  .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px}
  .full{grid-column:1/-1}
  .plan-item{border:1px solid var(--line);border-radius:8px;padding:10px;margin:8px 0;background:#0c1020}
  .pill{display:inline-block;font-size:11px;padding:2px 8px;border-radius:999px;background:#243056;color:#bcd0ff;margin-right:6px}
  .why{border-left:3px solid var(--acc);padding:8px 12px;margin-top:10px;background:#101633;border-radius:0 8px 8px 0}
  .why b{color:var(--acc)}
  .muted{color:var(--muted)} .good{color:var(--good)} .warn{color:var(--warn)}
  .metrics{display:flex;gap:18px;flex-wrap:wrap}
  .metric{font-size:22px;font-weight:700} .metric small{display:block;font-size:11px;color:var(--muted);font-weight:400}
  code{background:#0c1020;padding:1px 5px;border-radius:4px}
</style>
</head>
<body>
<header>
  <h1>ReliefRoute <span class="sub">- the last-mile aid dispatcher that proves every delivery decision</span></h1>
  <div class="sub">Sense &rarr; reason &rarr; act &rarr; <b>prove</b>. Powered by Amazon Bedrock (Nova Lite) on AWS Lambda.</div>
</header>
<main>
  <section class="card">
    <h2>1. Need reports <button class="ghost" id="mic" title="Dictate a need by voice">&#127908; Speak</button></h2>
    <textarea id="needs">[
  {"location":"Shelter A, Ward 3","people":120,"needs":"drinking water, baby formula","urgency":"high"},
  {"location":"Clinic B","people":40,"needs":"ORS packets, bandages","urgency":"medium"},
  {"location":"Camp C (elderly)","people":60,"needs":"blankets, water","urgency":"high"}
]</textarea>
    <div class="sub muted">JSON array of need reports. Use the mic to append a spoken need.</div>
  </section>

  <section class="card">
    <h2>2. Available resources</h2>
    <textarea id="resources">{
  "supplies":[
    {"item":"water (20L)","qty":90},
    {"item":"baby formula (tins)","qty":50},
    {"item":"ORS packets","qty":200},
    {"item":"blankets","qty":40},
    {"item":"bandages (box)","qty":25}
  ],
  "volunteers":[
    {"name":"Asha","vehicle":"van"},
    {"name":"Ravi","vehicle":"bike"},
    {"name":"Meera","vehicle":"van"}
  ]
}</textarea>
    <div class="row"><button id="go">Generate dispatch plan</button><span id="status" class="sub muted"></span></div>
  </section>

  <section class="card full" id="planCard" style="display:none">
    <h2>Dispatch plan <span id="pid" class="pill"></span> <span id="conf" class="pill"></span></h2>
    <div id="plan"></div>
    <div class="why" id="why"></div>
    <div class="row">
      <button id="confirm">Confirm first delivery completed as planned</button>
      <button class="ghost" id="confirmMiss">Confirm with a mismatch</button>
      <span id="cstatus" class="sub muted"></span>
    </div>
  </section>

  <section class="card full">
    <h2>Loop metrics <button class="ghost" id="refresh">Refresh</button></h2>
    <div class="metrics" id="metrics"><span class="muted">No confirmations yet.</span></div>
    <div class="sub muted" style="margin-top:8px">Every confirmed delivery feeds back here - this is the closed loop most entries skip.</div>
  </section>
</main>

<script>
const $ = s => document.querySelector(s);
let lastPlanId = null;

function parseJSON(id){ try { return JSON.parse($(id).value); } catch(e){ alert("Invalid JSON in "+id); throw e; } }

$("#go").onclick = async () => {
  const needs = parseJSON("#needs");
  const resources = parseJSON("#resources");
  $("#status").textContent = "Thinking (Bedrock Nova Lite)...";
  $("#go").disabled = true;
  try {
    const r = await fetch("/plan", {method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({needs, resources})});
    const d = await r.json();
    if(!r.ok){ $("#status").textContent = d.error || "Error"; return; }
    renderPlan(d);
    localStorage.setItem("rr_last_plan", JSON.stringify(d)); // offline cache
    $("#status").textContent = "Done.";
  } catch(e){ $("#status").textContent = "Network error - showing cached plan if any."; const c = localStorage.getItem("rr_last_plan"); if(c) renderPlan(JSON.parse(c)); }
  finally { $("#go").disabled = false; }
};

function renderPlan(d){
  lastPlanId = d.plan_id || null;
  $("#planCard").style.display = "block";
  $("#pid").textContent = d.plan_id ? ("plan "+d.plan_id) : "";
  $("#conf").textContent = (d.confidence!=null) ? ("confidence "+Math.round(d.confidence*100)+"%") : "";
  const items = (d.plan||[]).slice().sort((a,b)=>(a.priority||9)-(b.priority||9));
  $("#plan").innerHTML = items.map(p => `
    <div class="plan-item">
      <span class="pill">priority ${p.priority ?? "-"}</span>
      <b>${esc(p.recipient)}</b> &larr; ${esc(p.items)} <span class="muted">via ${esc(p.assigned_to)}</span>
      <div class="muted" style="margin-top:4px">${esc(p.reason||"")}</div>
    </div>`).join("") || "<span class='muted'>No items.</span>";
  const assume = (d.assumptions||[]).map(a=>"<li>"+esc(a)+"</li>").join("");
  $("#why").innerHTML = `<b>Why this plan</b><div style="margin-top:4px">${esc(d.rationale||"")}</div>`
    + (assume ? `<div style="margin-top:6px" class="muted">Assumptions:<ul>${assume}</ul></div>` : "");
}
function esc(s){ return String(s??"").replace(/[&<>]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }

async function confirm(matched){
  if(!lastPlanId){ alert("Generate a plan first."); return; }
  $("#cstatus").textContent = "Recording...";
  const r = await fetch("/confirm",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({plan_id:lastPlanId, delivered:true, matched_plan:matched, note: matched?"delivered as planned":"delivered with a change"})});
  $("#cstatus").textContent = r.ok ? "Recorded." : "Error.";
  loadMetrics();
}
$("#confirm").onclick = ()=>confirm(true);
$("#confirmMiss").onclick = ()=>confirm(false);
$("#refresh").onclick = loadMetrics;

async function loadMetrics(){
  const r = await fetch("/metrics"); const m = await r.json();
  if(!m.confirmations){ $("#metrics").innerHTML = "<span class='muted'>No confirmations yet.</span>"; return; }
  $("#metrics").innerHTML = `
    <div class="metric">${m.confirmations}<small>confirmations</small></div>
    <div class="metric good">${m.delivered}<small>delivered</small></div>
    <div class="metric">${m.plan_accuracy!=null?Math.round(m.plan_accuracy*100)+"%":"-"}<small>plan accuracy</small></div>
    <div class="metric">${m.delivery_rate!=null?Math.round(m.delivery_rate*100)+"%":"-"}<small>delivery rate</small></div>`;
}

// Voice input (browser-native Web Speech API - $0, no Amazon Transcribe).
$("#mic").onclick = () => {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if(!SR){ alert("Voice input isn't supported in this browser. Try Chrome."); return; }
  const rec = new SR(); rec.lang="en-IN"; rec.interimResults=false;
  $("#mic").textContent = "...listening";
  rec.onresult = e => {
    const said = e.results[0][0].transcript;
    try{
      const arr = JSON.parse($("#needs").value);
      arr.push({location:"(spoken)", needs: said, urgency:"high"});
      $("#needs").value = JSON.stringify(arr, null, 2);
    }catch(_){ $("#needs").value += "\n// spoken: "+said; }
    $("#mic").innerHTML = "&#127908; Speak";
  };
  rec.onerror = ()=>{ $("#mic").innerHTML="&#127908; Speak"; };
  rec.onend = ()=>{ $("#mic").innerHTML="&#127908; Speak"; };
  rec.start();
};

loadMetrics();
</script>
</body>
</html>
"""
