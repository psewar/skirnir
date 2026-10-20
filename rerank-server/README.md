# rerank-server - Cross-Encoder-Reranker mit TEI-Protokoll auf OpenVINO

Kleiner Dienst fuer einen GPU-Knoten: ordnet Kandidatentexte zu einer Frage (Cross-Encoder) und spricht das
`/rerank`-Protokoll von Hugging Face TEI. Gedacht fuer einen Gedaechtnisdienst wie Hindsight, dessen Reranking sonst auf der
CPU des Router-Hosts laeuft. Der Skirnir-Router nimmt `POST /rerank` an und reicht es ueber den Agenten-Tunnel an diesen Dienst
weiter. Auf dem Knoten laeuft er als Kindprozess des Agenten.

- **OpenVINO statt CUDA**: Laeuft auf einer Intel-iGPU (`GPU.0`), der CPU oder einer NPU und belegt so kein VRAM der grossen GPU.
  Eine iGPU bleibt auch dann schnell, wenn die dedizierte GPU durch ein Spiel oder ein LLM voll ausgelastet ist.
- **Standardmodell** `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` (mehrsprachig, 118 M Parameter).
- **Nur Loopback.** Der Dienst kennt keine Anmeldung; der Agent reicht weiter, und der Router prueft die Berechtigung.
- **Prueft sich selbst, bevor er sich gesund meldet**:
  - SHA-256 jeder Laufzeitdatei gegen `manifest.json`
  - ein eindeutiges Fragepaar (relevant vor irrelevant, keine NaN)
  - Hintergrund: Eine beschaedigte Modelldatei mit richtiger Groesse lieferte einmal still Unsinn.

## Gemessen

Testlauf 2026-10-20: 108 Kandidaten je Frage, 26 Fragen, Core-Ultra-iGPU (Xe, 64 EUs), fp16, dynamische Laenge.

| Variante | Zeit je Anfrage | Qualitaet |
|---|---|---|
| dieser Dienst, iGPU | **Median 451 ms**, p95 468 ms | Rangkorrelation zu Torch fp32: 0,9999 |
| zum Vergleich: ms-marco-MiniLM-L-6-v2 auf einer 4-Kern-Server-CPU (Hindsights Standard) | 2,3-3,7 s | englisch |

## Dateien

| Datei | Zweck |
|---|---|
| `server.py` | HTTP-Dienst: `POST /rerank`, `GET /health` |
| `rerank_service.py` | Kern ohne OpenVINO: Pruefung der Anfrage, Zustand, Antwortformat, Manifest (in `test/selftest_rerank_server.py` getestet) |
| `engine_openvino.py` | Cross-Encoder auf OpenVINO, Tokenizer aus `tokenizer.json` |
| `export_model.py` | einmalig: Hugging-Face-Modell -> ONNX -> OpenVINO-IR (fp16) + `manifest.json`, mit Abgleich gegen Torch |
| `requirements.txt` | Laufzeit: `openvino`, `tokenizers`, `numpy` (kein torch) |

## Einrichten

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt               # Linux: .venv/bin/python
<venv mit torch+transformers>/python export_model.py onnx               # einmalig, mit Netz
.venv/Scripts/python export_model.py ir                                 # schreibt models/<name>/manifest.json
.venv/Scripts/python server.py --device GPU.0 --port 8090               # lokal pruefen: curl http://127.0.0.1:8090/health
```

`export_model.py ir` schreibt das Manifest nur, wenn zwei Abgleiche stimmen:
- die Bibliothek `tokenizers` liefert dieselben Token-IDs wie `transformers`
- das IR weicht hoechstens 0,05 Logit von Torch ab

Ohne Manifest startet der Dienst nicht (`/health` -> 503 `selftest_failed`, Grund `manifest_missing`).

## Protokoll

- `POST /rerank` mit `{"query": "...", "texts": ["...", ...]}`
  - Antwort `200` mit `[{"index": 0, "score": 0.97}, ...]`: genau ein Eintrag je Text, absteigend sortiert
  - Score = Sigmoid des Logits (wie TEI ohne `raw_scores`)
  - Weitere Felder (`return_text`, `raw_scores`, `truncate`) werden ignoriert.
- Grenzen (Statuscode und Grund in `error.reason`):
  - mehr als 512 Texte -> 413 `too_many_texts`
  - ein Text ueber 8 KiB -> 413 `text_too_long`
  - Body ueber 4 MiB -> 413 `body_too_large`
- `GET /health`:
  - `200 {"status": "ok", "model", "revision", "device", ...}`
  - sonst `503 {"status": "starting"|"selftest_failed", "reason": ...}`
- Eine Inferenz zur Zeit. Laengen werden auf Vielfache von 32 Token aufgerundet, damit OpenVINO weniger Formen kompiliert.

## Als Kind des Agenten

Ausschnitt fuer `agent/config.yaml`, Pfade anpassen:

```yaml
children:
  - name: rerank
    enabled: true
    cmd: C:\skirnir\rerank-server\.venv\Scripts\python.exe
    args: ["-I", "C:\\skirnir\\rerank-server\\server.py", "--device", "GPU.0", "--port", "8090"]
    cwd: C:\skirnir\rerank-server
    health_tcp_port: 8090
    startup_grace_s: 60
```

Den Pfad `/rerank` an diesen Dienst reichen die Tunnel-Routen des Agenten (`tunnel.routes`), den Endpunkt fuer Clients der
Router-Abschnitt `router.rerank`. Beide kommen in eigenen Aenderungen.

## Hindsight anbinden

Sobald Router und Agent das koennen. Kette: Router -> Hindsights eigener CPU-Reranker -> ohne Reranking.

```bash
HINDSIGHT_API_RERANKER_PROVIDER=tei
HINDSIGHT_API_RERANKER_TEI_URL=https://router.example:11434
HINDSIGHT_API_RERANKER_TEI_HTTP_TIMEOUT=3
HINDSIGHT_API_RERANKER_1_PROVIDER=local
HINDSIGHT_API_RERANKER_2_PROVIDER=rrf
```

Hindsights TEI-Client wiederholt 429 und 5xx dreimal und kennt keinen Circuit Breaker. Ist der Knoten nicht erreichbar, antwortet
der Router deshalb mit 424 (nicht wiederholt), und Hindsight faellt sofort auf seinen CPU-Reranker zurueck.
