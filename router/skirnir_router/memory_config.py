"""Strict opt-in memory policy; environment names only, never connection secrets.

Parsers return the value or a Problem (outcome.py)."""
import math
import re
from dataclasses import dataclass

from .outcome import failed, invalid


@dataclass(frozen=True)
class HindsightConfig:
    enabled: bool = False
    endpoint_env: str = "SKIRNIR_HINDSIGHT_URL"
    api_key_env: str = "SKIRNIR_HINDSIGHT_TOKEN"
    namespace_key_env: str = "SKIRNIR_MEMORY_NAMESPACE_KEY"
    timeout_s: float = 3.0
    retries: int = 1
    fail_open: bool = True
    allow_http_loopback: bool = False
    recall_max_tokens: int = 512      # token budget sent to Hindsight for its own selection (advisory upstream)
    recall_max_bytes: int = 4096      # UTF-8 byte cap of the rendered recall wrapper (notice + facts + provenance)
    recall_max_facts: int = 10        # facts passed on per recall (after duplicates are dropped); the byte cap still applies
    recall_min_score: float = 0.0
    max_query_bytes: int = 512
    max_response_bytes: int = 65536
    max_inflight: int = 4
    retention_enabled: bool = False
    queue_size: int = 32
    max_content_bytes: int = 4096
    deny_terms: tuple = ()
    outbox_enabled: bool = False
    outbox_attempts: int = 5
    outbox_backoff_s: int = 2
    outbox_lease_s: int = 120
    outbox_capacity: int = 1024
    outbox_workers: int = 1
    outbox_client_capacity: int = 0
    outbox_retention_days: int = 0
    gateway_enabled: bool = False
    # Verified delivery: after Hindsight acknowledged a retain, the outbox checks that extraction really completed and
    # resubmits a failed one later (e.g. the local model was busy for hours), under a new upstream operation id.
    verify_delay_s: int = 1
    verify_max_delay_s: int = 30
    verify_timeout_s: int = 3600
    upstream_attempts: int = 12
    upstream_backoff_s: int = 300
    # Synchronous gateway retain (async=false): wait for verified completion, bounded.
    sync_wait_s: int = 20
    sync_max_waiting: int = 8


