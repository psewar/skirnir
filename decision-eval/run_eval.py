#!/usr/bin/env python3
"""Decision-Engines gegen den Testdatensatz messen (Abschnitt 10): Rules vs. Jevlike Byte vs. Jevlike+Encoder vs. lokales LLM.

Quellen:
  * Jevlike-Varianten: direkt am Dienst (server.py) oder ein Checkpoint, den dieses Skript selbst als Dienst startet
    (--jevlike NAME=PFAD.pt, mehrfach; braucht die venv aus decision-jevlike).
  * rules / local_llm: ueber den Router, POST /admin/decide {"engine": ...} (Dev-Umgebung http://127.0.0.1:21435 oder
    Produktion mit Basic Auth per --router-auth user:pass; ohne Angabe werden diese Engines ausgelassen).

Kennzahlen je Engine: Top-1, Top-3, Konfusionsmatrix, Latenz p50/p95/p99, Fallback-Rate unter der Policy, ECE (15 Bins),
Temperatur-Kalibrierung auf dem Validierungssatz (ECE vorher/nachher) -> calibration.json fuer den Router. Ausserdem die
"Routing Accuracy" der Kette (erste sichere Engine gewinnt, sonst Fallback) fuer die konfigurierten Ketten.

    ..\\decision-jevlike\\.venv\\Scripts\\python run_eval.py --jevlike tiny=..\\decision-jevlike\\models\\skirnir-router-tiny.pt --router http://127.0.0.1:21435
"""

from __future__ import annotations

import argparse
import base64
import collections
import json
import math
import os
import statistics
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
POLICY = {"min_top_probability": 0.5, "min_margin": 0.2, "max_entropy_ratio": 0.75}


def load(name):
    return [json.loads(l) for l in open(os.path.join(DATA, f"{name}.jsonl"), encoding="utf-8") if l.strip()]


def http(url, body=None, headers=None, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})}, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# --- Engines als Aufrufer ---------------------------------------------------------------------------------------------

class JevlikeService:
    """Startet server.py fuer einen Checkpoint auf einem freien Port (oder nutzt --endpoint)."""

    def __init__(self, name, checkpoint=None, endpoint=None, port=18100, device="auto"):
        self.name, self.proc, self.endpoint = name, None, endpoint
        if checkpoint:
            server = os.path.join(HERE, "..", "decision-jevlike", "server.py")
            self.proc = subprocess.Popen([sys.executable, server, "--checkpoint", checkpoint, "--model-name", name, "--port", str(port), "--device", device],
                                         stdout=subprocess.DEVNULL, stderr=open(os.path.join(HERE, f"eval-{name}.log"), "w"))
            self.endpoint = f"http://127.0.0.1:{port}"
            for _ in range(120):
                try:
                    http(self.endpoint + "/health", timeout=3)
                    break
                except Exception:  # noqa: BLE001
                    time.sleep(0.5)
            else:
                raise SystemExit(f"{name}: Dienst startet nicht (siehe eval-{name}.log)")

    def decide(self, context, options):
        r = http(self.endpoint + "/decide", {"context": context, "options": options})
        return r["probabilities"]

    def close(self):
        if self.proc:
            self.proc.terminate()


class RouterEngine:
    def __init__(self, name, router, auth=None):
        self.name, self.router = name, router.rstrip("/")
        self.headers = {"Authorization": "Basic " + base64.b64encode(auth.encode()).decode()} if auth else {}

    def decide(self, context, options):
        r = http(self.router + "/admin/decide", {"context": context, "options": options, "engine": self.name}, self.headers, timeout=180)
        return r["probabilities"]


# --- Kennzahlen -------------------------------------------------------------------------------------------------------

def uncertainty(probs):
    ranked = sorted(probs.values(), reverse=True)
    top, second = ranked[0], (ranked[1] if len(ranked) > 1 else 0.0)
    ent = -sum(p * math.log(p) for p in ranked if p > 0)
    return {"top": top, "margin": top - second, "entropy_ratio": ent / math.log(len(ranked)) if len(ranked) > 1 else 0.0}


def uncertain(unc, policy=POLICY):
    return unc["top"] < policy["min_top_probability"] or unc["margin"] < policy["min_margin"] or unc["entropy_ratio"] > policy["max_entropy_ratio"]


def ece(rows, bins=15):
    """Expected Calibration Error ueber die Konfidenz der Top-1-Wahl."""
    buckets = collections.defaultdict(list)
    for r in rows:
        buckets[min(bins - 1, int(r["top"] * bins))].append(r)
    total = len(rows)
    return sum(len(b) / total * abs(sum(x["correct"] for x in b) / len(b) - sum(x["top"] for x in b) / len(b)) for b in buckets.values()) if total else 0.0


