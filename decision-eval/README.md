# decision-eval - Datensatz, Training und Messreihe der Decision Engine

## Ergebnis (2026-09-17, Testsatz 132 Beispiele aus ungesehenen Vorlagen-Gruppen, 1282 Beispiele gesamt)

| Engine | Top-1 | Top-3 | sicher & richtig | Fallback-Rate | Latenz p50 / p95 | ECE | Wo gemessen |
|---|---|---|---|---|---|---|---|
| Rules (Stichwoerter) | 0,523 | 0,985 | 0,829 | 0,735 | 16 / 31 ms (Netz; Engine 0,1 ms) | 0,153 | Produktion, `/admin/decide` |
| **tfidf** (Stufe 1: Zeichen-n-Gramme + Softmax-Regression, 570 KB) | **0,848** | 1,000 | **0,971** | 0,212 | 25 / 31 ms (Netz; Engine <1 ms) | 0,134 | Produktion, im Router-Prozess |
| Jevlike Byte-Encoder (`tiny`, 0,6 MB) | 0,750 | 0,932 | 0,777 | 0,151 | 15 / 28 ms | 0,137 | gpu-desktop, eigener Dienst |
| Jevlike + eingefrorener Qwen2.5-0.5B (`hf`) | 0,750 | 0,992 | 0,772 | 0,038 | 62 / 75 ms (GPU) | 0,195 | gpu-desktop, eigener Dienst |
| **embed** (Stufe 2: multilingual-e5-small ONNX int8 + Softmax-Kopf auf 384-dim Vektoren) | **0,932** | 1,000 | 0,944 | 0,061 | 18 / 32 ms lokal; **CT: 10-19 / 14-23 ms** | 0,112 -> 0,078 (T 0,5) | Container auf router-host, 456 MiB |
| Lokales LLM (qwen3.6:35b-a3b als `assist`, JSON-Schema, Rollenbeschreibungen) | **0,977** | 1,000 | 0,977 | 0,000 | **200 / 225 ms** | 0,023 | Produktion ueber den Router, gpu-desktop warm |

Ketten (erste sichere Engine gewinnt, `results-all/`): `tfidf -> local_llm -> rules` **0,955** (LLM in 21 %), `embed -> local_llm -> rules` 0,947 (LLM in 6 %), `embed -> tfidf -> rules` 0,909 (rein CPU auf dem CT, kein LLM), `tfidf -> embed -> local_llm -> rules` 0,924.

Kaskade `tfidf -> local_llm -> rules`: **0,955** Routing Accuracy, das LLM wird nur in 21 % der
Faelle gefragt, mittlere Latenz rund 42 ms statt 200 ms. Vorher (Datensatz 1047, 2026-09-16): tiny 0,748, hf 0,689, rules 0,585,
LLM 1,000; die Aenderung kommt von neuen englischen Vorlagen fuer `gross`/`standard` - vorher fehlte dem Training jede englische
Analyse-Anfrage, und eine Testgruppe mit 28 solchen Beispielen fiel bei allen kleinen Modellen komplett um.

**Antwort auf die Kernfrage des Vorschlags** (kann ein kleines, lokales, nicht-generatives Modell die Routing-Frage schnell
und zuverlaessig genug uebernehmen?): **als Alleinentscheider nein, als Vorfilter ja.** Jevlike bringt fuer feste Rollen nichts,
was eine schlichte TF-IDF-Klassifikation nicht besser und ohne Abhaengigkeiten kann. Die tfidf-Engine liegt bei 85 %, aber wo
sie sich sicher ist (79 % der Anfragen), zu 97 % richtig; den Rest uebernimmt das LLM. Das ist die Kaskade, die der Vorschlag
meinte, nur mit einem linearen Modell statt Jevlike an der Spitze.

**Stufe 2 (2026-09-17 00:30):** `decision-embed/` - multilingual-e5-small (118 M Parameter) nach ONNX exportiert und dynamisch auf
int8 quantisiert (448 -> 113 MB), Mean-Pooling, Softmax-Regression auf den 384-dim Vektoren (kNN und Mischung waren schlechter:
0,879 / 0,917). int8 kostet keine Genauigkeit (fp32 0,924 vs int8 0,939 beim Kopftraining, Rauschen). Auf dem CT (4-Kern-Xeon,
Container mit 4 Threads) 10-19 ms je Entscheidung inkl. HTTP, 456 MiB RAM. Schwaeche: die Fehler sind selbstbewusst
(sicher-und-richtig 0,944 bei nur 6 % Fallback), darum gewinnt die Kaskade mit dem LLM wenig; kNN-Uneinigkeit als zweites
Unsicherheitsmass waere der naechste Versuch.

