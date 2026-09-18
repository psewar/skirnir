# decision-embed - Stufe 2: Embedding-Modell auf CPU als Entscheidungsdienst

Mehrsprachiges Satz-Embedding (`intfloat/multilingual-e5-small`, 118 M Parameter, 384 Dimensionen) auf ONNX Runtime, dynamisch
auf int8 quantisiert (113 MB), Mean-Pooling, darauf eine Softmax-Regression fuer die Rollen. Gleiches HTTP-Protokoll wie
`decision-jevlike` (`POST /decide`, `GET /health`), im Router als Engine `embed`.

Gemessen 2026-09-17 (decision-eval, 132 Testfaelle): **0,932 Top-1**, Fallback 6 %, auf dem Router-CT (4-Kern-Xeon)
**10-19 ms** je Entscheidung, Container 456 MiB.

| Datei | Zweck |
|---|---|
| `export_model.py` | einmalig mit Netz: HF-Modell -> `models/e5-small/model.onnx` + `model_quantized.onnx` + Tokenizer (optimum) |
| `embedder.py` | Laufzeitkern: onnxruntime + tokenizers + numpy, Prefix `query: `, Mean-Pooling, L2-Norm |
| `train_head.py` | Embeddings des Datensatzes, Softmax-Regression und kNN, Latenz einzeln -> `models/e5-small/head.json` (7,7 MB, enthaelt auch die kNN-Vektoren) |
| `server.py` | Dienst (stdlib-HTTP), `--mode logreg|knn|mix`, Inferenz unter Lock |
| `Dockerfile`, `compose.yaml` | CPU-Container fuer router-host, nur `127.0.0.1:8082`, Modelle als Read-only-Volume, 768 MiB Limit |
| `deploy_ct.py` | Modellordner + Code nach `/opt/skirnir-embed` (SFTP + Container-Push), `docker compose up -d --build`, Gesundheit |
| `measure.py` | Latenzmessung im CT (60 Anfragen, p50/p95) |

```bash
..\decision-jevlike\.venv\Scripts\python export_model.py                       # einmalig
..\decision-jevlike\.venv\Scripts\python train_head.py --threads 4             # nach neuem Datensatz
..\decision-jevlike\.venv\Scripts\python server.py --port 8082                 # lokal testen
python deploy_ct.py                                                            # auf den CT
```

Router: `decision_engine.embed: { endpoint: http://127.0.0.1:8082 }`, dann `embed` in `chain` aufnehmen. Zur Laufzeit braucht
der Dienst kein Netz; Offline ist durch den Ordner gegeben (kein HF-Zugriff im Code).
