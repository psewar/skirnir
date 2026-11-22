#!/usr/bin/env python3
"""Self-test (router 0.37.0, ADR-0105): locale-neutral router defaults.

- Cost keys without a currency in their name: `router.cloud.providers.<p>.budget_month` and `models.<m>.price_per_m`, with
  the old names `budget_month_chf` and `price_chf_per_m` accepted as aliases in config.yaml, in roles.yaml (catalogue
  entries and the settings block the web UI writes) and in `PUT /admin/config`. Rule for both names in one place: equal
  values are accepted, different values are refused with both names in the message. Layers are normalised one by one, so
  a neutral key in roles.yaml overrides the old name in config.yaml (and the other way round).
- `router.cloud.currency` (default CHF, three capitals) names the unit; wire fields and metric names keep `_chf`.
- `router.benchmark.prompt`: default unchanged (the prompt of all earlier measurements), configurable, recorded per result.
- `decision_engine.observe_light`: the default lives in the configuration (absent = [standard, assist] among the options),
  an explicit list is taken as given, an empty list means none; fallback_observation reads only the configuration.
- English messages in core modules (tunnel, node requests, update status, node name fallback).

Runs without router and network; all names, keys and texts are synthetic."""
import asyncio
import copy
import os
import sys
import tempfile
import types

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import admin, cloud, common, config, cost_keys, fallback_observation, perf, settings, state  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402

EXAMPLE = os.path.join(HERE, "..", "router", "config.example.yaml")
FAILS = []
os.environ["OPENAI_API_KEY"] = "synthetic-openai-test-value"


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