Befunde je Engine:
- **tfidf** verwechselt noch `standard` mit `code` (6) und `gross` mit `standard`/`assist` (je 4): kurze Alltagsfragen ohne
  Fachwoerter und englische Analyse-Prompts. Mehr echte Daten helfen direkt; das Modell trainiert in 20 s neu.
- **Byte-Encoder** lernt Oberflaeche (Laenge, Wortformen); `gross` und `assist` fehlerfrei, `code` landet bei `assist` (13/26).
- **Frozen-Encoder-Kopf** ueberfittet in 2-3 Epochen (train NLL 1e-6, validation steigt), egal ob rank 32-256; Top-1 schwankt
  zwischen Laeufen von 0,38 bis 0,75; braucht ~2 GiB RAM und passt nicht auf den Router-CT.
- **Kalibrierung:** Temperatur hilft der Byte-Variante kaum (0,137 -> 0,135), beim Frozen-Encoder etwas (0,195 -> 0,126); bei
  tfidf/rules findet die Rastersuche T = 0,5 (Verteilung zu flach) - alles Zeichen fuer zu wenige, zu gleichfoermige Daten.
- **Vorbehalt Datensatz:** 1282 Beispiele aus 136 Vorlagen (Deutsch/Englisch, Smart-Home, Coding, Analysen, Alltag), Split nach
  Vorlage. Vorlagen-Sprache ist sauberer als echter Verkehr; 97,7 % beim LLM heisst: der Satz ist fuer ein 35B-Modell leicht.

**Stand Produktion (2026-09-17 00:30):** Kette `[embed, local_llm, tfidf, rules]`, Capture an fuer zwei Clients (Agenten-Framework, Node-RED) (anonymisiert, `/var/lib/skirnir-router/decisions.jsonl`; `build_dataset.py --capture` nimmt die `client`-Labels). Empfehlung: nach ein paar Wochen Capture
(Agenten-Framework, Node-RED) den tfidf auf echten Daten neu trainieren, dann `[tfidf, local_llm, rules]` schalten und Stufe 2
(mehrsprachiges Embedding-Modell, ONNX, CPU) nur angehen, wenn tfidf auf echten Daten unter ~90 % bleibt.

## Dateien

| Datei | Zweck |
|---|---|
| `build_dataset.py` | Vorlagen -> `data/{train,validation,test}.jsonl` (Jevlike-Format + `role`, `group`), Split je Rolle nach Gruppen, `--capture` mischt echte Client-Labels dazu |
| `train_tfidf.py` | Stufe 1: TF-IDF + Softmax-Regression (numpy, 20 s) -> `router/decision-tfidf.json`, deploy.py legt es nach `/etc/skirnir-router/` |
| `train.py` | Jevlike-Training beider Varianten (`tiny`, `hf`) mit Skirnir-Parametern (Kontext 512), danach `jevlike-eval` |
| `run_eval.py` | Messreihe: startet Jevlike-Dienste aus Checkpoints, fragt rules/local_llm ueber `/admin/decide`, rechnet Top-1/Top-3, Konfusion, p50/p95/p99, Fallback-Rate, ECE, Temperatur -> `results*/report.md`, `calibration.json` |
| `results-router/` | rules + tfidf + local_llm gegen Produktion (2026-09-17 00:00) |
| `results-jevlike/` | tiny + hf lokal (2026-09-16 23:50) |
| `results-embed/`, `results-all/` | Stufe 2 allein bzw. alle Engines mit vier Ketten (2026-09-17 00:30); `results-all/calibration.json` fuer `decision_engine.calibration_path` |
| `train-hf.log` | Verlauf des Frozen-Encoder-Trainings (Ueberfitten ab Epoche 2 sichtbar); entsteht lokal, nicht im Repo (`*.log`) |

```bash
python build_dataset.py                                   # Datensatz
python train_tfidf.py                                     # Stufe 1 (20 s, CPU)
..\decision-jevlike\.venv\Scripts\python train.py tiny    # Byte-Encoder (GPU, ~1 min)
..\decision-jevlike\.venv\Scripts\python run_eval.py --jevlike tiny=..\decision-jevlike\models\skirnir-router-tiny.pt
python run_eval.py --jevlike embed=http://127.0.0.1:8082 --router https://router.example.net:11435 --router-auth-ops --router-engines rules,tfidf,local_llm --chains "embed,local_llm,rules;tfidf,local_llm,rules"
```
