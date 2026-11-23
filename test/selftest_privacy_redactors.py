#!/usr/bin/env python3
"""Self-test (router 0.42.0, ADR-0119, ADR-0125): context redactors per tool format and locale packs for the content
check before a cloud tier (privacy.py, redactors.py, privacy_locale.py).

1. Default unchanged: a corpus of synthetic payloads (rules, Home Assistant live context in JSON and as text, other tool
   formats, listed tools, classifier outcomes, several configurations) must give byte for byte the redacted body, rule
   hit and verdict that `privacy.py` gave before router 0.42.0. The expected values in `privacy_default_corpus.json` were
   recorded with the code of the base commit (router 0.34.1); `--record` writes them again with the code on the path,
   which is only right when a later ADR changes the default on purpose.
2. Rejected cases under the default configuration: every sensitive pattern and keyword group keeps the request local,
   and every default domain, device class and listed tool is redacted.
3. The new packs and redactors: `en` hits its own test sentences and nothing of the neutral ones, `generic_json` removes
   configured paths and keys, a named tool whose result is not JSON keeps the request local, `keep_local` keeps an
   unrecognised result local, and the configuration refuses what would silently redact nothing.
4. Wiring: a request kept local by the redaction gets the refusal and nothing reaches the provider.
Runs without network, classifier or provider."""
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import cloud, privacy, proxy  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402
from skirnir_router.shape import OllamaShape  # noqa: E402

CORPUS = os.path.join(HERE, "privacy_default_corpus.json")
FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


# --- synthetic corpus (no real data: example domains, test IBAN, invented names and devices) ---

LIVE = ("Live Context: An overview of the areas and the devices in this smart home:\n"
        "- names: Bad Temperatur\n  domain: sensor\n  state: '22.9'\n  areas: Bad\n  attributes:\n    unit_of_measurement: °C\n"
        "    device_class: temperature\n"
        "- names: Flur Bewegung\n  domain: binary_sensor\n  state: 'off'\n  attributes:\n    device_class: motion\n"
        "- names: Einkaufsliste\n  domain: todo\n  state: '4'\n"
        "- names: Beispielauto verriegelt\n  domain: binary_sensor\n  state: 'on'\n"
        "- names: Haustuer\n  domain: binary_sensor\n  state: 'off'\n  attributes:\n    device_class: door\n"
        "- names: Person A\n  domain: person\n  state: home\n"
        "- names: Wohnzimmer Temperatur\n  domain: sensor\n  state: '23.0'\n")
LIVE_ALL_DEFAULTS = "Live Context:\n" + "".join(
    f"- names: Geraet {d}\n  domain: {d}\n  state: 'x'\n" for d in privacy.PrivacyConfig().redact_domains) + "".join(
    f"- names: Sensor {c}\n  domain: binary_sensor\n  state: 'on'\n  attributes:\n    device_class: {c}\n"
    for c in privacy.PrivacyConfig().redact_device_classes) + "- names: Lampe Kueche\n  domain: light\n  state: 'on'\n"


def tool(content, name="GetLiveContext", key="tool_name"):
    return {"role": "tool", key: name, "content": content}


def body(question, *tools, prompt=None):
    if prompt is not None:
        return {"model": "assist:latest", "prompt": prompt}
    return {"model": "assist:latest", "stream": False, "messages": [
        {"role": "system", "content": "Du bist der Sprachassistent."}, {"role": "user", "content": question}, *tools]}


HA_JSON = json.dumps({"success": True, "result": LIVE}, ensure_ascii=False)
HA_JSON_ASCII = json.dumps({"success": True, "result": LIVE})
INTENT = json.dumps({"speech": {"plain": {"speech": "Erledigt"}}, "response_type": "action_done",
                     "data": {"targets": [{"name": "Kueche"}], "success": [], "failed": []}})
NEUTRAL = "Ist es im Bad wärmer als im Wohnzimmer?"
CFG_TERMS = {"enabled": True, "terms": ["Lea", "Brunner"], "redact_names": ["Beispielauto"]}

