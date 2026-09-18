# Decision-Engine-Auswertung 2026-09-17 00:21

Testsatz 132 Beispiele, Validierung 189 (Kalibrierung). Policy: {'min_top_probability': 0.5, 'min_margin': 0.2, 'max_entropy_ratio': 0.75}.

| Engine | Top-1 | Top-3 | sicher & richtig | Fallback-Rate | p50 ms | p95 ms | p99 ms | ECE | ECE kalibriert (T) | Fehler |
|---|---|---|---|---|---|---|---|---|---|---|
| embed | 0.932 | 1.000 | 0.944 | 0.061 | 22.31 | 32.23 | 42.37 | 0.112 | 0.0783 (0.5) | 0 |

## Konfusionsmatrix embed (Zeile = wahr, Spalte = gewaehlt)

| | standard | gross | assist | code |
|---|---|---|---|---|
| **standard** | 34 | 0 | 0 | 3 |
| **gross** | 4 | 32 | 0 | 0 |
| **assist** | 1 | 0 | 32 | 0 |
| **code** | 1 | 0 | 0 | 25 |
