#!/usr/bin/env python3
"""Self-test (Router 0.31.0, ADR-0094): a role's `system_append` ({model: text}).
- checked when the configuration loads: a mapping, every model in the catalogue, text only, at most 8000 characters,
  blank text drops the entry; an invalid override changes nothing;
- survives a role save that does not carry it (web UI, role tools), shown by GET /admin/config, taken by PUT;
- appended to the leading system message (or as a new one) only when exactly that model serves a role's chat request:
  every tier of that model gets it (context ladder, canary), other models, a request for a concrete model and
  /api/generate get nothing; the client's messages stay untouched for the next tier.
Trigger: a prompt rule one model needed made another model of the same role worse (it queried room by room until a
guard stopped it). Runs without router, network or Ollama."""
import copy
import os
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import proxy  # noqa: E402  (load order as in the router)
from skirnir_router import admin, config, ops, state  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402

EXAMPLE = os.path.join(HERE, "..", "router", "config.example.yaml")
FAILS = []
BIG, SMALL = "qwen3.6:35b-a3b", "granite4.2:8b"


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


# --- validation ---------------------------------------------------------------------------------------------------
CATALOG = {BIG: {}, SMALL: {}}
ok = config.role_system_append("assist", {BIG: "  Rule A.\n", SMALL: "  "}, CATALOG)
check("text is taken without surrounding blanks, blank text drops the entry", ok == {BIG: "Rule A."}, ok)
check("not set = empty", config.role_system_append("assist", None, CATALOG) == {})
check("exactly the limit is taken", config.role_system_append("assist", {BIG: "x" * config.SYSTEM_APPEND_MAX_CHARS}, CATALOG) != {})
for raw, why in (({BIG: "x" * (config.SYSTEM_APPEND_MAX_CHARS + 1)}, "longer than the limit"),
                 ({"unknown:7b": "Rule."}, "a model missing from the catalogue (typo)"),
                 ({BIG: ["Rule."]}, "a list is no text"),
                 ({BIG: 42}, "a number is no text"),
                 ("Rule.", "plain text instead of a mapping model -> text"),
                 ([BIG, "Rule."], "a list instead of a mapping")):
    p = config.role_system_append("assist", raw, CATALOG)
    check(f"refused: {why}", failed(p) and p.status == 400, getattr(p, "message", p))

# --- configuration: config.yaml base, override, admin ----------------------------------------------------------------
cfg = config.Config.load(EXAMPLE, use_overrides=False)
check("example configuration loads, no text by default", not failed(cfg) and cfg.roles["assist:latest"]["system_append"] == {},
      getattr(cfg, "message", ""))


def override(assist):
    """roles.yaml as the UI writes it: every role (it replaces the set), here only assist with something of its own."""
    return {"roles": {name: (assist if name == "assist" else {}) for name in config.load_yaml(EXAMPLE)["roles"]}}


p = cfg.reload(override({"system_append": {BIG: "Rule for the big model."}}))
check("override with system_append is taken", p is None and cfg.roles["assist:latest"]["system_append"] == {BIG: "Rule for the big model."},
      getattr(p, "message", cfg.roles["assist:latest"]["system_append"]))
p = cfg.reload(override({"system_append": {BIG: 7}}))
check("rejected case: an invalid override is refused and nothing changes",
      failed(p) and cfg.roles["assist:latest"]["system_append"] == {BIG: "Rule for the big model."}, getattr(p, "message", p))

