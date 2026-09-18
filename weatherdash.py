"""OpenClaw Forecast Synthesis — Brief 2 Lama Host."""
import json, logging, time, hashlib, urllib.request
from config import CLOUD_LLM_API_KEY as DEEPSEEK_API_KEY

log = logging.getLogger(__name__)

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
CACHE_TTL = 300  # 5 min

_cache = {}

def _hash_payload(p):
    raw = json.dumps(p.get("time_series", {}), sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()

SYSTEM_PROMPT = (
    "You are a meteorological analysis assistant. Analyse sensor time-series "
    "data and an API forecast for a rural Wellington region, NZ location. "
    "Sensor trends carry significant weight: rapidly falling pressure, rising "
    "humidity, collapsing lux indicate frontal approach even if API disagrees. "
    "NZ context: roaring forties, rapid frontal passage, orographic effects in "
    "hilly Wellington terrain, Southern Hemisphere seasonality. "
    "Return ONLY valid JSON matching the schema. No prose, no markdown."
)


def _build_user_prompt(payload):
    ts = payload.get("time_series", {})
    lines = []
    for name, data in ts.items():
        if name == "wind":
            vals = data.get("values", [])
            if vals:
                wind_parts = []
                for w in vals[:24]:
                    wind_parts.append(f"{w['speed_kmh']:.0f}km/h@{w['dir_deg']:.0f}deg")
                lines.append(f"Wind, {data.get('interval_min',5)}min intervals: {', '.join(wind_parts)}")
        else:
            vals = data.get("values", [])
            if vals:
                lines.append(f"{name}, {data.get('interval_min','?')}min intervals: {', '.join(f'{v:.1f}' for v in vals[:24])}")
    forecast = payload.get("api_forecast", [])
    if forecast:
        lines.append("Norway API forecast:")
        for f in forecast:
            lines.append(f"  +{f['hour_offset']}h: {f['condition']} {f['temp_c']}C rain={f['rain_prob_pct']}%")
    lines.append("Return JSON: current_summary, hours[{hour_offset,condition,temp_c,rain_likely_pct,confidence,note}], sensor_api_agreement, alert.")
    return "\n".join(lines)


def _validate_response(obj):
    if not isinstance(obj, dict):
        return False
    if "current_summary" not in obj or "hours" not in obj:
        return False
    if not isinstance(obj["hours"], list) or len(obj["hours"]) == 0:
        return False
    return True


def synthesise(payload):
    t0 = time.monotonic()
    h = _hash_payload(payload)
    if h in _cache and time.time() - _cache[h][1] < CACHE_TTL:
        log.info("Weather synthesis: cache hit hash=%s", h[:12])
        return _cache[h][0]

    prompt = _build_user_payload(payload)
    body = json.dumps({
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "stream": False,
    }).encode()
    req = urllib.request.Request(DEEPSEEK_URL, data=body, headers={
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }, method="POST")

    resp_text = None
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                obj = json.loads(r.read().decode())
            resp_text = obj["choices"][0]["message"]["content"]
            import re
            m = re.search(r"\{[\s\S]*\}", resp_text)
            if m:
                forecast = json.loads(m.group(0))
            else:
                forecast = json.loads(resp_text)
            if _validate_response(forecast):
                break
        except Exception:
            log.warning("Weather synthesis attempt %d failed", attempt, exc_info=True)
    else:
        log.error("Weather synthesis: all attempts failed")
        return {"error": "synthesis_failed", "degraded": True}, 502

    latency = time.monotonic() - t0
    _cache[h] = (forecast, time.time())
    log.info("Weather synthesis: success latency=%.1fs hash=%s", latency, h[:12])
    return forecast, 200
