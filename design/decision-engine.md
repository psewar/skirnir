# Decision Engine (2026-09-16) - Entwurf und Abweichungen vom Vorschlag

Vorschlag des Betreibers: eine generische Decision-Engine-Abstraktion, zuerst mit Jevlike (lokal, nicht generativ) als Provider, um
die semantische Routing-Frage "welche Rolle passt zu dieser Anfrage?" schnell zu beantworten und ein LLM nur noch fuer
schwierige Faelle zu brauchen. Dieser Text haelt fest, was umgesetzt ist und wo ich bewusst abweiche.

## Was gebaut ist

| Baustein | Ort | Kurz |
|---|---|---|
| Grundtypen | `router/ollama_router/decision/base.py` | `DecisionRequest`, `DecisionResult` (Verteilung, `model_probability`, `calibrated_confidence`, Engine, Modell, Latenz, Unsicherheit, Fallback-Spur), Unsicherheitsmasse, Temperatur-Skalierung |
| Engines | `decision/rules.py`, `decision/tfidf.py`, `decision/jevlike.py`, `decision/local_llm.py` | Regeln (letzte Stufe), TF-IDF + Softmax-Regression (Stufe 1, 2026-09-17, im Prozess, <1 ms), Jevlike-Adapter (HTTP zu eigenem Prozess), kleines LLM ueber den eigenen Router mit erzwungenem JSON-Schema |
| Kette + Fallback | `decision/__init__.py` (`Decider`) | erste sichere Engine gewinnt; unsicher = Abstand Top1/Top2, normierte Entropie, Mindest-p; alle unsicher -> letzte entscheidet (markiert `forced`), alle ausgefallen -> `default` |
| Integration | `proxy._decide_role` | nur die Auto-Rolle (`auto:latest`) geht durch die Engine; Ergebnis ist eine bestehende Rolle, danach unveraenderter Weg (Stufen, Faehigkeiten, Verfuegbarkeit, Score). Antwort traegt `routing.decision` |
| Messbarkeit | `metrics` (`skirnir_decision_total`, `_latency_seconds`, `_fallback_total`), Entscheidungsprotokoll `event: decision`, UI-Protokollzeile | |
| Trainingsdaten | `decision/capture.py` | JSONL im Jevlike-Format; Labels `client` (echte Rollenwahl eines Clients) getrennt von `engine` (Pseudo-Label); Opt-in je Client, Anonymisierung, Gruppen-Hash fuer saubere Splits |
| Admin | `POST /admin/decide`, `GET /admin/decision` | Engine direkt fragen (auch eine bestimmte), Status/Gesundheit |
| Embedding-Dienst (Stufe 2) | `decision-embed/` | multilingual-e5-small als ONNX int8 + Softmax-Kopf, gleiches Protokoll wie Jevlike (`JevlikeEngine(name="embed")`), Container auf dem Router-Host; Beispielkette `[embed, local_llm, tfidf, rules]` |
| Jevlike-Dienst | `decision-jevlike/` | `server.py` (stdlib-HTTP + torch, Offline-Variablen vor dem Import), `Dockerfile`/`compose.yaml` (nur 127.0.0.1:8081), `prepare_encoder.py` (einmaliger HF-Download in den Cache) |
| Datensatz + Auswertung | `decision-eval/` | Vorlagen aus dem Betrieb, Split nach Gruppen, `run_eval.py` mit Top-1/Top-3, Konfusionsmatrix, Latenz p50/p95/p99, Fallback-Rate, ECE, Temperatur-Kalibrierung |

## Abweichungen und Ergaenzungen

1. **Python statt C#-Interface.** Skirnir ist Python; `DecisionEngine` ist eine kleine Basisklasse mit `decide()` und
   `health()`, die Datentypen sind Dataclasses. Das Protokoll zum Jevlike-Dienst ist JSON ueber HTTP, Jevlikes eigene
   Typen (Checkpoint, Collator, Tensoren) bleiben im Dienst.
2. **Einstiegspunkt ist eine Auto-Rolle, nicht jede Anfrage.** Skirnirs Clients waehlen heute bewusst Rollen (HA -> assist,
   Node-RED -> standard, Agenten-Framework -> code). Diese Wahl ist wertvoll und bleibt unangetastet. Die Engine greift nur, wenn ein
   Client `auto:latest` sagt. So gibt es keinen Regressionsweg fuer bestehende Clients, und die expliziten Rollenwahlen
   liefern kostenlos echte Labels fuer den Trainingssatz (Opt-in).
3. **Drei Engines von Anfang an**, damit die Messreihe des Vorschlags (Rules vs. Jevlike Byte vs. Jevlike+Encoder vs.
   kleines LLM) sofort laufen kann: `rules` (deterministisch, null Abhaengigkeiten), `jevlike`, `local_llm` (nutzt den Router
   selbst: Admission, Breaker, Metriken greifen; erzwungenes JSON-Schema, kein Denken). TypeSafe Jev ist als vierter Provider
   vorgesehen (gleiche Schnittstelle), aber nicht gebaut.