def example():
    with open(EXAMPLE, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw["router"].pop("state_dir", None)   # roles.yaml next to the test's config.yaml
    return raw


def write(tmp, raw, roles=None):
    path = os.path.join(tmp, "config.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True)
    rpath = os.path.join(tmp, "roles.yaml")
    if roles is None:
        if os.path.exists(rpath):
            os.remove(rpath)
    else:
        with open(rpath, "w", encoding="utf-8") as f:
            yaml.safe_dump(roles, f, allow_unicode=True)
    return path


def load(tmp, raw, roles=None):
    return config.Config.load(write(tmp, raw, roles), use_overrides=roles is not None)


def provider(raw, **fields):
    spec = raw["router"]["cloud"]["providers"]["openai"]
    spec.pop("budget_month", None)
    spec.update(fields)
    return raw


def price(raw, model="openai:gpt-5-mini", **fields):
    entry = raw["models"][model]
    entry.pop("price_per_m", None)
    entry.update(fields)
    return raw


def cost_keys_in_config(tmp):
    cfg = load(tmp, provider(example(), budget_month_chf=7))
    check("config.yaml: old budget_month_chf is read as budget_month", not failed(cfg)
          and cfg.cloud["providers"]["openai"].get("budget_month") == 7 and "budget_month_chf" not in cfg.cloud["providers"]["openai"])
    cfg = load(tmp, provider(example(), budget_month=8))
    check("config.yaml: neutral budget_month", not failed(cfg) and cfg.cloud["providers"]["openai"]["budget_month"] == 8)
    cfg = load(tmp, provider(example(), budget_month=9, budget_month_chf=9))
    check("config.yaml: both names with the same value are accepted", not failed(cfg)
          and cfg.cloud["providers"]["openai"]["budget_month"] == 9)
    cfg = load(tmp, provider(example(), budget_month=9, budget_month_chf=5))
    check("config.yaml: both names with different values are refused, naming both", failed(cfg) and cfg.status == 400
          and "budget_month_chf" in cfg.message and "budget_month=" in cfg.message, getattr(cfg, "message", ""))

    old = {"input": 1.0, "output": 4.0}
    cfg = load(tmp, price(example(), price_chf_per_m=old))
    entry = cfg.models["openai:gpt-5-mini"]
    check("catalogue: old price_chf_per_m is read as price_per_m", entry.get("price_per_m") == old and "price_chf_per_m" not in entry)
    state.CFG = cfg
    cost = cloud.request_cost("openai:gpt-5-mini", 1_000_000, 500_000)
    check("request cost uses the price under the neutral key", abs(cost - 3.0) < 1e-9, str(cost))
    cfg = load(tmp, price(example(), price_per_m=old, price_chf_per_m={"input": 2.0, "output": 4.0}))
    check("catalogue: both price names with different values are refused", failed(cfg) and "price_chf_per_m" in cfg.message
          and "models.openai:gpt-5-mini" in cfg.message, getattr(cfg, "message", ""))
    cfg = load(tmp, price(example(), price_per_m=old, price_chf_per_m=dict(old)))
    check("catalogue: both price names with the same value are accepted", not failed(cfg))

    # direct callers of parse() (tools, tests) may still pass the old names
    probe = config.Config.__new__(config.Config)
    probe._locate(write(tmp, example()), False)
    tree = provider(example(), budget_month_chf=3)
    check("Config.parse accepts the old name without merged()", probe.parse(tree) is None
          and probe.cloud["providers"]["openai"]["budget_month"] == 3)


def layers(tmp):
    # config.yaml with the old name, roles.yaml settings with the neutral path: the later layer wins
    cfg = load(tmp, provider(example(), budget_month_chf=10), {"settings": {"router.cloud.providers.openai.budget_month": 25}})
    check("roles.yaml neutral setting overrides the old name in config.yaml", not failed(cfg)
          and cfg.cloud["providers"]["openai"]["budget_month"] == 25, getattr(cfg, "message", ""))
    # roles.yaml written by an earlier router: the old settings path keeps working
    cfg = load(tmp, provider(example(), budget_month=10), {"settings": {"router.cloud.providers.openai.budget_month_chf": 30}})
    check("roles.yaml from an earlier router: old settings path still applies", not failed(cfg)
          and cfg.cloud["providers"]["openai"]["budget_month"] == 30, getattr(cfg, "message", ""))
    cfg = load(tmp, provider(example(), budget_month=10), {"settings": {"router.cloud.providers.openai.budget_month_chf": 30,
                                                                        "router.cloud.providers.openai.budget_month": 31}})
    check("roles.yaml settings with both paths and different values are refused", failed(cfg) and "budget_month_chf" in cfg.message,
          getattr(cfg, "message", ""))
    # catalogue entries stored by the UI with the old name, config.yaml with the new one: no conflict, roles.yaml wins
    cfg = load(tmp, price(example(), price_per_m={"input": 1, "output": 2}),
               {"models": {"openai:gpt-5-mini": {"cloud": "openai", "price_chf_per_m": {"input": 5, "output": 6}}}})
    entry = {} if failed(cfg) else cfg.models["openai:gpt-5-mini"]
    check("roles.yaml catalogue entry with the old name overrides config.yaml's neutral price", entry.get("price_per_m") == {"input": 5, "output": 6}
          and "price_chf_per_m" not in entry, getattr(cfg, "message", str(entry)))
    # settings view: base value and override flag under the renamed path
    cfg = load(tmp, provider(example(), budget_month_chf=12), {"settings": {"router.cloud.providers.openai.budget_month_chf": 40}})
    state.CFG = cfg
    items = {i["path"]: i for i in config.settings_view("en")["items"]}
    item = items.get("router.cloud.providers.openai.budget_month") or {}
    check("settings view: renamed path with effective value, config.yaml base value and override flag",
          item.get("value") == 40 and item.get("base") == 12 and item.get("overridden") is True
          and "router.cloud.providers.openai.budget_month_chf" not in items, str(item))
    check("settings view: currency setting is listed with default CHF", (items.get("router.cloud.currency") or {}).get("default") == "CHF")


def settings_paths():
    norm = cost_keys.normalize_settings({"router.cloud.providers.p.budget_month_chf": None, "modes.score.warm": 3})
    check("old settings path with null (remove override) keeps its meaning under the new path",
          norm == {"router.cloud.providers.p.budget_month": None, "modes.score.warm": 3}, str(norm))
    norm = cost_keys.normalize_settings({"router.cloud.providers.p.budget_month_chf": 5, "router.cloud.providers.p.budget_month": 5})
    check("old and new settings path with the same value merge", norm == {"router.cloud.providers.p.budget_month": 5}, str(norm))
    check("other paths are unchanged", cost_keys.neutral_path("router.cloud.currency") == "router.cloud.currency"
          and cost_keys.neutral_path("router.client_auth.clients.budget_month_chf.note") == "router.client_auth.clients.budget_month_chf.note")
    check("the old settings path is no longer an EDITABLE key, the new one is", "router.cloud.providers.*.budget_month" in settings.EDITABLE
          and "router.cloud.providers.*.budget_month_chf" not in settings.EDITABLE)
    legacy = cost_keys.with_legacy_price({"cloud": "x", "price_per_m": {"input": 1}})
    check("views keep the old price field name next to the neutral one", legacy["price_chf_per_m"] == legacy["price_per_m"] == {"input": 1})


def admin_paths(tmp):
    cfg = load(tmp, example())
    state.CFG = cfg
    ov = admin._overrides_from_request({"settings": {"router.cloud.providers.openai.budget_month_chf": 50}})
    check("PUT /admin/config: old settings path lands under the neutral path",
          not failed(ov) and ov["settings"] == {"router.cloud.providers.openai.budget_month": 50}, str(ov))
    bad = admin._overrides_from_request({"settings": {"router.cloud.providers.openai.budget_month_chf": 50,
                                                      "router.cloud.providers.openai.budget_month": 60}})
    check("PUT /admin/config: both settings paths with different values -> 400", failed(bad) and bad.status == 400)
    current = {"settings": {"router.cloud.providers.openai.budget_month_chf": 20, "modes.score.warm": 1}}
    merged = admin._merge_overrides(copy.deepcopy(current), {"settings": {"router.cloud.providers.openai.budget_month": 60}})
    check("roles.yaml from an earlier router + a new value: only the neutral path remains, with the new value",
          merged["settings"] == {"router.cloud.providers.openai.budget_month": 60, "modes.score.warm": 1}, str(merged))
    merged = admin._merge_overrides(copy.deepcopy(current), {"settings": {"router.cloud.providers.openai.budget_month": None}})
    check("reset via the neutral path also removes a value stored under the old path",
          merged["settings"] == {"modes.score.warm": 1}, str(merged))
    entry = admin._cloud_model_entry("openai:example", {"cloud": "openai", "price_chf_per_m": {"input": 2, "output": 8}})
    check("PUT /admin/config: cloud model with the old price name is stored under price_per_m",
          entry.get("price_per_m") == {"input": 2.0, "output": 8.0} and "price_chf_per_m" not in entry, str(entry))
    entry = admin._cloud_model_entry("openai:example", {"cloud": "openai", "price_per_m": {"input": 2, "output": 8},
                                                        "price_chf_per_m": {"input": 3, "output": 8}})
    check("PUT /admin/config: cloud model with both price names and different values -> 400", failed(entry) and entry.status == 400)


def currency(tmp):
    cfg = load(tmp, example())
    state.CFG = cfg
    check("currency: example config and default are CHF", cfg.cloud["currency"] == "CHF" and cloud.currency() == "CHF")
    raw = example()
    raw["router"]["cloud"].pop("currency", None)
    check("currency: absent key = CHF (previous behaviour)", load(tmp, raw).cloud["currency"] == "CHF")
    raw["router"]["cloud"]["currency"] = "EUR"
    cfg = load(tmp, raw)
    state.CFG = cfg
    cloud.setup()
    snap = state.CLOUD["openai"].snapshot()
    check("currency: EUR in the provider snapshot, budget fields keep their names", snap["currency"] == "EUR"
          and "budget_month_chf" in snap and "spend_month_chf" in snap and cloud.currency() == "EUR", str(snap))
    for bad in ("eur", "EURO", 5, ""):
        raw["router"]["cloud"]["currency"] = bad
        cfg = load(tmp, raw)
        check(f"currency: {bad!r} is refused", failed(cfg) and "currency" in cfg.message)
    cfg = load(tmp, example(), {"settings": {"router.cloud.currency": "USD"}})
    check("currency: settable through the settings block (web UI)", not failed(cfg) and cfg.cloud["currency"] == "USD")
    cfg = load(tmp, example(), {"settings": {"router.cloud.currency": "dollars"}})
    check("currency: settings block with a non-code value is refused", failed(cfg))


class FakeNode:
    name, gpu, inflight = "node-a", "synthetic gpu", 0

    def loaded_context(self, model):
        return 8192


async def bench(tmp):
    raw = example()
    cfg = load(tmp, raw)
    check("benchmark prompt: default unchanged (the prompt of earlier measurements)",
          cfg.bench_prompt == config.BENCH_PROMPT_DEFAULT and "Antworte auf Deutsch" in cfg.bench_prompt)
    raw["router"]["benchmark"] = {"prompt": "Explain in about 200 words what a network switch does."}
    cfg = load(tmp, raw)
    state.CFG = cfg
    check("benchmark prompt: configurable", cfg.bench_prompt.startswith("Explain") and perf.bench_prompt() == cfg.bench_prompt)
    sent = []

    async def fake_ollama(node, method, path, body=None, timeout=60, answer=True):
        sent.append(body)
        return {"eval_count": 200, "eval_duration": 4_000_000_000, "prompt_eval_count": 30, "prompt_eval_duration": 50_000_000,
                "load_duration": 1_000_000}

    async def no_release(node, model, why):
        return None

    orig = (perf._ollama, perf._release_node, perf.perf_save)
    perf._ollama, perf._release_node, perf.perf_save = fake_ollama, no_release, lambda: None
    try:
        await perf.bench_model("example:7b", FakeNode())
    finally:
        perf._ollama, perf._release_node, perf.perf_save = orig
    result = (state.PERF.get("example:7b@node-a") or {}).get("bench") or {}
    check("benchmark sends the configured prompt in every run", len(sent) == 3
          and all(b["messages"][0]["content"] == cfg.bench_prompt for b in sent), str(len(sent)))
    check("benchmark result records which prompt it used", result.get("prompt_id") == perf.prompt_id(cfg.bench_prompt)
          and result.get("prompt_id") != perf.prompt_id(config.BENCH_PROMPT_DEFAULT) and result.get("gen_tps") == 50.0, str(result))
    for bad in ("", "   ", 42, "x" * (config.BENCH_PROMPT_MAX_CHARS + 1)):
        raw["router"]["benchmark"] = {"prompt": bad}
        cfg = load(tmp, raw)
        check(f"benchmark prompt: {str(bad)[:12]!r} is refused", failed(cfg) and "benchmark.prompt" in cfg.message)
    raw["router"]["benchmark"] = {"prompt": None}
    check("benchmark prompt: null = default", load(tmp, raw).bench_prompt == config.BENCH_PROMPT_DEFAULT)


class FakeDecision:
    def __init__(self, selected):
        self.selected = selected

    async def classify_body(self, body, path):
        return types.SimpleNamespace(selected=self.selected, engine="rules", uncertainty={"top": 0.9, "margin": 0.8},
                                     latency_ms=1.0)


async def observed(selected):
    """Run fallback_observation._observe with a fake engine and a free alternative; the would_fallback it records."""
    events = []
    orig = (fallback_observation.decision.active, fallback_observation.alternative, state.remember)
    fallback_observation.decision.active = lambda: FakeDecision(selected)
    fallback_observation.alternative = lambda tiers, model, req: {"tier": 1, "model": "small", "node": "node-a", "warm": True}
    state.remember = events.append
    try:
        req = types.SimpleNamespace(request_id="r1", priority="normal")
        await fallback_observation._observe({}, "/api/chat", {"name": "standard"}, [], req, FakeNode(), "big", "busy", "c1")
    finally:
        fallback_observation.decision.active, fallback_observation.alternative, state.remember = orig
    return events[-1]["would_fallback"] if events else None


async def observe_light(tmp):
    raw = example()
    cfg = load(tmp, raw)
    check("observe_light absent: default [standard, assist] (previous behaviour)", cfg.decision["observe_light"] == ["standard", "assist"])
    check("fallback_observation has no hard-coded class list any more", not hasattr(fallback_observation, "LIGHT_DEFAULT"))
    state.CFG = cfg
    check("observation: class in the default list -> would_fallback", await observed("standard") is True)
    check("observation: class outside the list -> no fallback", await observed("code") is False)
    raw["decision_engine"]["observe_light"] = ["code"]
    state.CFG = load(tmp, raw)
    check("observe_light: an explicit list is taken as given", state.CFG.decision["observe_light"] == ["code"])
    check("observation: configured class -> would_fallback, default class not", await observed("code") is True
          and await observed("standard") is False)
    raw["decision_engine"]["observe_light"] = []
    state.CFG = load(tmp, raw)
    check("observe_light: [] means none", state.CFG.decision["observe_light"] == [] and await observed("standard") is False)
    raw["decision_engine"]["observe_light"] = ["nonexistent"]
    check("observe_light: a name outside options is refused", failed(load(tmp, raw)))
    raw["decision_engine"]["observe_light"] = "standard"
    check("observe_light: a non-list is refused", failed(load(tmp, raw)))
    raw["decision_engine"].pop("observe_light")
    raw["decision_engine"]["options"] = ["gross", "code"]
    raw["decision_engine"]["default"] = "gross"
    raw["decision_engine"]["local_llm"]["descriptions"] = {"gross": "long analyses", "code": "programming"}
    cfg = load(tmp, raw)
    check("observe_light absent with other role names: only default names among the options (none here)",
          not failed(cfg) and cfg.decision["observe_light"] == [], getattr(cfg, "message", ""))


def english_messages():
    check("node name fallback is English", common.safe_node_name("!!!") == "node")
    for path in ("nodes.py", "tunnel.py", "agentupdate.py", "ollamaupdate.py", "admin.py"):
        with open(os.path.join(HERE, "..", "router", "skirnir_router", path), encoding="utf-8") as f:
            text = f.read()
        found = [w for w in ("kein Tunnel verbunden", "warte auf Heartbeat", "unlesbarer Antwortkopf", "unerwarteter Rahmen",
                             "laeuft mit", "nicht getauscht", "Einmalig sichtbar", "der Router selbst") if w in text]
        check(f"{path}: no German status or log text from the earlier list", not found, str(found))


async def main(tmp):
    cost_keys_in_config(tmp)
    layers(tmp)
    settings_paths()
    admin_paths(tmp)
    currency(tmp)
    await bench(tmp)
    await observe_light(tmp)
    english_messages()


with tempfile.TemporaryDirectory() as tmp_dir:
    asyncio.run(main(tmp_dir))
print(f"\n{'OK' if not FAILS else 'FAILED'}: {len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
