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
            `kontext_gekuerzt`, Metrik, Warnung, HA-Ereignis. Kam etwa die Schaetzung an, hat nur die Schaetzung
            uebertrieben - kein Alarm, nur eine Info-Zeile (Kalibrierhilfe).
Cloud-Stufen bleiben aussen vor: Anbieter lehnen zu lange Prompts mit Fehler ab statt still zu kuerzen.
"""
import json

from . import state
from .common import log

BYTES_JE_TOKEN = 3.5
NACHRICHT_AUFSCHLAG = 4   # Rollen- und Trennmarken des Chat-Templates je Nachricht
BESTAETIGT = 0.8          # angekommen < 0,8 x geschaetzt -> wahrscheinlich gekuerzt
SICHER = 0.45             # angekommen < 0,45 x geschaetzt -> sicher gekuerzt (schlechteste Ueberschaetzung gemessen: 0,46)
# Grenzen, gemessen 2026-09-24 an 80 echten Texten (echt/Schaetzung): p10 0,85, min 0,46 - 4 von 80 lagen unter 0,8,
# alle mit viel Leerraum (eingerueckte Ausgaben; Leerzeichenfolgen sind fuer den Tokenizer fast gratis). Feinere Regeln
# (Leerraum getrennt zaehlen) gewannen nur in der Stichprobe, nicht in der 5-fach-Kreuzvalidierung. Folge: eine
# Kuerzung um weniger als ~25 % ist von einem Schaetzfehler nicht zu unterscheiden und bleibt unsichtbar.
_ZIFFERN = "0123456789"


def schaetze_text(text):
    """Tokens eines Texts: ASCII-Ziffern je 1, alles andere ceil(UTF-8-Bytes / 3,5)."""
    if not text:
        return 0
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, separators=(",", ":"))
    ziffern = sum(map(text.count, _ZIFFERN))
    rest = len(text.encode("utf-8", "replace")) - ziffern
    return ziffern + int(-(-rest // BYTES_JE_TOKEN))


def _obergrenze(body):
    """Billige Obergrenze: kein Byte wird zu mehr als einem Token (Ziffern genau 1, CJK 3 Bytes je Token)."""
    n = sum(len(body.get(k) or "") for k in ("system", "prompt", "suffix") if isinstance(body.get(k), str))
    for m in body.get("messages") or []:
        c = m.get("content")
        n += NACHRICHT_AUFSCHLAG + (len(c.encode("utf-8", "replace")) if isinstance(c, str) else len(json.dumps(c or "")))
        if m.get("tool_calls"):
            n += len(json.dumps(m["tool_calls"]))
    if body.get("tools"):
        n += len(json.dumps(body["tools"]))
    return n


def schaetze_body(body):
    """Geschaetzte Prompt-Tokens eines Ollama-Bodys (/api/chat oder /api/generate). Bilder zaehlen nicht mit -
    ihr Preis haengt vom Modell ab und steckt nicht in den Bytes."""
    n = sum(schaetze_text(body.get(k)) for k in ("system", "prompt", "suffix"))
    for m in body.get("messages") or []:
        n += NACHRICHT_AUFSCHLAG + schaetze_text(m.get("content"))
        if m.get("tool_calls"):
            n += schaetze_text(m["tool_calls"])
    if body.get("tools"):
        n += schaetze_text(body["tools"])
    return n


def vorher(body, ctx, is_cloud=False):
    """Schaetzung, wenn die Anfrage num_ctx ueberschreiten koennte, sonst None (auch fuer Cloud/ohne ctx)."""
    if is_cloud or not ctx:
        return None
    if _obergrenze(body) < ctx:
        return None   # passt sicher - die allermeisten Anfragen enden hier ohne Schaetzung
    est = schaetze_body(body)
    return est if est > ctx else None


def nachher(geschaetzt, angekommen, ctx, info):
    """Abgleich nach der Antwort. Liefert True, wenn die Kuerzung bestaetigt und gemeldet wurde."""
    if not geschaetzt or not angekommen:
        return False
    if angekommen >= BESTAETIGT * geschaetzt:
        log.info("Kontextpruefung: geschaetzt %d > num_ctx %d, angekommen %d - Schaetzung zu hoch, keine Kuerzung (%s)",
                 geschaetzt, ctx, angekommen, info.get("model"))
        return False
    quote = angekommen / geschaetzt
    e = {"event": "kontext_gekuerzt", "role": info.get("role"), "model": info.get("model"), "node": info.get("node"),
         "client": info.get("client"), "path": info.get("path"), "num_ctx": ctx, "geschaetzt": geschaetzt,
         "angekommen": angekommen, "quote": round(quote, 2), "sicher": quote < SICHER, "request_id": info.get("request_id")}
    log.warning("Kontext %s gekuerzt: %s schickte ~%d Token an %s (%s, num_ctx %d), angekommen %d (%.0f %%) - Ollama kuerzt still (req=%s)",
                "sicher" if e["sicher"] else "wahrscheinlich", e["client"] or "-", geschaetzt, e["model"], e["node"], ctx,
                angekommen, quote * 100, e["request_id"])
    state.remember(e)
    state.KUERZUNGEN["anzahl"] += 1
    state.KUERZUNGEN["letzte"] = {k: v for k, v in e.items() if k not in ("event", "t")}
    state.MQTT_EVENTS.append({"event_type": "kontext_gekuerzt", **state.KUERZUNGEN["letzte"]})
    del state.MQTT_EVENTS[:-20]
    return True