4. **Fallback ist eine Kette, nicht ein Paar.** `chain: [jevlike, rules]` oder `[jevlike, local_llm, rules]`; jede Stufe
   wird an denselben Unsicherheitsmassen gemessen; Ausfaelle (Dienst weg, Timeout, unbrauchbare Antwort) zaehlen als
   Fallback-Grund `error`. Schwellen sind Einstellungen (UI-editierbar) und gelten live.
5. **ModelProbability vs. CalibratedConfidence sind zwei Felder.** `calibrated_confidence` bleibt `null`, bis in
   `calibration.json` eine gemessene Temperatur fuer genau diese Engine/Modell-Kombination steht (decision-eval fittet sie
   auf dem Validierungssatz und misst ECE vorher/nachher). Der Router wendet sie an und schreibt die rohe Verteilung in
   `metadata.raw_probabilities`.
6. **Kontext ist die letzte Benutzernachricht, nicht der ganze Verlauf.** Ein Byte-Encoder sieht nur die ersten
   `context_tokens` Bytes; darum steht der Benutzertext vorn, Hinweise (`[tools=2] [images=1]`) hinten, und der Jevlike-
   Checkpoint wird mit `--context-tokens 512` trainiert (Standard 192 waere zu kurz fuer deutsche Saetze).
7. **Deployment-Realitaet:** router-host hat 4 CPUs, 4 GiB RAM und kein GPU. Der Byte-Encoder passt dort in einen Container
   (768 MiB Limit). Ein eingefrorener Qwen2.5-0.5B-Encoder (~2 GiB fp32) passt nicht - er kaeme als weiteres Kind des
   Agent-Dienstes auf gpu-desktop (GPU) und faellt weg, wenn gpu-desktop schlaeft; die Kette faengt das auf (`rules`).
8. **Datenschutz vor Datenmenge.** Capture ist aus, bis ein Client in `capture.clients` steht; Kontexte werden gekuerzt und
   anonymisiert (Schluessel/Passwoerter wie der Cloud-Credential-Scan, E-Mail, IP, URL, lange Zahlen). Pseudo-Labels der
   Engine sind markiert und gehen nicht ungesichtet ins Training (`build_dataset.py --capture` nimmt nur `client`-Labels).

## Offen (bewusst)

- TypeSafe Jev als Provider (Schnittstelle steht, keine Implementierung).
- Faehigkeits-Hinweise aus der Engine (Abschnitt 6, `vision true/0.99`): heute leitet `request.implicit_caps` Faehigkeiten
  deterministisch aus dem Body ab (Bilder, Tools, format). Eine gelernte Ergaenzung braeuchte erst Daten.
- ~~Produktiv-Einsatz der Auto-Rolle: haengt an den Messwerten aus `decision-eval`.~~ Seit 2026-09-17 produktiv mit der
  Kette `embed -> local_llm -> tfidf -> rules` (Beispielkonfiguration). Jevlike bleibt Provider, ist aber nicht in der Kette.

## Nachtrag 2026-09-17: Stufe 1 und Ausblick

Gemessen (decision-eval/README.md): tfidf 0,848 Top-1, sicher-und-richtig 0,971; LLM 0,977 bei 200 ms; Kaskade
`tfidf -> local_llm -> rules` 0,955 mit LLM in nur 21 % der Faelle. Jevlike bleibt als Provider erhalten, ist fuer feste
Rollen aber nicht der richtige Hebel. Geplante Stufen: (2) mehrsprachiges Embedding-Modell (z. B. multilingual-e5-small,
ONNX int8, ~150 MB RAM, 15-40 ms auf 4 Kernen) mit kNN/logistischer Regression auf den Vektoren - passt auf den CT als
Container; (3) feinabgestimmter kleiner Transformer, erst mit 1000-2000 echten Beispielen. Datenweg: Capture an, LLM als
Labeler (Distillation), woechentlich sichten und neu trainieren; unsichere Faelle der Kaskade sind automatisch die
wertvollsten neuen Trainingsbeispiele.

## Nachtrag 2026-09-17 (spaet): Stufe 2 gebaut und gemessen

`decision-embed/`: multilingual-e5-small als ONNX int8 (113 MB) + Softmax-Kopf, Dienst mit demselben Protokoll wie Jevlike
(Adapter `JevlikeEngine(name="embed")`), Container auf router-host (127.0.0.1:8082, 456 MiB). Test: 0,932 Top-1, 10-19 ms auf dem
CT. Damit ist die CPU-gebundene, rein lokale Entscheidung moeglich (`embed -> tfidf -> rules` 0,909 ohne LLM). Der Kopf lernt
in Sekunden neu, das Embedding-Modell bleibt - der Weg fuer echte Daten aus dem Capture. Offen: besseres Unsicherheitsmass
(kNN-Uneinigkeit), damit die Kaskade zum LLM greift, wo embed irrt.
