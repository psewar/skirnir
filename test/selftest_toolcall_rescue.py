#!/usr/bin/env python3
"""Selbsttest der Tool-Call-Rettung. Laeuft ohne Router und ohne Netz.

Die Positivfaelle sind WOERTLICH die Ausgaben, an denen qwen3-coder:30b am
2026-09-15 durchgefallen ist (Messreihe local-toolcall-context) - kein
nachgebautes Wunschbild. Die Negativfaelle sind wichtiger als die Positivfaelle:
eine Rettung, die zu viel rettet, fuehrt beim Client ein Werkzeug aus.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "router"))
from ollama_router import toolcall_rescue as tr   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


TOOLS = [
    {"type": "function", "function": {"name": "read_note", "parameters": {"type": "object", "properties": {
        "note_id": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "set_light", "parameters": {"type": "object", "properties": {
        "entity": {"type": "string"}, "state": {"type": "string"}, "helligkeit": {"type": "integer"}}}}},
]

# --- genau so kam es aus Ollama zurueck ---
ECHT_XML = ("<function=read_note>\n<parameter=note_id>\nZ7QX-4417-VULPES\n</parameter>\n"
            "</function>\n</tool_call>")
ECHT_XML_EINZEILIG = "<function=read_note> <parameter=note_id> Z7QX-4417-VULPES </parameter> </function> </tool_call>"


def t_positiv():
    for label, txt in [("XML mehrzeilig", ECHT_XML), ("XML einzeilig", ECHT_XML_EINZEILIG)]:
        msg = {"role": "assistant", "content": txt}
        name = tr.rescue_message(msg, TOOLS)
        check("rettet %s" % label, name == "read_note", str(name))
        check("Argument %s" % label,
              msg.get("tool_calls", [{}])[0].get("function", {}).get("arguments") == {"note_id": "Z7QX-4417-VULPES"},
              str(msg.get("tool_calls")))
        check("content geleert %s" % label, msg.get("content") == "", repr(msg.get("content"))[:40])

    msg = {"role": "assistant", "content": '{"name": "read_note", "arguments": {"note_id": "AB-1"}}'}
    check("rettet JSON-Dialekt", tr.rescue_message(msg, TOOLS) == "read_note", str(msg.get("tool_calls")))

    msg = {"role": "assistant", "content": '<tool_call>{"name":"read_note","arguments":{"note_id":"AB-2"}}</tool_call>'}
    check("rettet JSON im tool_call-Mantel", tr.rescue_message(msg, TOOLS) == "read_note", str(msg.get("tool_calls")))

    # Typisierung: XML liefert Text, das Schema sagt integer
    msg = {"role": "assistant", "content": "<function=set_light><parameter=entity>licht.kueche</parameter>"
                                           "<parameter=state>on</parameter><parameter=helligkeit>80</parameter></function>"}
    tr.rescue_message(msg, TOOLS)
    args = msg.get("tool_calls", [{}])[0].get("function", {}).get("arguments", {})
    check("typisiert integer", args.get("helligkeit") == 80, repr(args.get("helligkeit")))


def t_negativ():
    """Hier darf NICHTS passieren."""
    msg = {"role": "assistant", "content": "Die Notiz Z7QX-4417-VULPES habe ich nicht gefunden."}
    check("laesst Prosa in Ruhe", tr.rescue_message(msg, TOOLS) is None and "tool_calls" not in msg)

    # Werkzeug, das der Client gar nicht angeboten hat -> halluziniert, nicht retten
    msg = {"role": "assistant", "content": "<function=run_shell><parameter=command>rm -rf /</parameter></function>"}
    check("rettet KEIN unangebotenes Werkzeug", tr.rescue_message(msg, TOOLS) is None, str(msg.get("tool_calls")))

    # Erklaertext mit beilaeufiger Erwaehnung -> kein Aufruf gewollt
    lang = ("Ich koennte hier read_note verwenden. Ein Aufruf saehe etwa so aus: "
            "<function=read_note><parameter=note_id>X</parameter></function> "
            "Aber dafuer brauche ich erst die Kennung, und die hast du mir nicht genannt. "
            "Nenne mir bitte die Kennung, dann hole ich die Notiz.")
    msg = {"role": "assistant", "content": lang}
    check("rettet nicht aus Erklaertext", tr.rescue_message(msg, TOOLS) is None, str(msg.get("tool_calls")))

    # schon geglueckter Aufruf -> nicht anfassen
    msg = {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read_note", "arguments": {}}}]}
    check("ruehrt echten tool_call nicht an", tr.rescue_message(msg, TOOLS) is None)

    check("ohne Werkzeuge inaktiv", tr.rescue_message({"content": ECHT_XML}, []) is None)
    check("aktiv_fuer: ohne tools nein", tr.aktiv_fuer({"messages": []}) is False)
    check("aktiv_fuer: mit format nein", tr.aktiv_fuer({"tools": TOOLS, "format": {"type": "object"}}) is False)
    check("aktiv_fuer: mit tools ja", tr.aktiv_fuer({"tools": TOOLS}) is True)


def _stream(stuecke, tools=TOOLS, done_extra=None):
    """Spielt Upstream-Chunks durch den Rescuer und gibt die herausgelassenen zurueck."""
    r = tr.StreamRescuer(tools)
    raus = []
    for s in stuecke:
        raus += r.chunk({"model": "m", "message": {"role": "assistant", "content": s}, "done": False})
    letzte = {"model": "m", "message": {"role": "assistant", "content": ""}, "done": True,
              "done_reason": "stop", "eval_count": 12, "prompt_eval_count": 40}
    letzte.update(done_extra or {})
    raus += r.chunk(letzte)
    return r, raus


def t_stream():
    # Aufruf kommt stueckweise -> nichts davon darf als Text beim Client landen
    r, raus = _stream(["<function=read_note>", "<parameter=note_id>", "Z7QX-4417-VULPES",
                       "</parameter>", "</function>"])
    text = "".join((c.get("message") or {}).get("content") or "" for c in raus)
    check("Stream: kein Prosa-Leck", text == "", repr(text)[:80])
    tcs = [c for c in raus if ((c.get("message") or {}).get("tool_calls"))]
    check("Stream: Aufruf gerettet", len(tcs) == 1, str(raus)[:120])
    if tcs:
        fn = tcs[0]["message"]["tool_calls"][0]["function"]
        check("Stream: richtiger Aufruf",
              fn["name"] == "read_note" and fn["arguments"] == {"note_id": "Z7QX-4417-VULPES"}, str(fn))
    check("Stream: Zaehler bleiben am Ende",
          any(c.get("done") and c.get("eval_count") == 12 for c in raus), str(raus)[-140:])

    # Normale Prosa -> vollstaendig und in Reihenfolge durchreichen
    r, raus = _stream(["Hal", "lo ", "Welt"])
    text = "".join((c.get("message") or {}).get("content") or "" for c in raus)
    check("Stream: Prosa unveraendert", text == "Hallo Welt", repr(text))
    check("Stream: genau ein Abschluss", sum(1 for c in raus if c.get("done")) == 1, str(len(raus)))

    # Prosa, die wie ein Aufruf beginnt, dann doch keiner ist
    r, raus = _stream(["{", "\"a\": 1} ", "war nur JSON im Text"])
    text = "".join((c.get("message") or {}).get("content") or "" for c in raus)
    check("Stream: Fehlstart wird nachgereicht", text == '{"a": 1} war nur JSON im Text', repr(text))

    # Ollama schafft den Aufruf selbst -> durchreichen, nichts verschlucken
    r = tr.StreamRescuer(TOOLS)
    raus = r.chunk({"model": "m", "done": False, "message": {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "read_note", "arguments": {"note_id": "Q"}}}]}})
    check("Stream: echter tool_call geht durch",
          len(raus) == 1 and (raus[0].get("message") or {}).get("tool_calls"), str(raus)[:100])

    # ohne Werkzeuge gar nicht erst eingreifen
    r, raus = _stream(["<function=read_note>", "</function>"], tools=[])
    text = "".join((c.get("message") or {}).get("content") or "" for c in raus)
    check("Stream: ohne Werkzeuge unveraendert", text == "<function=read_note></function>", repr(text)[:60])


def t_verloren():
    """Der Fall, den die Stufe NICHT heilt - er soll wenigstens auffallen."""
    fremd = "<function=run_shell><parameter=command>ls</parameter></function>"
    check("verdacht: fremdes Werkzeug faellt auf", tr.verdacht(fremd) is True)
    check("verdacht: Prosa nicht", tr.verdacht("Die Notiz gibt es leider nicht.") is False)
    check("verdacht: leer nicht", tr.verdacht("") is False)
    # Zusammenspiel: nicht gerettet UND verdaechtig = der meldepflichtige Fall
    msg = {"role": "assistant", "content": fremd}
    check("nicht gerettet, aber gemeldet",
          tr.rescue_message(msg, TOOLS) is None and tr.verdacht(msg["content"]) is True)

    # Stream: unbekannter Dialekt -> Text geht raus (nicht verschlucken!) und wird gemeldet
    r, raus = _stream(["<function=run_shell>", "<parameter=command>ls</parameter>", "</function>"])
    text = "".join((c.get("message") or {}).get("content") or "" for c in raus)
    check("Stream: unrettbarer Call wird NICHT verschluckt", text == fremd, repr(text)[:80])
    check("Stream: unrettbarer Call wird gemeldet", r.verloren is True and r.gerettet is None)


if __name__ == "__main__":
    t_positiv()
    t_negativ()
    t_stream()
    t_verloren()
    print()
    if FAILS:
        print("FEHLGESCHLAGEN: " + ", ".join(FAILS))
        sys.exit(1)
    print("alle Pruefungen bestanden")