def apply_temperature(probs, t):
    scaled = {k: v ** (1 / t) if v > 0 else 0.0 for k, v in probs.items()}
    s = sum(scaled.values())
    return {k: v / s for k, v in scaled.items()} if s else probs


def fit_temperature(results):
    """Temperatur mit minimaler negativer Log-Likelihood auf dem Validierungssatz (Rastersuche 0,5 .. 5)."""
    best = (None, float("inf"))
    for t in [x / 20 for x in range(10, 101)]:
        nll = 0.0
        for r in results:
            p = apply_temperature(r["probs"], t).get(r["truth"], 1e-9)
            nll -= math.log(max(p, 1e-9))
        if nll < best[1]:
            best = (t, nll)
    return best[0]


def percentile(values, q):
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


def evaluate(engine, rows):
    out = []
    for r in rows:
        t0 = time.perf_counter()
        try:
            probs = engine.decide(r["context"], r["options"])
            lat = (time.perf_counter() - t0) * 1000
        except Exception as e:  # noqa: BLE001
            out.append({"truth": r["role"], "probs": {}, "top": 0.0, "correct": 0, "top3": 0, "latency_ms": (time.perf_counter() - t0) * 1000, "error": str(e)[:80]})
            continue
        ranked = sorted(probs.items(), key=lambda kv: -kv[1])
        sel = ranked[0][0]
        out.append({"truth": r["role"], "probs": probs, "selected": sel, "top": ranked[0][1], "correct": int(sel == r["role"]),
                    "top3": int(r["role"] in [k for k, _ in ranked[:3]]), "latency_ms": lat, "unc": uncertainty(probs)})
    return out


def summarize(name, results, options, temperature=None):
    ok = [r for r in results if r.get("probs")]
    n = len(results)
    conf = {a: {b: 0 for b in options} for a in options}
    for r in ok:
        conf[r["truth"]][r["selected"]] += 1
    lats = [r["latency_ms"] for r in results]
    fb = sum(1 for r in ok if uncertain(r["unc"]))
    summary = {"engine": name, "n": n, "errors": n - len(ok), "top1": round(sum(r["correct"] for r in ok) / n, 4), "top3": round(sum(r["top3"] for r in ok) / n, 4),
               "latency_ms": {"p50": round(percentile(lats, 0.5), 2), "p95": round(percentile(lats, 0.95), 2), "p99": round(percentile(lats, 0.99), 2), "mean": round(statistics.fmean(lats), 2)},
               "fallback_rate": round(fb / n, 4), "accuracy_when_confident": round(sum(r["correct"] for r in ok if not uncertain(r["unc"])) / max(1, len(ok) - fb), 4),
               "ece": round(ece(ok), 4), "confusion": conf}
    if temperature:
        cal = [{**r, "top": max(apply_temperature(r["probs"], temperature).values())} for r in ok]
        summary["temperature"] = temperature
        summary["ece_calibrated"] = round(ece(cal), 4)
    return summary


