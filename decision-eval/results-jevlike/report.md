# Decision-Engine-Auswertung 2026-09-16 23:55

Testsatz 132 Beispiele, Validierung 189 (Kalibrierung). Policy: {'min_top_probability': 0.5, 'min_margin': 0.2, 'max_entropy_ratio': 0.75}.

| Engine | Top-1 | Top-3 | sicher & richtig | Fallback-Rate | p50 ms | p95 ms | p99 ms | ECE | ECE kalibriert (T) | Fehler |
|---|---|---|---|---|---|---|---|---|---|---|
| tiny | 0.750 | 0.932 | 0.777 | 0.151 | 15.42 | 27.79 | 29.26 | 0.137 | 0.1352 (1.3) | 0 |
| hf | 0.750 | 0.992 | 0.772 | 0.038 | 62.39 | 75.42 | 76.66 | 0.195 | 0.1256 (2.45) | 0 |

Kette `tiny -> hf`: Routing Accuracy 0.788, Fallback-Rate 0.015

## Konfusionsmatrix tiny (Zeile = wahr, Spalte = gewaehlt)

| | standard | gross | assist | code |
|---|---|---|---|---|
| **standard** | 25 | 4 | 4 | 4 |
| **gross** | 0 | 36 | 0 | 0 |
| **assist** | 0 | 0 | 33 | 0 |
| **code** | 7 | 1 | 13 | 5 |

## Konfusionsmatrix hf (Zeile = wahr, Spalte = gewaehlt)

| | standard | gross | assist | code |
|---|---|---|---|---|
| **standard** | 28 | 8 | 1 | 0 |
| **gross** | 4 | 28 | 0 | 4 |
| **assist** | 2 | 0 | 29 | 2 |
| **code** | 0 | 12 | 0 | 14 |
