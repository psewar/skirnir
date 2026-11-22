"""Currency-neutral cost keys (router 0.37.0, ADR-0105).

The budget and price keys used to carry the currency in their name (`budget_month_chf`, `price_chf_per_m`), although the
router never converts: the amounts are in whatever currency the catalogue prices are written in. The neutral names are
`budget_month` (per cloud provider) and `price_per_m` (per catalogue model); `router.cloud.currency` names the unit for
display (default CHF). The old names stay accepted as aliases with the same meaning, in config.yaml, in the roles.yaml the
web UI writes (catalogue entries and the settings block) and in `PUT /admin/config`.

Rule when both names are given in one place: equal values are accepted (the alias is dropped), different values are a
configuration error that names both keys. Each layer (config.yaml, roles.yaml, a request body) is normalised on its own
before the layers are merged, so a neutral key in one layer and the alias in another never conflict: the later layer wins
as for every other key.

Wire fields, persisted usage data and Prometheus metric names keep their `_chf` names (ADR-0105).
"""

import re

from .outcome import invalid

PROVIDER_ALIASES = {"budget_month_chf": "budget_month"}   # router.cloud.providers.<p>.<legacy> -> neutral
MODEL_ALIASES = {"price_chf_per_m": "price_per_m"}         # models.<m>.<legacy> -> neutral
PROVIDER_PATH = re.compile(r"^router\.cloud\.providers\.[^.]+\.[^.]+$")
CURRENCY_DEFAULT = "CHF"
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")   # ISO 4217 alphabetic code; the router only displays it


def normalize_entry(entry, aliases, where):
    """Move legacy keys of one mapping (in place) to their neutral names. None = done, Problem 400 = both names given
    with different values. A non-mapping entry is left to the schema check."""
    if not isinstance(entry, dict):
        return None
    for old, new in aliases.items():
        if old not in entry:
            continue
        value = entry[old]
        if new in entry and entry[new] != value:
            return invalid(f"{where}: {old} is the old name of {new}; give only one of them (they differ: "
                           f"{new}={entry[new]!r}, {old}={value!r})")
        del entry[old]
        entry.setdefault(new, value)
    return None


def normalize_tree(c, where="configuration"):
    """config.yaml or roles.yaml tree (in place): provider budgets and catalogue prices under their neutral names.
    None = done, Problem 400 = a conflicting pair."""
    if not isinstance(c, dict):
        return None
    router = c.get("router")
    cloud = router.get("cloud") if isinstance(router, dict) else None
    providers = cloud.get("providers") if isinstance(cloud, dict) else None
    for name, spec in (providers.items() if isinstance(providers, dict) else ()):
        problem = normalize_entry(spec, PROVIDER_ALIASES, f"{where}: router.cloud.providers.{name}")
        if problem is not None:
            return problem
    models = c.get("models")
    for name, spec in (models.items() if isinstance(models, dict) else ()):
        problem = normalize_entry(spec, MODEL_ALIASES, f"{where}: models.{name}")
        if problem is not None:
            return problem
    return None


def neutral_path(path):
    """Settings path with the neutral key: router.cloud.providers.<p>.budget_month_chf -> ...budget_month."""
    path = str(path)
    if not PROVIDER_PATH.match(path):
        return path
    head, _, key = path.rpartition(".")
    return f"{head}.{PROVIDER_ALIASES.get(key, key)}"


def normalize_settings(settings, where="settings"):
    """Settings block {path: value} with neutral paths, as a new dict; a non-mapping is returned unchanged (the settings
    check refuses it). Problem 400 = a path and its alias with different values. A null value (remove the override)
    keeps its meaning under the neutral path."""
    if not isinstance(settings, dict):
        return settings
    out, legacy = {}, {}
    for path, value in settings.items():
        new = neutral_path(path)
        if new == str(path):
            out[new] = value
        else:
            legacy[new] = (str(path), value)
    for new, (old, value) in legacy.items():
        if new in out and out[new] != value:
            return invalid(f"{where}: {old} is the old name of {new}; give only one of them (they differ: "
                           f"{out[new]!r}, {value!r})")
        out.setdefault(new, value)
    return out


def with_legacy_price(entry):
    """Catalogue entry for a control-port view: the neutral `price_per_m` plus the old field name `price_chf_per_m`
    with the same value, so readers of the earlier field keep working (ADR-0105)."""
    if not isinstance(entry, dict) or "price_per_m" not in entry:
        return entry
    return {**entry, "price_chf_per_m": entry["price_per_m"]}
