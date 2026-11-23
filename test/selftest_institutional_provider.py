#!/usr/bin/env python3
"""Self-test (router 0.34.0, ADR-0099): a cloud provider with `trust: institutional`.

An institutional provider (a model server run by the operator's own organisation under its data protection rules) takes
its own `max_data_class` instead of `router.cloud.max_cloud_data_class`, so a personal request may reach it while every
other provider stays closed. Checks the configuration boundary (unknown trust, the highest class, the web UI cannot set
trust or lift the class to the highest), the data-class gates for accepted and rejected cases (opt-outs, credential
scan, the highest class, a narrower own class, a disabled provider, providers without the trust), the facts in the
provider snapshot, and the wiring in proxy._via_cloud (no content check for a cleared institutional provider, still one
for an ordinary provider). Runs without network, classifier or provider; all names, keys and texts are synthetic."""
import asyncio
import json
import os
import shutil
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import cloud, config, privacy, proxy, request, settings, state  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402
from skirnir_router.shape import OllamaShape  # noqa: E402

EXAMPLE = os.path.join(HERE, "..", "router", "config.example.yaml")
FAILS = []
OA = {"model": "openai:gpt-5-mini", "num_ctx": 8192, "cloud": "openai"}
INST = {"model": "institution:example-model", "num_ctx": 8192, "cloud": "institution"}
os.environ["OPENAI_API_KEY"] = "synthetic-openai-test-value"
os.environ["INSTITUTION_API_KEY"] = "synthetic-institution-test-value"


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


