import json, time, urllib.request, statistics
body = json.dumps({"context": "Schalte das Licht im Wohnzimmer an und dimm es auf 40 Prozent", "options": ["standard", "gross", "assist", "code"]}).encode()
lat = []
for _i in range(60):
    t0 = time.perf_counter()
    r = urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8082/decide", data=body, headers={"Content-Type": "application/json"}), timeout=10)
    d = json.loads(r.read()); lat.append((time.perf_counter() - t0) * 1000)
lat.sort()
print(json.dumps({"n": len(lat), "p50_ms": round(lat[len(lat)//2], 1), "p95_ms": round(lat[int(len(lat)*0.95)], 1), "mean_ms": round(statistics.fmean(lat), 1), "service_latency_ms_last": d["latency_ms"], "selected": max(d["probabilities"], key=d["probabilities"].get)}))