def parse_hindsight(raw):
    raw = {} if raw is None else raw
    default = HindsightConfig()
    if not isinstance(raw, dict) or set(raw) - set(default.__dataclass_fields__):
        return invalid("invalid router.hindsight fields")
    values = {k: raw.get(k, getattr(default, k)) for k in default.__dataclass_fields__}
    for k in ("enabled", "fail_open", "allow_http_loopback", "retention_enabled", "outbox_enabled", "gateway_enabled"):
        if type(values[k]) is not bool:
            return invalid(f"hindsight.{k} must be boolean")
    for k in ("endpoint_env", "api_key_env", "namespace_key_env"):
        if not isinstance(values[k], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", values[k]):
            return invalid(f"hindsight.{k} must name an environment variable")
    ranges = {"outbox_workers": (1, 16), "outbox_client_capacity": (0, 100000), "outbox_attempts": (1, 20), "outbox_backoff_s": (1, 3600), "outbox_lease_s": (90, 3600),
              "outbox_capacity": (1, 100000), "outbox_retention_days": (0, 36500), "retries": (0, 2), "recall_max_tokens": (256, 4096), "recall_max_bytes": (1024, 65536), "recall_max_facts": (1, 100), "max_query_bytes": (64, 1024),
              "max_response_bytes": (1024, 262144), "max_inflight": (1, 16), "queue_size": (1, 256), "max_content_bytes": (256, 16384),
              "verify_delay_s": (1, 60), "verify_max_delay_s": (1, 3600), "verify_timeout_s": (60, 86400), "upstream_attempts": (1, 100),
              "upstream_backoff_s": (10, 86400), "sync_wait_s": (1, 110), "sync_max_waiting": (1, 256)}
    for k, (low, high) in ranges.items():
        if type(values[k]) is not int or not low <= values[k] <= high:
            return invalid(f"hindsight.{k} must be an integer between {low} and {high}")
    score = values["recall_min_score"]
    if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
        return invalid("recall_min_score must be finite, between 0 and 1")
    timeout = values["timeout_s"]
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0.1 <= timeout <= 30:
        return invalid("hindsight.timeout_s must be finite, between 0.1 and 30")
    terms = values["deny_terms"]
    if not isinstance(terms, (tuple, list)) or len(terms) > 32 or any(not isinstance(x, str) or not x or len(x) > 128 for x in terms):
        return invalid("hindsight.deny_terms must contain at most 32 nonempty strings (max 128 characters)")
    values["deny_terms"] = tuple(terms)
    return HindsightConfig(**values)


def parse_client_memory(raw, classes):
    raw = {} if raw is None else raw
    if not isinstance(raw, dict) or set(raw) - {"owner", "projects", "strategy", "default_project", "auto_retain"}:
        return invalid("invalid client memory fields")
    owner, projects = raw.get("owner", "client"), raw.get("projects", {})
    if owner not in ("client", "skirnir") or not isinstance(projects, dict):
        return invalid("client memory.owner must be client or skirnir; projects must be a mapping")
    clean = {}
    for project, policy in projects.items():
        if not isinstance(project, str) or not project.strip() or len(project) > 128 or any(ord(c) < 32 for c in project):
            return invalid("invalid memory project name")
        if not isinstance(policy, dict) or set(policy) - {"read", "write", "delete", "data_class", "allow_cloud", "scope"}:
            return invalid("invalid memory project policy")
        if policy.get("data_class") not in classes:
            return invalid("each memory project needs an explicit configured data_class")
        clean[project] = {"data_class": policy["data_class"]}
        for k in ("read", "write", "allow_cloud", "delete"):
            if type(policy.get(k, False)) is not bool:
                return invalid(f"memory project {k} must be boolean")
            clean[project][k] = policy.get(k, False)
        from .shared_config import name
        scope = name(policy["scope"]) if "scope" in policy else ""
        if failed(scope):
            return scope
        clean[project]["scope"] = scope
    strategy = raw.get("strategy", "legacy")
    if strategy not in ("legacy", "off", "manual", "auto", "hybrid"):
        return invalid("invalid memory strategy")
    default = raw.get("default_project")
    if default is not None and default not in clean:
        return invalid("memory default_project must be a granted project")
    if strategy in ("auto", "hybrid") and (owner != "skirnir" or default is None):
        return invalid("automatic strategies require router ownership and default_project")
    if type(raw.get("auto_retain", False)) is not bool or (raw.get("auto_retain") and strategy != "auto"):
        return invalid("auto_retain requires auto strategy and a boolean")
    return {"owner": owner, "projects": clean, "strategy": strategy, "default_project": default, "auto_retain": raw.get("auto_retain", False)}


def parse_memory_request(raw):
    if raw is None or raw is False:
        return None
    if not isinstance(raw, dict) or set(raw) - {"project", "recall", "retain", "event_id"}:
        return invalid("routing.memory has invalid fields")
    project = raw.get("project")
    if not isinstance(project, str) or not project.strip() or len(project) > 128 or any(ord(c) < 32 for c in project):
        return invalid("routing.memory.project is required (max 128 characters)")
    result = {"project": project, "recall": raw.get("recall", True), "retain": raw.get("retain", False)}
    for k in ("recall", "retain"):
        if type(result[k]) is not bool:
            return invalid(f"routing.memory.{k} must be boolean")
    eid = raw.get("event_id")
    if eid is not None and (not isinstance(eid, str) or not eid.strip() or len(eid) > 128 or any(ord(c) < 32 for c in eid)):
        return invalid("memory.event_id must be 1-128 characters")
    if result["retain"] and not eid:
        return invalid("routing.memory.event_id required for retention")
    result["event_id"] = eid
    return result