CASES = [
    # (name, configuration, classifier probability or None = no answer, body)
    ("neutral, no tools", {"enabled": True}, 0.05, body("Wie wird das Wetter morgen?")),
    ("ha json live context", {"enabled": True}, 0.05, body(NEUTRAL, tool(HA_JSON))),
    ("ha json ascii-escaped", {"enabled": True}, 0.05, body(NEUTRAL, tool(HA_JSON_ASCII))),
    ("ha plain-text live context", {"enabled": True}, 0.05, body(NEUTRAL, tool(LIVE))),
    ("ha all default domains and classes", {"enabled": True}, 0.05, body(NEUTRAL, tool(LIVE_ALL_DEFAULTS))),
    ("ha named entity", CFG_TERMS, 0.05, body(NEUTRAL, tool(HA_JSON))),
    ("listed tool by tool_name", {"enabled": True}, 0.05, body(NEUTRAL, tool('{"items": ["Brot"]}', "todo_get_items"))),
    ("listed tool by name", {"enabled": True}, 0.05, body(NEUTRAL, tool('{"items": ["Brot"]}', "todo_get_items", "name"))),
    ("operator listed tool", {"enabled": True, "redact_tools": ["weather"]}, 0.05, body(NEUTRAL, tool("Sonnig", "weather"))),
    ("intent result json", {"enabled": True}, 0.05, body("Mach das Licht an", tool(INTENT, "HassTurnOn"))),
    ("plain text result", {"enabled": True}, 0.05, body("Wie spät ist es?", tool("Es ist 14:05.", "GetTime"))),
    ("json list result", {"enabled": True}, 0.05, body(NEUTRAL, tool(json.dumps([{"result": LIVE}]), "list_tool"))),
    ("json result not text", {"enabled": True}, 0.05, body(NEUTRAL, tool('{"result": 5}', "calc"))),
    ("non-string content", {"enabled": True}, 0.05, body(NEUTRAL, tool([{"type": "text", "text": LIVE}]))),
    ("null content", {"enabled": True}, 0.05, body(NEUTRAL, tool(None))),
    ("several tools", {"enabled": True}, 0.05, body(NEUTRAL, tool(HA_JSON), tool(LIVE, "other"), tool("[]", "todo_get_items"))),
    ("custom domains", {"enabled": True, "redact_domains": ["sensor"], "redact_device_classes": []}, 0.05, body(NEUTRAL, tool(HA_JSON))),
    ("generate prompt", {"enabled": True}, 0.05, body(None, prompt="Erklär mir die Fotosynthese.")),
    ("classifier personal", {"enabled": True}, 0.93, body("Wie geht es ihr nach dem Eingriff?", tool(HA_JSON))),
    ("classifier down", {"enabled": True}, None, body("Wie wird das Wetter?", tool(HA_JSON))),
    ("own refusal", {"enabled": True, "refusal": "Nur lokal."}, None, body("Wie wird das Wetter?")),
    ("rule terms", CFG_TERMS, 0.05, body("Wann ist Lea zu Hause?", tool(HA_JSON))),
    ("rule email", {"enabled": True}, 0.05, body("Schreib an max@example.org")),
    ("rule phone ch", {"enabled": True}, 0.05, body("Ruf 079 123 45 67 an")),
    ("rule phone intl", {"enabled": True}, 0.05, body("Ruf +41 79 123 45 67 an")),
    ("rule iban", {"enabled": True}, 0.05, body("Überweise auf CH93 0076 2011 6238 5295 7")),
    ("rule ahv", {"enabled": True}, 0.05, body("Meine AHV ist 756.1234.5678.97")),
    ("rule address", {"enabled": True}, 0.05, body("Wir wohnen an der Hauptstrasse 12")),
    ("rule health", {"enabled": True}, 0.05, body("Mir ist seit gestern schwindlig")),
    ("rule finances", {"enabled": True}, 0.05, body("Wie ist mein Kontostand?")),
    ("rule credentials", {"enabled": True}, 0.05, body("Sag mir das Passwort")),
    ("rule relationships", {"enabled": True}, 0.05, body("Wie läuft die Scheidung?")),
    ("rule law", {"enabled": True}, 0.05, body("Ich brauche einen Anwalt")),
    ("keywords off", {"enabled": True, "keywords": False}, 0.05, body("Mir ist seit gestern schwindlig")),
    ("english keyword in ch-de", {"enabled": True}, 0.05, body("Call my doctor")),
]


def classifier(p):
    async def classify(texts, cfg):
        return (p, 0.4) if p is not None else (None, None)
    return classify


