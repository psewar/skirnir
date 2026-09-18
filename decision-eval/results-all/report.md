# Decision-Engine-Auswertung 2026-09-17 00:22

Testsatz 132 Beispiele, Validierung 189 (Kalibrierung). Policy: {'min_top_probability': 0.5, 'min_margin': 0.2, 'max_entropy_ratio': 0.75}.

| Engine | Top-1 | Top-3 | sicher & richtig | Fallback-Rate | p50 ms | p95 ms | p99 ms | ECE | ECE kalibriert (T) | Fehler |
|---|---|---|---|---|---|---|---|---|---|---|
| embed | 0.932 | 1.000 | 0.944 | 0.061 | 18.1 | 31.79 | 33.31 | 0.112 | 0.0783 (0.5) | 0 |
| rules | 0.523 | 0.985 | 0.829 | 0.735 | 16.33 | 31.53 | 31.84 | 0.153 | 0.1694 (0.8) | 0 |
| tfidf | 0.849 | 1.000 | 0.971 | 0.212 | 16.34 | 31.75 | 32.52 | 0.134 | 0.1038 (0.5) | 0 |
| local_llm | 0.977 | 1.000 | 0.977 | 0.000 | 201.9 | 216.23 | 227.64 | 0.023 | 0.0227 (0.5) | 0 |

Kette `embed -> local_llm -> rules`: Routing Accuracy 0.947, Fallback-Rate 0.000

Kette `tfidf -> local_llm -> rules`: Routing Accuracy 0.955, Fallback-Rate 0.000

Kette `embed -> tfidf -> rules`: Routing Accuracy 0.909, Fallback-Rate 0.038

Kette `tfidf -> embed -> local_llm -> rules`: Routing Accuracy 0.924, Fallback-Rate 0.000

## Konfusionsmatrix embed (Zeile = wahr, Spalte = gewaehlt)

| | standard | gross | assist | code |
|---|---|---|---|---|
| **standard** | 34 | 0 | 0 | 3 |
| **gross** | 4 | 32 | 0 | 0 |
| **assist** | 1 | 0 | 32 | 0 |
| **code** | 1 | 0 | 0 | 25 |

## Konfusionsmatrix rules (Zeile = wahr, Spalte = gewaehlt)

| | standard | gross | assist | code |
|---|---|---|---|---|
| **standard** | 34 | 0 | 3 | 0 |
| **gross** | 30 | 0 | 6 | 0 |
| **assist** | 22 | 0 | 11 | 0 |
| **code** | 2 | 0 | 0 | 24 |

## Konfusionsmatrix tfidf (Zeile = wahr, Spalte = gewaehlt)

| | standard | gross | assist | code |
|---|---|---|---|---|
| **standard** | 30 | 0 | 1 | 6 |
| **gross** | 4 | 28 | 4 | 0 |
| **assist** | 3 | 0 | 30 | 0 |
| **code** | 2 | 0 | 0 | 24 |

## Konfusionsmatrix local_llm (Zeile = wahr, Spalte = gewaehlt)

| | standard | gross | assist | code |
|---|---|---|---|---|
| **standard** | 34 | 0 | 3 | 0 |
| **gross** | 0 | 36 | 0 | 0 |
| **assist** | 0 | 0 | 33 | 0 |
| **code** | 0 | 0 | 0 | 26 |