# A role saved without the key (web UI, role tools that write tiers only) keeps what config.yaml has.
with tempfile.TemporaryDirectory() as tmp:
    base = config.load_yaml(EXAMPLE)
    base["roles"]["assist"]["system_append"] = {SMALL: "Rule for the small model."}
    path = os.path.join(tmp, "config.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(base, f, allow_unicode=True, sort_keys=False)
    own = config.Config.load(path, use_overrides=False)
    check("system_append in config.yaml loads", not failed(own) and own.roles["assist:latest"]["system_append"] == {SMALL: "Rule for the small model."},
          getattr(own, "message", ""))
    tiers_only = {"tiers": [{"model": SMALL, "num_ctx": 16384, "busy_ok": True}], "latency_first": False, "priority": "interactive"}
    p = own.reload({"roles": {name: (tiers_only if name == "assist" else {}) for name in base["roles"]}})
    check("a role save without system_append keeps config.yaml's text (web UI, role tools)",
          p is None and own.roles["assist:latest"]["system_append"] == {SMALL: "Rule for the small model."},
          getattr(p, "message", own.roles["assist:latest"]))

state.CFG = cfg
view = config.roles_as_config()["assist"]
check("GET /admin/config shows the text per model", view.get("system_append") == {BIG: "Rule for the big model."}, view.get("system_append"))
saved = admin._roles_from({"assist": {"tiers": view["tiers"], "system_append": view["system_append"]}})
check("PUT /admin/config passes system_append on", not failed(saved) and saved["assist"]["system_append"] == {BIG: "Rule for the big model."}, saved)
saved = admin._roles_from({"assist": {"tiers": view["tiers"]}})
check("PUT /admin/config without the key leaves it out (config.yaml stays in force)", "system_append" not in saved["assist"], saved)


# --- applied per serving model ---------------------------------------------------------------------------------------
class FakeNode:
    state = "free"
    is_cloud = False

    def loaded_context(self, model):
        return None


ROLE = dict(cfg.roles["assist:latest"], system_append={BIG: "Rule for the big model."})
BIG_65K, BIG_32K = {"model": BIG, "num_ctx": 65536}, {"model": BIG, "num_ctx": 32768}
SMALL_TIER = {"model": SMALL, "num_ctx": 16384, "busy_ok": True}
client = {"model": "assist:latest", "options": {},
          "messages": [{"role": "system", "content": "House prompt.\n"}, {"role": "user", "content": "How warm is it?"}]}
kept = copy.deepcopy(client)
out, _ = proxy._backend_body(client, ROLE, BIG_65K, 8192, FakeNode())
check("the serving model's text is appended to the leading system message",
      out["messages"][0] == {"role": "system", "content": "House prompt.\n\nRule for the big model."}, out["messages"][0])
check("the other messages are passed on unchanged", out["messages"][1:] == kept["messages"][1:], out["messages"])
check("the client's own body stays untouched (the next tier starts from it)", client == kept, client)
out, _ = proxy._backend_body(client, ROLE, BIG_32K, 8192, FakeNode())
check("the same model on the next rung of the context ladder gets it too", out["messages"][0]["content"].endswith("Rule for the big model."),
      out["messages"][0])
out, _ = proxy._backend_body(client, ROLE, SMALL_TIER, 8192, FakeNode())
check("rejected case: another model of the role gets the house prompt only", out["messages"] == kept["messages"], out["messages"])

out, _ = proxy._backend_body({"model": "assist:latest", "messages": [{"role": "user", "content": "hi"}]}, ROLE, BIG_65K, 8192, FakeNode())
check("without a system message the text becomes a new leading one",
      out["messages"] == [{"role": "system", "content": "Rule for the big model."}, {"role": "user", "content": "hi"}], out["messages"])
parts = {"role": "system", "content": [{"type": "text", "text": "House prompt."}]}
out, _ = proxy._backend_body({"model": "assist:latest", "messages": [parts, {"role": "user", "content": "hi"}]}, ROLE, BIG_65K, 8192, FakeNode())
check("a system message with non-text content is left as it is, the text goes in front",
      out["messages"][0] == {"role": "system", "content": "Rule for the big model."} and out["messages"][1] == parts, out["messages"])

out, _ = proxy._backend_body({"model": BIG, "messages": [{"role": "user", "content": "hi"}]},
                             {"exposed": BIG, "options": {}, "system_append": {BIG: "Rule."}}, BIG_65K, 8192, FakeNode())
check("rejected case: a request for a concrete model gets no text", out["messages"] == [{"role": "user", "content": "hi"}], out["messages"])
out, _ = proxy._backend_body({"model": "assist:latest", "prompt": "hi", "system": "House prompt."}, ROLE, BIG_65K, 8192, FakeNode())
check("rejected case: /api/generate (no messages) is not changed", "messages" not in out and out["system"] == "House prompt.", out)

for model, expect in ((SMALL, None), (BIG, "Rule for the big model.")):
    tier = ops.canary_tier(dict(ROLE, canary={"model": model, "num_ctx": 16384, "percent": 100.0, "busy_ok": True}))
    out, _ = proxy._backend_body({"model": "assist:latest", "messages": [{"role": "user", "content": "hi"}]}, ROLE, tier, 8192, FakeNode())
    got = out["messages"][0]["content"] if out["messages"][0]["role"] == "system" else None
    check(f"a canary tier with {model} gets {'its model text' if expect else 'nothing'}", got == expect, out["messages"])

print(f"\n{'ALL GREEN' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
sys.exit(1 if FAILS else 0)