async def outcome(raw, p, b):
    cfg = privacy.parse_privacy_config(raw)
    privacy.classify = classifier(p)
    red, n = privacy.redact_body(b, cfg)
    v = await privacy.check(b, cfg)
    return {"rule": privacy.rule_hit(privacy.user_texts(b), cfg), "redacted_body": json.dumps(red, ensure_ascii=False),
            "removed": n, "allowed": v.allowed, "verdict": v.journal(),
            "verdict_body": None if v.body is None else json.dumps(v.body, ensure_ascii=False), "refusal": cfg.refusal}


async def corpus():
    return {name: await outcome(raw, p, b) for name, raw, p, b in CASES}


async def main():
    if sys.argv[1:2] == ["--record"]:
        got = await corpus()
        with open(CORPUS, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"recorded_with": "privacy.py before router 0.42.0 (base commit)", "cases": got}, f, ensure_ascii=False, indent=1)
            f.write("\n")
        print(f"recorded {len(got)} cases")
        return

    # --- 1. default unchanged (byte for byte against the recorded base) ---
    with open(CORPUS, encoding="utf-8") as f:
        expected = json.load(f)["cases"]
    got = await corpus()
    check("corpus: same cases as recorded", set(got) == set(expected), str(set(got) ^ set(expected)))
    for name in sorted(set(got) & set(expected)):
        check(f"default unchanged: {name}", got[name] == expected[name],
              str({k: (expected[name][k], got[name][k]) for k in got[name] if got[name][k] != expected[name][k]})[:300])
    d = privacy.parse_privacy_config({"enabled": True})
    check("default: locale ch-de, redactor ha_assist, unrecognised results pass",
          d.locale == "ch-de" and d.redactors == ("ha_assist",) and d.unrecognised_tool_results == "pass" and d.generic_json is None)
    check("default: refusal and marker as before", d.refusal == privacy.DEFAULT_REFUSAL
          and privacy.DEFAULT_REFUSAL.startswith("Das beantworte ich nur lokal") and privacy.REDACTED == "[fuer die Cloud entfernt]")
    check("default pack holds the earlier patterns", list(privacy.PATTERNS) == ["email", "phone", "iban", "ahv", "address"]
          and list(privacy.KEYWORDS) == ["health", "finances", "credentials", "relationships", "law"])

    # --- 2. rejected cases under the default configuration ---
    for text, rule in (("Mail an jemand@example.com", "email"), ("Tel. 044 123 45 67", "phone"), ("Nummer 0041 79 123 45 67", "phone"),
                       ("IBAN DE89 3704 0044 0532 0130 00", "iban"), ("AHV 756 1234 5678 97", "ahv"),
                       ("Am Rosenweg 3a", "address"), ("Termin beim Arzt", "health"), ("Meine Schulden", "finances"),
                       ("Der PIN ist", "credentials"), ("Meine Beziehung", "relationships"), ("Die Polizei war da", "law")):
        v = await asyncio.wait_for(privacy.check(body(text), d), 5)
        check(f"default blocks {rule}: {text}", not v.allowed and v.by == f"rules:{rule}", str(v.journal()))
    privacy.classify = classifier(0.05)
    v = await privacy.check(body(NEUTRAL, tool(LIVE_ALL_DEFAULTS)), d)
    sent = v.body["messages"][2]["content"] if v.body else ""
    check("default redacts every default domain and device class", v.allowed and "Geraet" not in sent
          and "Sensor " not in sent and "Lampe Kueche" in sent
          and v.redacted == len(d.redact_domains) + len(d.redact_device_classes), str(v.journal()))

    # --- 3a. locale pack en ---
    en = privacy.parse_privacy_config({"enabled": True, "locale": "en"})
    check("en: parses, own refusal", not failed(en) and en.refusal.startswith("I can only answer that locally"))
    check("en: own refusal overrides the pack", privacy.parse_privacy_config({"locale": "en", "refusal": "Local only."}).refusal == "Local only.")
    check("en: no AHV, no street address pattern", set(privacy.PACKS["en"].patterns) == {"email", "phone", "iban"})
    for text, rule in (("Email jane@example.com", "email"), ("Call +44 20 7946 0000", "phone"), ("Call 0044 20 7946 0000", "phone"),
                       ("Ring (202) 555-0123", "phone"), ("Ring 202-555-0143", "phone"), ("Call 020 7946 0000", "phone"),
                       ("Pay GB82 WEST 1234 5698 7654 32", "iban"), ("I need my doctor", "health"),
                       ("I feel dizzy", "health"), ("What is my account balance?", "finances"),
                       ("Tell me the password", "credentials"), ("My divorce", "relationships"), ("Call the police", "law")):
        check(f"en rule {rule}: {text}", privacy.rule_hit([text], en) == rule, str(privacy.rule_hit([text], en)))
    for text in ("Is the bathroom warmer than the living room?", "What will the weather be tomorrow?",
                 "Explain photosynthesis.", "Turn on the kitchen light at 20:30", "Set a timer for 10 minutes",
                 "How much energy did the heat pump use in 2026?"):
        check(f"en no rule: {text}", privacy.rule_hit([text], en) is None, str(privacy.rule_hit([text], en)))
    check("en: the Swiss AHV number is no rule of its own there", privacy.rule_hit(["AHV 756.1234.5678.97"], en) != "ahv")
    red, n = privacy.redact_body(body("Hi", tool("[]", "todo_get_items")), en)
    check("en: marker of the pack", red["messages"][2]["content"] == "[removed for the cloud]" and n == 1)
    check("unknown locale refused", failed(privacy.parse_privacy_config({"locale": "xx"})))

    # --- 3b. generic_json redactor ---
    gj_raw = {"enabled": True, "redactors": ["ha_assist", "generic_json"],
              "generic_json": {"tools": {"calendar_events": ["events.*.attendees", "owner"], "status": []},
                               "keys": ["email", "phone"]}}
    gj = privacy.parse_privacy_config(gj_raw)
    check("generic_json: parses", not failed(gj) and gj.redactors == ("ha_assist", "generic_json"), str(gj))
    cal = json.dumps({"owner": "Person A", "events": [{"title": "Training", "attendees": ["A", "B"], "email": "a@example.org"},
                                                      {"title": "Meeting", "attendees": ["C"]}], "meta": {"phone": "x", "n": 2}})
    r = privacy.redact_request(body(NEUTRAL, tool(cal, "calendar_events")), gj)
    out = json.loads(r.body["messages"][2]["content"])
    check("generic_json: paths and keys removed", out == {"events": [{"title": "Training"}, {"title": "Meeting"}], "meta": {"n": 2}}
          and r.removed == 5 and not r.keep_local, f"{out} {r.removed}")
    r = privacy.redact_request(body(NEUTRAL, tool(cal, "calendar_events", "name")), gj)
    check("generic_json: tool matched by name too", "attendees" not in r.body["messages"][2]["content"])
    r = privacy.redact_request(body(NEUTRAL, tool("owner: Person A", "calendar_events")), gj)
    check("generic_json: named tool whose result is not JSON keeps the request local",
          r.keep_local == "redaction:generic_json", r.keep_local)
    r = privacy.redact_request(body(NEUTRAL, tool('"just a string"', "calendar_events")), gj)
    check("generic_json: named tool with a JSON scalar keeps the request local", r.keep_local == "redaction:generic_json")
    r = privacy.redact_request(body(NEUTRAL, tool('{"ok": true}', "status")), gj)
    check("generic_json: listed tool with nothing to drop passes unchanged",
          r.body["messages"][2]["content"] == '{"ok": true}' and r.removed == 0 and not r.keep_local)
    r = privacy.redact_request(body(NEUTRAL, tool(HA_JSON), tool("Sonnig", "weather")), gj)
    check("generic_json: ha_assist still redacts, other tools untouched", "Flur Bewegung" not in r.body["messages"][2]["content"]
          and r.body["messages"][3]["content"] == "Sonnig" and not r.keep_local)
    star = privacy.parse_privacy_config({"enabled": True, "redactors": ["generic_json"],
                                         "generic_json": {"tools": {"*": ["secret"]}, "keys": ["email"]}})
    r = privacy.redact_request(body(NEUTRAL, tool('{"secret": 1, "list": [{"email": "a@example.org"}]}', "any"),
                                    tool("plain text", "other")), star)
    check("generic_json *: every JSON result, plain text is just not its format",
          json.loads(r.body["messages"][2]["content"]) == {"list": [{}]} and r.body["messages"][3]["content"] == "plain text"
          and r.removed == 2 and not r.keep_local)
    ha_only = privacy.redact_request(body(NEUTRAL, tool(HA_JSON)), star)
    check("generic_json alone: no Home Assistant parsing", ha_only.body["messages"][2]["content"] == HA_JSON)
    deep = "[" * 100000 + "]" * 100000
    v = await privacy.check(body(NEUTRAL, tool(deep)), d)
    check("default: JSON too deep to parse keeps the request local (no internal error)", not v.allowed
          and v.by == "redaction:ha_assist", str(v.journal()))
    # the depth limit is counted on the text, not left to the json module (whose limit differs between Python versions)
    from skirnir_router import redactors
    calls = []
    real_loads = redactors.json.loads
    redactors.json.loads = lambda s: calls.append(len(s)) or real_loads(s)
    at_limit = "[" * redactors.MAX_PARSE_DEPTH + "]" * redactors.MAX_PARSE_DEPTH
    over = '{"a":' * redactors.MAX_PARSE_DEPTH + "[1]" + "}" * redactors.MAX_PARSE_DEPTH
    r_over = privacy.redact_request(body(NEUTRAL, tool(over)), d)
    check("default: 513 levels are not parsed at all and keep the request local", r_over.keep_local == "redaction:ha_assist"
          and not calls, f"{r_over.keep_local} {calls}")
    r_at = privacy.redact_request(body(NEUTRAL, tool(at_limit)), d)
    check("default: 512 levels are parsed and pass as before", not r_at.keep_local and calls == [len(at_limit)], str(calls))
    redactors.json.loads = real_loads
    in_string = json.dumps({"result": "[" * 5000 + "Live Context:\n- names: Haustuer\n  domain: lock\n"})
    r = privacy.redact_request(body(NEUTRAL, tool(in_string)), d)
    check("default: brackets inside JSON strings do not count", not r.keep_local and r.removed == 1, f"{r.keep_local} {r.removed}")
    r = privacy.redact_request(body(NEUTRAL, tool("Note: " + "[" * 5000, "notes")), d)
    check("default: plain text with many brackets is not JSON and passes as before", not r.keep_local
          and r.body["messages"][2]["content"] == "Note: " + "[" * 5000)
    nested = lambda n: json.dumps(json.loads("[" * (n - 1) + '{"email": 1}' + "]" * (n - 1)))  # noqa: E731
    r = privacy.redact_request(body(NEUTRAL, tool(nested(101), "any")), star)
    check("generic_json: deeper than its walk limit keeps the request local", r.keep_local == "redaction:generic_json")
    r = privacy.redact_request(body(NEUTRAL, tool(nested(100), "any")), star)
    check("generic_json: at the walk limit still redacted", not r.keep_local and r.removed == 1, f"{r.keep_local} {r.removed}")
    v = await privacy.check(body(NEUTRAL, tool("owner: Person A", "calendar_events")), gj)
    check("check: unredactable result -> stays local, nothing to send", not v.allowed and v.result == "unredacted"
          and v.by == "redaction:generic_json" and v.body is None, str(v.journal()))

    # --- 3c. unrecognised_tool_results: keep_local (ADR-0125) ---
    strict = privacy.parse_privacy_config({"enabled": True, "unrecognised_tool_results": "keep_local"})
    for name, msg in (("plain text", tool("Es ist 14:05.", "GetTime")), ("intent json", tool(INTENT, "HassTurnOn")),
                      ("non-string", tool([{"type": "text", "text": "x"}])), ("null", tool(None))):
        v = await privacy.check(body(NEUTRAL, msg), strict)
        check(f"keep_local: unrecognised {name} stays local", not v.allowed and v.by == "redaction:unrecognised", str(v.journal()))
    for name, msg in (("ha json", tool(HA_JSON)), ("ha text", tool(LIVE)), ("listed tool", tool("[]", "todo_get_items"))):
        v = await privacy.check(body(NEUTRAL, msg), strict)
        check(f"keep_local: recognised {name} passes", v.allowed, str(v.journal()))
    v = await privacy.check(body(NEUTRAL), strict)
    check("keep_local: no tool results passes", v.allowed)
    strict_gj = privacy.parse_privacy_config({**gj_raw, "unrecognised_tool_results": "keep_local"})
    v = await privacy.check(body(NEUTRAL, tool(INTENT, "status")), strict_gj)
    check("keep_local: a tool listed for generic_json counts as recognised", v.allowed, str(v.journal()))
    v = await privacy.check(body(NEUTRAL, tool(INTENT, "HassTurnOn")), strict_gj)
    check("keep_local: an unlisted one does not", not v.allowed and v.by == "redaction:unrecognised")

    # --- 3d. configuration refuses what would silently redact nothing ---
    for name, raw in (("empty redactors", {"redactors": []}), ("unknown redactor", {"redactors": ["openhab"]}),
                      ("duplicate redactor", {"redactors": ["ha_assist", "ha_assist"]}),
                      ("redactors not a list", {"redactors": "ha_assist"}),
                      ("generic_json without the redactor", {"generic_json": {"tools": {"x": []}}}),
                      ("the redactor without generic_json", {"redactors": ["generic_json"]}),
                      ("generic_json without tools", {"redactors": ["generic_json"], "generic_json": {"keys": ["a"]}}),
                      ("generic_json unknown key", {"redactors": ["generic_json"], "generic_json": {"tools": {"x": []}, "paths": []}}),
                      ("generic_json empty segment", {"redactors": ["generic_json"], "generic_json": {"tools": {"x": ["a..b"]}}}),
                      ("generic_json path not text", {"redactors": ["generic_json"], "generic_json": {"tools": {"x": [3]}}}),
                      ("generic_json keys not a list", {"redactors": ["generic_json"], "generic_json": {"tools": {"x": []}, "keys": "a"}}),
                      ("unknown unrecognised mode", {"unrecognised_tool_results": "drop"}),
                      ("locale null", {"locale": None})):
        check(f"config refused: {name}", failed(privacy.parse_privacy_config(raw)))
    from skirnir_router import settings
    check("no privacy_check key is editable from the web UI", all(settings.spec_for(f"router.cloud.privacy_check.{k}") is None
                                                                 for k in privacy.FIELDS))
    try:
        privacy.redact_request(body(NEUTRAL, tool(HA_JSON)), privacy.PrivacyConfig(redactors=("generic_json",)))
        unparsed = False
    except ValueError:   # exception-ok: the test expects the programming-error guard
        unparsed = True
    check("an unparsed config with other redactors is a programming error, not a silent default", unparsed)

    # --- 4. wiring: redaction keeps the request local -> refusal, nothing sent ---
    from skirnir_router import state, config
    state.CFG = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
    sent = []

    async def fake_run(node, path, out, model, stream):
        sent.append(out)
        return {"p_tok": 1, "c_tok": 1}, {"model": model, "message": {"role": "assistant", "content": "ok"}, "done": True}
    cloud.run = fake_run

    def relay(b):
        x = object.__new__(proxy._Relay)
        x.req = type("Q", (), {"cloud_privacy": True, "data_class_effective": "personal", "request_id": "t1", "explicit": False})()
        x.path, x.out, x.stream, x.model, x.exposed = "/api/chat", b, False, "openai:gpt-5-mini", "assist:latest"
        x.node = type("N", (), {"name": "cloud:openai", "is_cloud": True, "spec": {"kind": "openai", "trust": "cloud"}})()
        x.shape, x.headers, x.memory_capture, x.info = OllamaShape(), {}, None, None
        return x

    state.CFG.cloud = {**state.CFG.cloud, "enabled": True, "privacy_check": gj}
    resp = await proxy._Relay._via_cloud(relay(body(NEUTRAL, tool("owner: Person A", "calendar_events"))))
    check("proxy: unredactable result -> refusal, no provider call", not sent
          and json.loads(resp.body)["message"]["content"] == gj.refusal)
    resp = await proxy._Relay._via_cloud(relay(body(NEUTRAL, tool(cal, "calendar_events"))))
    check("proxy: allowed -> provider gets the generic_json-redacted body", len(sent) == 1
          and "attendees" not in json.dumps(sent[0]) and "Person A" not in json.dumps(sent[0]) and resp.status == 200)
    sent.clear()
    state.CFG.cloud = {**state.CFG.cloud, "privacy_check": en}
    privacy.classify = classifier(None)
    resp = await proxy._Relay._via_cloud(relay(body("What is the weather?")))
    check("proxy: en pack refusal is the answer", not sent and json.loads(resp.body)["message"]["content"] == en.refusal)


asyncio.run(main())
if sys.argv[1:2] != ["--record"]:
    print(f"\n{'ALL OK' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)
