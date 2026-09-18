# decision-jevlike - Jevlike als lokaler Entscheidungsdienst fuer Skirnir

[Jevlike](https://github.com/vinnylarouge/jevlike) (MIT, Research-Starter, Commit `94f5fd1`) trainiert kleine Modelle, die aus
einem Kontext und N Optionen in einem Vorwaertsdurchlauf eine Wahrscheinlichkeit je Option liefern. Skirnir nutzt das fuer
die Auto-Rolle: Kontext = letzte Benutzernachricht, Optionen = Rollen (`standard`, `gross`, `assist`, `code`). Der Router
kennt nur das HTTP-Protokoll von `server.py`; Jevlike selbst laeuft als eigener Prozess oder Container.

## Dateien

| Datei | Zweck |
|---|---|
| `server.py` | Dienst: `POST /decide {context, options}` -> Verteilung, `GET /health`. Standardbibliothek + torch + jevlike, Offline-Variablen vor dem Import, Inferenz unter Lock |
| `prepare_encoder.py` | einmaliger Download eines HF-Encoders in `models/hf-cache` (danach `HF_HUB_OFFLINE=1`) |
| `Dockerfile`, `compose.yaml` | CPU-Container fuer router-host, nur `127.0.0.1:8081`, Checkpoints als Read-only-Volume, 768 MiB Limit |
| `requirements.txt` | lokale venv fuer Training/Auswertung auf gpu-desktop (CUDA 12.8) |
| `models/` | Checkpoints (`skirnir-router-tiny.pt` ~0,6 MB) und HF-Cache; nicht im Repo |

Training und Auswertung liegen in `../decision-eval/` (`build_dataset.py`, `train.py`, `run_eval.py`).

## Betrieb

```bash
# lokal (venv)
.venv\Scripts\python server.py --checkpoint models/skirnir-router-tiny.pt --port 8081
curl -s localhost:8081/decide -d '{"context":"Schalte das Licht im Wohnzimmer an","options":["standard","gross","assist","code"]}'

# router-host (Docker): Checkpoint nach /opt/skirnir-jevlike/models/ kopieren, dann
docker compose up -d --build
# Router: decision_engine.jevlike.endpoint: http://127.0.0.1:8081, chain: [jevlike, rules]
```

Offline: `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `HF_HUB_DISABLE_TELEMETRY=1` sind im Bild gesetzt und werden in
`server.py` vor dem Import von torch/transformers gesetzt. Der Byte-Encoder braucht gar keinen HF-Cache. `--allow-network`
gibt es nur fuer Setup-Zwecke.

## Grenzen

- Der Byte-Encoder sieht `context_tokens` Bytes (Skirnir trainiert mit 512); laengere Anfragen werden abgeschnitten, darum
  steht der Benutzertext vorn und die Hinweise (`[tools=1]`) hinten.
- Ein eingefrorener Qwen2.5-0.5B-Encoder braucht ~2 GiB RAM (fp32) und passt nicht auf den 4-GiB-CT; dafuer ist gpu-desktop (GPU)
  der Ort - als weiteres Kind des Agent-Dienstes -, mit der Regel-Engine als Rueckfall, wenn der PC schlaeft.
- Wahrscheinlichkeiten sind Modellausgaben, keine kalibrierten Konfidenzen; die Kalibrierung (Temperatur) misst
  `decision-eval/run_eval.py` und der Router wendet sie nur an, wenn `calibration.json` sie fuer genau dieses Modell enthaelt.
