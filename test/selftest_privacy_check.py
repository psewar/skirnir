#!/usr/bin/env python3
"""Self-test (router 0.13.0): content check before a cloud tier for a client with personal data (privacy.py).

A client with `privacy_check: true` keeps its cloud tiers as candidates despite the data class `personal`; when a cloud
tier is chosen, rules and the classifier decide for the one request, the house context is redacted, and a request that
must stay local gets the configured refusal as its answer. Checks configuration, the data-class gate, the rules, the
redaction of a Home Assistant live context, fail-closed behaviour, and the wiring in proxy._via_cloud (refusal without
a provider call, redacted body to the provider). Runs without network, classifier or provider."""
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import cloud, config, privacy, proxy, state  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


CFG = privacy.parse_privacy_config({"enabled": True, "terms": ["Lea", "Brunner"], "redact_names": ["Enyaq"]})

LIVE = ("Live Context: An overview of the areas and the devices in this smart home:\n"
        "- names: Bad Temperature\n  domain: sensor\n  state: '22.9'\n  areas: Bad\n  attributes:\n    unit_of_measurement: °C\n"
        "    device_class: temperature\n"
        "- names: Eingang Bewegung\n  domain: binary_sensor\n  state: 'off'\n  attributes:\n    device_class: motion\n"
        "- names: Wocheneinkauf\n  domain: todo\n  state: '4'\n"
        "- names: Enyaq verriegelt\n  domain: binary_sensor\n  state: 'on'\n"
        "- names: Wohnzimmer Temperatur\n  domain: sensor\n  state: '23.0'\n")


def body(question, tool_msgs=()):
    return {"model": "assist:latest", "stream": False, "messages": [
        {"role": "system", "content": "Du bist der Sprachassistent."}, {"role": "user", "content": question}, *tool_msgs]}


