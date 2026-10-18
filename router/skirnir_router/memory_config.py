"""Strict opt-in memory policy; environment names only, never connection secrets."""
import math
import re
from dataclasses import dataclass


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
    recall_max_tokens: int = 512
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


def parse_hindsight(raw):
    raw = {} if raw is None else raw
    default = HindsightConfig()
    if not isinstance(raw, dict) or set(raw) - set(default.__dataclass_fields__):
        raise ValueError("invalid router.hindsight fields")
    values = {k: raw.get(k, getattr(default, k)) for k in default.__dataclass_fields__}
    for k in ("enabled", "fail_open", "allow_http_loopback", "retention_enabled", "outbox_enabled", "gateway_enabled"):
        if type(values[k]) is not bool:
            raise ValueError(f"hindsight.{k} must be boolean")
    for k in ("endpoint_env", "api_key_env", "namespace_key_env"):
        if not isinstance(values[k], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", values[k]):
            raise ValueError(f"hindsight.{k} must name an environment variable")
    ranges = {"outbox_workers": (1, 16), "outbox_client_capacity": (0, 100000), "outbox_attempts": (1, 20), "outbox_backoff_s": (1, 3600), "outbox_lease_s": (90, 3600),
              "outbox_capacity": (1, 100000), "outbox_retention_days": (0, 36500), "retries": (0, 2), "recall_max_tokens": (256, 4096), "max_query_bytes": (64, 1024),
              "max_response_bytes": (1024, 262144), "max_inflight": (1, 16), "queue_size": (1, 256), "max_content_bytes": (256, 16384)}
    for k, (low, high) in ranges.items():
        if type(values[k]) is not int or not low <= values[k] <= high:
            raise ValueError(f"hindsight.{k} must be an integer between {low} and {high}")
    score = values["recall_min_score"]
    if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError("recall_min_score must be finite, between 0 and 1")
    timeout = values["timeout_s"]
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0.1 <= timeout <= 30:
        raise ValueError("hindsight.timeout_s must be finite, between 0.1 and 30")
    terms = values["deny_terms"]
    if not isinstance(terms, (tuple, list)) or len(terms) > 32 or any(not isinstance(x, str) or not x or len(x) > 128 for x in terms):
        raise ValueError("hindsight.deny_terms must contain at most 32 nonempty strings (max 128 characters)")
    values["deny_terms"] = tuple(terms)
    return HindsightConfig(**values)


def parse_client_memory(raw, classes):
    raw = {} if raw is None else raw
    if not isinstance(raw, dict) or set(raw) - {"owner", "projects", "strategy", "default_project", "auto_retain"}:
        raise ValueError("invalid client memory fields")
    owner, projects = raw.get("owner", "client"), raw.get("projects", {})
    if owner not in ("client", "skirnir") or not isinstance(projects, dict):
        raise ValueError("client memory.owner must be client or skirnir; projects must be a mapping")
    clean = {}
    for project, policy in projects.items():
        if not isinstance(project, str) or not project.strip() or len(project) > 128 or any(ord(c) < 32 for c in project):
            raise ValueError("invalid memory project name")
        if not isinstance(policy, dict) or set(policy) - {"read", "write", "delete", "data_class", "allow_cloud", "scope"}:
            raise ValueError("invalid memory project policy")
        if policy.get("data_class") not in classes:
            raise ValueError("each memory project needs an explicit configured data_class")
        clean[project] = {"data_class": policy["data_class"]}
        for k in ("read", "write", "allow_cloud", "delete"):
            if type(policy.get(k, False)) is not bool:
                raise ValueError(f"memory project {k} must be boolean")
            clean[project][k] = policy.get(k, False)
        from .shared_config import name
        clean[project]["scope"] = name(policy["scope"]) if "scope" in policy else ""
    strategy = raw.get("strategy", "legacy")
    if strategy not in ("legacy", "off", "manual", "auto", "hybrid"):
        raise ValueError("invalid memory strategy")
    default = raw.get("default_project")
    if default is not None and default not in clean:
        raise ValueError("memory default_project must be a granted project")
    if strategy in ("auto", "hybrid") and (owner != "skirnir" or default is None):
        raise ValueError("automatic strategies require router ownership and default_project")
    if type(raw.get("auto_retain", False)) is not bool or (raw.get("auto_retain") and strategy != "auto"):
        raise ValueError("auto_retain requires auto strategy and a boolean")
    return {"owner": owner, "projects": clean, "strategy": strategy, "default_project": default, "auto_retain": raw.get("auto_retain", False)}


def parse_memory_request(raw):
    if raw is None or raw is False:
        return None
    if not isinstance(raw, dict) or set(raw) - {"project", "recall", "retain", "event_id"}:
        raise ValueError("routing.memory has invalid fields")
    project = raw.get("project")
    if not isinstance(project, str) or not project.strip() or len(project) > 128 or any(ord(c) < 32 for c in project):
        raise ValueError("routing.memory.project is required (max 128 characters)")
    result = {"project": project, "recall": raw.get("recall", True), "retain": raw.get("retain", False)}
    for k in ("recall", "retain"):
        if type(result[k]) is not bool:
            raise ValueError(f"routing.memory.{k} must be boolean")
    eid = raw.get("event_id")
    if eid is not None and (not isinstance(eid, str) or not eid.strip() or len(eid) > 128 or any(ord(c) < 32 for c in eid)):
        raise ValueError("memory.event_id must be 1-128 characters")
    if result["retain"] and not eid:
        raise ValueError("routing.memory.event_id required for retention")
    result["event_id"] = eid
    return result