def chain_accuracy(order, per_engine, default):
    """Routing Accuracy einer Kette: je Beispiel die erste sichere Engine, sonst die letzte, sonst default."""
    n = len(next(iter(per_engine.values())))
    correct = fallbacks = 0
    for i in range(n):
        chosen, forced = None, True
        for name in order:
            r = per_engine[name][i]
            if not r.get("probs"):
                continue
            chosen = r["selected"]
            if not uncertain(r["unc"]):
                forced = False
                break
        if forced:
            fallbacks += 1
        truth = per_engine[order[0]][i]["truth"]
        correct += int((chosen or default) == truth)
    return {"chain": order, "routing_accuracy": round(correct / n, 4), "fallback_rate": round(fallbacks / n, 4)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jevlike", action="append", default=[], help="NAME=checkpoint.pt oder NAME=http://host:port")
    ap.add_argument("--router", default=None, help="Router-Control-URL fuer rules/local_llm ueber /admin/decide")
    ap.add_argument("--router-auth", default=None, help="user:pass (Basic) fuer den Router")
    ap.add_argument("--router-auth-ops", action="store_true", help="Basic-Auth UI_USER:<UI_PASS_ENV> aus dem Secret-Store, Parameter aus skirnir-ops/deploy.env (deploy/ops_env.py)")
    ap.add_argument("--router-engines", default="rules,local_llm")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--out", default=os.path.join(HERE, "results"))
    ap.add_argument("--chains", default=None, help="Ketten fuer die Routing Accuracy, z. B. 'embed,local_llm,rules;tfidf,local_llm,rules'")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    if a.router_auth_ops:
        sys.path.insert(0, os.path.join(HERE, "..", "deploy"))
        import ops_env   # noqa: E402
        ops = ops_env.load()
        ops_env.load_secrets(ops)
        a.router_auth = ops["UI_USER"] + ":" + os.environ[ops["UI_PASS_ENV"]]
    test, val = load("test"), load("validation")
    options = test[0]["options"]
    engines, services = [], []
    for i, spec in enumerate(a.jevlike):
        name, target = spec.split("=", 1)
        svc = JevlikeService(name, endpoint=target) if target.startswith("http") else JevlikeService(name, checkpoint=target, port=18100 + i, device=a.device)
        services.append(svc)
        engines.append(svc)
    if a.router:
        for e in a.router_engines.split(","):
            engines.append(RouterEngine(e.strip(), a.router, a.router_auth))
    summaries, per_engine, calibration = [], {}, {}
    try:
        for e in engines:
            print(f"== {e.name}: Validierung ({len(val)}) fuer die Kalibrierung ...", flush=True)
            val_res = evaluate(e, val)
            t = fit_temperature([r for r in val_res if r.get("probs")]) if any(r.get("probs") for r in val_res) else None
            print(f"== {e.name}: Test ({len(test)}) ...", flush=True)
            res = evaluate(e, test)
            per_engine[e.name] = res
            s = summarize(e.name, res, options, t)
            s["ece_validation"] = round(ece([r for r in val_res if r.get("probs")]), 4)
            summaries.append(s)
            if t:
                calibration[e.name if e.name in ("rules", "local_llm") else f"jevlike/{e.name}"] = {"temperature": t, "ece_before": s["ece"], "ece_after": s.get("ece_calibrated"),
                                                                                                        "measured_at": time.strftime("%Y-%m-%d"), "n_validation": len(val)}
            print(json.dumps({k: v for k, v in s.items() if k != "confusion"}, ensure_ascii=False), flush=True)
    finally:
        for svc in services:
            svc.close()
    chains = []
    names = [e.name for e in engines]
    orders = [c.split(",") for c in a.chains.split(";")] if a.chains else [[n for n in names if n != "rules"] + ["rules"] if "rules" in names else names]
    for order in orders:
        order = [n.strip() for n in order if n.strip() in per_engine]
        if len(order) > 1:
            chains.append(chain_accuracy(order, per_engine, options[0]))
    report = {"date": time.strftime("%Y-%m-%d %H:%M"), "test_n": len(test), "validation_n": len(val), "policy": POLICY, "engines": summaries, "chains": chains}
    json.dump(report, open(os.path.join(a.out, "report.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    json.dump(calibration, open(os.path.join(a.out, "calibration.json"), "w", encoding="utf-8"), indent=1)
    write_markdown(report, os.path.join(a.out, "report.md"), options)
    print("->", os.path.join(a.out, "report.md"))


def write_markdown(report, path, options):
    lines = [f"# Decision-Engine-Auswertung {report['date']}", "",
             f"Testsatz {report['test_n']} Beispiele, Validierung {report['validation_n']} (Kalibrierung). Policy: {report['policy']}.", "",
             "| Engine | Top-1 | Top-3 | sicher & richtig | Fallback-Rate | p50 ms | p95 ms | p99 ms | ECE | ECE kalibriert (T) | Fehler |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in report["engines"]:
        cal = f"{s.get('ece_calibrated', '-')} ({s.get('temperature', '-')})"
        lines.append(f"| {s['engine']} | {s['top1']:.3f} | {s['top3']:.3f} | {s['accuracy_when_confident']:.3f} | {s['fallback_rate']:.3f} | "
                     f"{s['latency_ms']['p50']} | {s['latency_ms']['p95']} | {s['latency_ms']['p99']} | {s['ece']:.3f} | {cal} | {s['errors']} |")
    for c in report["chains"]:
        lines += ["", f"Kette `{' -> '.join(c['chain'])}`: Routing Accuracy {c['routing_accuracy']:.3f}, Fallback-Rate {c['fallback_rate']:.3f}"]
    for s in report["engines"]:
        lines += ["", f"## Konfusionsmatrix {s['engine']} (Zeile = wahr, Spalte = gewaehlt)", "", "| | " + " | ".join(options) + " |", "|---|" + "---|" * len(options)]
        for a in options:
            lines.append(f"| **{a}** | " + " | ".join(str(s["confusion"][a][b]) for b in options) + " |")
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