async def main():
    # --- configuration ---
    check("valid configuration parses", not failed(CFG) and CFG.enabled and CFG.terms_rx is not None)
    check("endpoint off the host refused", failed(privacy.parse_privacy_config({"endpoint": "http://192.0.2.7:8082"})))
    check("unknown key refused", failed(privacy.parse_privacy_config({"treshold": 0.4})))
    check("threshold outside (0, 1) refused", failed(privacy.parse_privacy_config({"threshold": 1.5})))
    check("defaults: off, built-in redaction lists", not privacy.parse_privacy_config(None).enabled
          and "todo" in privacy.PrivacyConfig().redact_domains)

    # --- data-class gate (cloud.request_block_reason) ---
    state.CFG = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
    state.CFG.cloud = {**state.CFG.cloud, "enabled": True, "privacy_check": CFG}
    state.CFG.client_auth["clients"] = {"ha": {"privacy_check": True}, "other": {}, "ha-internal": {"privacy_check": True, "data_class": "secret"}}
    b = body("Wie warm ist es im Bad?")
    def new_req():   # the fields request_block_reason reads and writes
        return type("R", (), {"execution": "auto", "data_class": None, "cloud_privacy": False, "data_class_effective": None})()
    r = new_req()
    why = cloud.request_block_reason(r, b, "ha")
    check("client with privacy_check: cloud stays a candidate, check pending", why is None and r.cloud_privacy, str(why))
    r2 = new_req()
    check("client without it: blocked as before", cloud.request_block_reason(r2, b, "other") is not None and not r2.cloud_privacy)
    r3 = new_req()
    r3.data_class = "personal"
    check("declared routing.data_class personal: blocked (a declaration is not overruled)",
          cloud.request_block_reason(r3, b, "ha") is not None and not r3.cloud_privacy)
    r4 = new_req()
    check("client class above personal: blocked", cloud.request_block_reason(r4, b, "ha-internal") is not None and not r4.cloud_privacy)

    # --- rules ---
    for text, expect in (("Wann ist Lea zu Hause?", "terms"), ("Schreib an max@example.org", "email"),
                         ("Ruf 079 123 45 67 an", "phone"), ("Überweise auf CH93 0076 2011 6238 5295 7", "iban"),
                         ("Meine AHV ist 756.1234.5678.97", "ahv"), ("Wir wohnen an der Hauptstrasse 12", "address"),
                         ("Mir ist seit gestern schwindlig", "health"), ("Wie ist mein Kontostand?", "finances"),
                         ("Sag mir das Passwort", "credentials")):
        hit = privacy.rule_hit([text], CFG)
        check(f"rule {expect}: {text}", hit == expect, str(hit))
    for text in ("Ist es im Bad wärmer als im Wohnzimmer?", "Wie wird das Wetter morgen?", "Erklär mir die Fotosynthese.",
                 "Wie viel Akku hat Mähi noch?", "Bleiben wir bei Leandro?"):
        check(f"no rule: {text}", privacy.rule_hit([text], CFG) is None, str(privacy.rule_hit([text], CFG)))

    # --- redaction of a live context (Home Assistant: JSON with a result text) ---
    tool = {"role": "tool", "tool_name": "GetLiveContext", "content": json.dumps({"success": True, "result": LIVE}, ensure_ascii=False)}
    todo = {"role": "tool", "tool_name": "todo_get_items", "content": json.dumps({"items": ["Brot", "Milch"]})}
    b = body("Ist es im Bad wärmer als im Wohnzimmer?", [tool, todo])
    red, n = privacy.redact_body(b, CFG)
    txt = json.loads(red["messages"][2]["content"])["result"]
    check("redaction keeps temperatures", "Bad Temperature" in txt and "Wohnzimmer Temperatur" in txt, txt)
    check("redaction removes motion sensor, todo list and named car", "Bewegung" not in txt and "Wocheneinkauf" not in txt
          and "Enyaq" not in txt, txt)
    check("whole result of a listed tool removed", red["messages"][3]["content"] == privacy.REDACTED)
    check("count: 3 entries + 1 tool result", n == 4, str(n))
    check("the original request is unchanged", "Bewegung" in b["messages"][2]["content"])

    # --- check: classifier outcomes, fail closed ---
    calls = []

    async def classify_ok(texts, cfg):
        calls.append(texts)
        return 0.05, 0.4
    privacy.classify = classify_ok
    v = await privacy.check(b, CFG)
    check("neutral question: allowed, redacted copy to send", v.allowed and v.redacted == 4 and v.body is not None
          and calls == [["Ist es im Bad wärmer als im Wohnzimmer?"]], str(v.journal()))

    async def classify_personal(texts, cfg):
        return 0.93, 0.4
    privacy.classify = classify_personal
    v = await privacy.check(body("Wie geht es ihr nach dem Eingriff?"), CFG)
    check("classifier above threshold: stays local", not v.allowed and v.result == "personal" and v.by == "classifier", str(v.journal()))

    async def classify_down(texts, cfg):
        return None, None
    privacy.classify = classify_down
    v = await privacy.check(body("Wie wird das Wetter?"), CFG)
    check("classifier unavailable: stays local (fail closed)", not v.allowed and v.result == "unavailable", str(v.journal()))
    v = await privacy.check(body("Wann kommt Lea heim?"), CFG)
    check("rule hit: no classifier needed, stays local", not v.allowed and v.by == "rules:terms")
    j = json.dumps(v.journal())
    check("journal carries no text", "Lea" not in j and "heim" not in j, j)

    # --- wiring in proxy._via_cloud ---
    sent = []

    async def fake_run(node, path, out, model, stream):
        sent.append(out)
        return {"p_tok": 10, "c_tok": 3}, {"model": model, "message": {"role": "assistant", "content": "Im Wohnzimmer."}, "done": True}
    cloud.run = fake_run

    def relay(question, tools=()):
        x = object.__new__(proxy._Relay)
        x.req = type("Q", (), {"cloud_privacy": True, "request_id": "t1", "explicit": False})()
        x.path, x.out, x.stream, x.model, x.exposed = "/api/chat", body(question, tools), False, "openai:gpt-5-mini", "assist:latest"
        x.node = type("N", (), {"name": "cloud:openai", "is_cloud": True})()
        x.shape, x.headers, x.memory_capture, x.info = None, {}, None, None
        return x

    privacy.classify = classify_ok
    resp = await proxy._Relay._via_cloud(relay("Ist es im Bad wärmer als im Wohnzimmer?", [tool]))
    check("allowed: provider gets the redacted body", len(sent) == 1 and "Bewegung" not in json.dumps(sent[0], ensure_ascii=False)
          and resp.status == 200)
    sent.clear()
    resp = await proxy._Relay._via_cloud(relay("Wann hat Lea den Termin beim Frauenarzt?"))
    ans = json.loads(resp.body)
    check("refused: no provider call, the refusal is the answer", not sent and ans["message"]["content"] == CFG.refusal
          and ans["model"] == "assist:latest" and ans["done"], str(ans)[:200])
    x = relay("Wie wird das Wetter?")
    x.path = "/api/embed"
    check("embeddings with a pending check: next candidate, nothing sent", await proxy._Relay._via_cloud(x) is None and not sent)


asyncio.run(main())
print(f"\n{'ALLE OK' if not FAILS else str(len(FAILS)) + ' FEHLER: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