def load(tmp, **provider):
    """The example configuration with the institution provider enabled (fields overridden by `provider`, None removes one)
    and a catalog entry for it, written to tmp and loaded like the router does."""
    with open(EXAMPLE, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    spec = raw["router"]["cloud"]["providers"]["institution"]
    spec["enabled"] = True
    for k, v in provider.items():
        if v is None:
            spec.pop(k, None)
        else:
            spec[k] = v
    raw["models"]["institution:example-model"] = {"cloud": "institution", "provider_model": "example-org/example-model",
                                                  "price_chf_per_m": {"input": 0, "output": 0}}
    path = os.path.join(tmp, "config.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True)
    return config.Config.load(path, use_overrides=False)


def use(cfg):
    state.CFG = cfg
    state.CFG.client_auth["clients"] = {
        "team": {"data_class": "personal"}, "plain": {}, "voice": {"privacy_check": True},
        "optout": {"data_class": "personal", "cloud": False}}
    cloud.setup()


def routed(client, data_class=None, execution="auto", text="Wann ist das Teammeeting am Montag?"):
    r = request.Routing()
    r.request_id, r.execution, r.data_class = "t1", execution, data_class
    r.cloud_block = cloud.request_block_reason(r, {"messages": [{"role": "user", "content": text}]}, client)
    return r


def open_for(r, tier):
    return r.tier_blocked(tier) is None


async def main(tmp):
    # --- configuration boundary ---
    cfg = load(tmp)
    check("example with an enabled institutional provider loads", not failed(cfg), getattr(cfg, "message", ""))
    check("trust defaults to cloud, institutional where set",
          cfg.cloud["providers"]["openai"]["trust"] == "cloud" and cfg.cloud["providers"]["institution"]["trust"] == "institutional")
    bad = load(tmp, trust="trusted")
    check("unknown trust refused", failed(bad) and "trust must be one of" in bad.message, getattr(bad, "message", ""))
    bad = load(tmp, max_data_class="secret")
    check("institutional provider with the highest class refused", failed(bad) and "highest data class" in bad.message,
          getattr(bad, "message", ""))
    with open(EXAMPLE, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw["router"]["cloud"]["data_classes"] = ["public", "internal", "confidential"]
    raw["router"]["cloud"]["default_data_class"] = "confidential"
    raw["router"]["cloud"]["providers"]["institution"]["max_data_class"] = "confidential"
    path = os.path.join(tmp, "config.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True)
    bad = config.Config.load(path, use_overrides=False)
    check("highest class of a custom class list refused too", failed(bad) and "(confidential)" in bad.message,
          getattr(bad, "message", ""))
    p = settings.coerce("router.cloud.providers.institution.trust", "institutional", {"router": {"cloud": {"providers": {"institution": {}}}}})
    check("the web UI cannot set trust (ADR-0016)", failed(p) and "config.yaml only" in p.message, getattr(p, "message", ""))
    cfg = load(tmp)
    probe = cfg.reload({"settings": {"router.cloud.providers.institution.max_data_class": "secret"}})
    check("the web UI cannot lift an institutional provider to the highest class", failed(probe) and "highest data class" in probe.message,
          getattr(probe, "message", ""))
    lifted = cfg.reload({"settings": {"router.cloud.providers.openai.max_data_class": "personal"}})
    check("an ordinary provider's own class above the global limit still loads (it only narrows)", lifted is None,
          getattr(lifted, "message", ""))
    use(cfg)
    check("... and its ceiling stays at the global limit", state.CLOUD["openai"].snapshot()["ceiling"] == "internal")
    check("... so personal data stays away from it", not open_for(routed("team"), OA))

    # --- facts ---
    cfg = load(tmp)
    use(cfg)
    snap = cloud.view()
    check("snapshot reports trust and ceiling", snap["institution"]["trust"] == "institutional" and snap["institution"]["ceiling"] == "personal"
          and snap["openai"]["trust"] == "cloud" and snap["openai"]["ceiling"] == "internal", json.dumps({k: (v["trust"], v["ceiling"]) for k, v in snap.items()}))

    # --- accepted ---
    r = routed("team")
    check("client with the class personal: institutional tier open", r.cloud_block is None and open_for(r, INST), r.tier_blocked(INST))
    check("... ordinary provider closed with the reason", "personal > internal for provider openai" in (r.tier_blocked(OA) or ""),
          r.tier_blocked(OA))
    r = routed("plain")
    check("undeclared request (default personal): institutional open, ordinary closed", open_for(r, INST) and not open_for(r, OA))
    r = routed("plain", data_class="internal")
    check("declared internal: both open as before", open_for(r, INST) and open_for(r, OA))

    # --- rejected ---
    r = routed("plain", data_class="secret")
    check("the highest class reaches no provider", r.cloud_block is not None and not open_for(r, INST) and not open_for(r, OA), r.cloud_block)
    r = routed("team", execution="local")
    check("routing.execution: local closes the institutional tier", r.cloud_block is not None and not open_for(r, INST), r.cloud_block)
    r = routed("optout")
    check("clients.<c>.cloud: false closes the institutional tier", r.cloud_block is not None and not open_for(r, INST), r.cloud_block)
    r = routed("team", text="Hier ist es: password: synthetic-not-a-real-one")
    check("credential scan closes the institutional tier", r.cloud_block is not None and "credential_scan" in r.cloud_block, r.cloud_block)

    use(load(tmp, max_data_class="public"))
    r = routed("plain", data_class="internal")
    check("a narrower own class still narrows an institutional provider", not open_for(r, INST) and open_for(r, OA), r.tier_blocked(INST))
    use(load(tmp, max_data_class=None))
    r = routed("team")
    check("institutional without its own class: global limit, personal closed", r.cloud_block is not None and not open_for(r, INST), r.cloud_block)
    use(load(tmp, enabled=False))
    r = routed("team")
    check("a disabled institutional provider widens nothing", r.cloud_block is not None
          and "personal > internal (not cleared for the cloud)" in r.cloud_block, r.cloud_block)

    # --- content check (privacy.py) per provider ---
    use(load(tmp))
    r = routed("voice")
    check("client with privacy_check: both tiers candidates", r.cloud_privacy and open_for(r, INST) and open_for(r, OA))
    check("... check needed for the ordinary provider only", cloud.needs_privacy_check(state.CLOUD["openai"], r)
          and not cloud.needs_privacy_check(state.CLOUD["institution"], r))
    use(load(tmp, max_data_class="internal"))
    r = routed("voice")
    check("institutional cleared only up to internal: personal closed despite the pending check", not open_for(r, INST), r.tier_blocked(INST))

    # --- wiring in proxy._via_cloud ---
    use(load(tmp))
    sent, checked = [], []

    async def fake_run(node, path, out, model, stream):
        sent.append((node.provider, out))
        return {"p_tok": 5, "c_tok": 2}, {"model": model, "message": {"role": "assistant", "content": "Um neun Uhr."}, "done": True}

    async def fake_check(body, cfg):
        checked.append(body)
        return privacy.Verdict(False, "personal", by="rules:terms")
    cloud.run, privacy.check = fake_run, fake_check

    def relay(provider, model):
        x = object.__new__(proxy._Relay)
        x.req = routed("voice")
        x.path, x.stream, x.model, x.exposed = "/api/chat", False, model, "assist:latest"
        x.out = {"model": "assist:latest", "stream": False, "messages": [{"role": "user", "content": "Wann ist das Teammeeting?"}]}
        x.node = state.CLOUD[provider]
        x.shape, x.headers, x.memory_capture, x.info = OllamaShape(), {}, None, None
        return x

    resp = await proxy._Relay._via_cloud(relay("institution", INST["model"]))
    check("institutional provider: no content check, the body goes out unchanged",
          resp.status == 200 and not checked and len(sent) == 1 and sent[0][0] == "institution", f"checked {len(checked)} sent {len(sent)}")
    sent.clear()
    resp = await proxy._Relay._via_cloud(relay("openai", OA["model"]))
    check("ordinary provider: content check runs and its refusal keeps the request local", len(checked) == 1 and not sent
          and json.loads(resp.body)["message"]["content"] == state.CFG.cloud["privacy_check"].refusal)


tmp = tempfile.mkdtemp(prefix="skirnir-institutional-")
try:
    asyncio.run(main(tmp))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{'ALLE OK' if not FAILS else str(len(FAILS)) + ' FEHLER: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
