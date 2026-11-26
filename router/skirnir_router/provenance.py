"""Bounded provenance projection; never cut a serialized source list mid-value."""
import json

from . import session_api
from .outcome import failed, invalid


def make_provenance(raw, principal, *, confirmed):
    """Server-side provenance record, or a Problem."""
    if failed(problem := session_api.fields(raw, ("source_refs", "kind", "version", "supersedes"))):
        return problem
    refs = raw.get("source_refs", [])
    if not isinstance(refs, list) or len(refs) > 16 or any(not isinstance(x, str) or len(x) > 256 for x in refs):
        return invalid("invalid provenance references")
    kind = raw.get("kind", "confirmed_information" if confirmed else "assistant_derivation")
    if kind not in ("user_assertion", "confirmed_information", "assistant_derivation") or (not confirmed and kind == "confirmed_information"):
        return invalid("invalid knowledge kind")
    result = {"kind": kind, "origin_client": principal, "source_refs": json.dumps(refs), "confirmed": str(confirmed).lower()}
    # Wall-clock time is added on dispatch; it is not part of an event fingerprint.
    for key in ("version", "supersedes"):
        if key in raw:
            if failed(value := session_api.text(raw[key], key)):
                return value
            result[key] = value
    return result


def recall_metadata(raw):
    clean = {k: raw[k][:256] for k in ("kind", "confirmed", "origin_client", "observed_at", "version", "supersedes")
             if isinstance(raw.get(k), str)}
    refs = raw.get("source_refs")
    if isinstance(refs, str):
        refs = session_api.parse_json_value(refs)
        refs = None if failed(refs) else refs
    if isinstance(refs, list) and len(refs) <= 16 and all(isinstance(v, str) and len(v) <= 256 for v in refs):
        # Keep the Hindsight string-metadata contract, but only whole references.
        clean["source_refs"] = json.dumps(refs, ensure_ascii=False, separators=(",", ":"))
    return clean
