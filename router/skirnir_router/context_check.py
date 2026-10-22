"""Vorabpruefung des Kontexts: passt eine Anfrage ueberhaupt in num_ctx?

Ollama kuerzt zu lange Prompts still. Beim Chat entfernt seine Go-Schicht vorne ganze Nachrichten, bevor der Prompt
an llama-server geht - das steht nur mit OLLAMA_DEBUG im Log (Beleg 2026-09-24 02:29:19: 350k Zeichen kamen als
`task.n_tokens = 2477` an, ohne eine einzige Log-Zeile). Der Client bekommt eine normale Antwort, nur fehlt ihm ein
Teil des Verlaufs. Der Knoten-Agent sieht das nicht; der Router sieht aber beides: was geschickt wurde und was laut
Ollama angekommen ist (prompt_eval_count = volle Promptlaenge, auch bei gecachtem Praefix).

Zwei Stufen, damit eine grobe Schaetzung keinen Fehlalarm ausloest:
  vorher  - Schaetzung "jede Ziffer 1 Token, Rest Bytes/3,5" (Qwen zerlegt Zahlen in Einzelziffern). Gemessen an 80
            echten Agenten-Texten (Werkzeugausgaben, Nutzernachrichten) gegen qwen3.6: echt/Schaetzung Median 0,99,
            p90 1,10, max 1,26. Liegt sie ueber
            num_ctx, ist die Anfrage ein Kandidat.
  nachher - Kamen laut Ollama weniger als BESTAETIGT x Schaetzung an, ist die Kuerzung bestaetigt: Journal-Ereignis
            `context_truncated`, Metrik, Warnung, HA-Ereignis. Kam etwa die Schaetzung an, hat nur die Schaetzung
            uebertrieben - kein Alarm, nur eine Info-Zeile (Kalibrierhilfe).
Cloud-Stufen bleiben aussen vor: Anbieter lehnen zu lange Prompts mit Fehler ab statt still zu kuerzen.
"""
import json

from . import state
from .common import log

BYTES_PER_TOKEN = 3.5
MESSAGE_OVERHEAD = 4   # Rollen- und Trennmarken des Chat-Templates je Nachricht
CONFIRMED = 0.8          # angekommen < 0,8 x geschaetzt -> wahrscheinlich gekuerzt
CERTAIN = 0.45             # angekommen < 0,45 x geschaetzt -> sicher gekuerzt (schlechteste Ueberschaetzung gemessen: 0,46)
# Grenzen, gemessen 2026-09-24 an 80 echten Texten (echt/Schaetzung): p10 0,85, min 0,46 - 4 von 80 lagen unter 0,8,
# alle mit viel Leerraum (eingerueckte Ausgaben; Leerzeichenfolgen sind fuer den Tokenizer fast gratis). Feinere Regeln
# (Leerraum getrennt zaehlen) gewannen nur in der Stichprobe, nicht in der 5-fach-Kreuzvalidierung. Folge: eine
# Kuerzung um weniger als ~25 % ist von einem Schaetzfehler nicht zu unterscheiden und bleibt unsichtbar.
_DIGITS = "0123456789"


def estimate_text(text):
    """Tokens eines Texts: ASCII-Ziffern je 1, alles andere ceil(UTF-8-Bytes / 3,5)."""
    if not text:
        return 0
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, separators=(",", ":"))
    digits = sum(map(text.count, _DIGITS))
    rest = len(text.encode("utf-8", "replace")) - digits
    return digits + int(-(-rest // BYTES_PER_TOKEN))


def _upper_bound(body):
    """Billige Obergrenze: kein Byte wird zu mehr als einem Token (Ziffern genau 1, CJK 3 Bytes je Token)."""
    n = sum(len(body.get(k) or "") for k in ("system", "prompt", "suffix") if isinstance(body.get(k), str))
    for m in body.get("messages") or []:
        c = m.get("content")
        n += MESSAGE_OVERHEAD + (len(c.encode("utf-8", "replace")) if isinstance(c, str) else len(json.dumps(c or "")))
        if m.get("tool_calls"):
            n += len(json.dumps(m["tool_calls"]))
    if body.get("tools"):
        n += len(json.dumps(body["tools"]))
    return n


def estimate_body(body):
    """Geschaetzte Prompt-Tokens eines Ollama-Bodys (/api/chat oder /api/generate). Bilder zaehlen nicht mit -
    ihr Preis haengt vom Modell ab und steckt nicht in den Bytes."""
    n = sum(estimate_text(body.get(k)) for k in ("system", "prompt", "suffix"))
    for m in body.get("messages") or []:
        n += MESSAGE_OVERHEAD + estimate_text(m.get("content"))
        if m.get("tool_calls"):
            n += estimate_text(m["tool_calls"])
    if body.get("tools"):
        n += estimate_text(body["tools"])
    return n


def before_request(body, ctx, is_cloud=False):
    """Schaetzung, wenn die Anfrage num_ctx ueberschreiten koennte, sonst None (auch fuer Cloud/ohne ctx)."""
    if is_cloud or not ctx:
        return None
    if _upper_bound(body) < ctx:
        return None   # passt sicher - die allermeisten Anfragen enden hier ohne Schaetzung
    est = estimate_body(body)
    return est if est > ctx else None


def after_request(estimated, received, ctx, info):
    """Abgleich nach der Antwort. Liefert True, wenn die Kuerzung bestaetigt und gemeldet wurde."""
    if not estimated or not received:
        return False
    if received >= CONFIRMED * estimated:
        log.info("context check: estimated %d > num_ctx %d, received %d - estimate too high, no truncation (%s)",
                 estimated, ctx, received, info.get("model"))
        return False
    ratio = received / estimated
    e = {"event": "context_truncated", "role": info.get("role"), "model": info.get("model"), "node": info.get("node"),
         "client": info.get("client"), "path": info.get("path"), "num_ctx": ctx, "estimated": estimated,
         "received": received, "ratio": round(ratio, 2), "certain": ratio < CERTAIN, "request_id": info.get("request_id")}
    log.warning("context %s truncated: %s sent ~%d tokens to %s (%s, num_ctx %d), received %d (%.0f %%) - Ollama truncates silently (req=%s)",
                "certainly" if e["certain"] else "probably", e["client"] or "-", estimated, e["model"], e["node"], ctx,
                received, ratio * 100, e["request_id"])
    state.remember(e)
    state.TRUNCATIONS["count"] += 1
    state.TRUNCATIONS["last"] = {k: v for k, v in e.items() if k not in ("event", "t")}
    state.MQTT_EVENTS.append({"event_type": "context_truncated", **state.TRUNCATIONS["last"]})
    del state.MQTT_EVENTS[:-20]
    return True


def conservative_bound(body):
    """Reusable text/tool byte bound; excludes model-specific image token costs."""
    return _upper_bound(body)
