"""Bounded provenance projection; never cut a serialized source list mid-value."""
import json

from .outcome import failed
from .session_api import parse_json_value


def recall_metadata(raw):
    clean = {k: raw[k][:256] for k in ("kind", "confirmed", "origin_client", "observed_at", "version", "supersedes")
             if isinstance(raw.get(k), str)}
    refs = raw.get("source_refs")
    if isinstance(refs, str):
        refs = parse_json_value(refs)
        refs = None if failed(refs) else refs
    if isinstance(refs, list) and len(refs) <= 16 and all(isinstance(v, str) and len(v) <= 256 for v in refs):
        # Keep the Hindsight string-metadata contract, but only whole references.
        clean["source_refs"] = json.dumps(refs, ensure_ascii=False, separators=(",", ":"))
    return clean
