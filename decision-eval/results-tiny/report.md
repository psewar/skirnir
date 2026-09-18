# Decision-Engine-Auswertung 2026-09-16 23:14

Testsatz 135 Beispiele, Validierung 82 (Kalibrierung). Policy: {'min_top_probability': 0.5, 'min_margin': 0.2, 'max_entropy_ratio': 0.75}.

| Engine | Top-1 | Top-3 | sicher & richtig | Fallback-Rate | p50 ms | p95 ms | p99 ms | ECE | ECE kalibriert (T) | Fehler |
|---|---|---|---|---|---|---|---|---|---|---|
| tiny | 0.748 | 0.948 | 0.779 | 0.096 | 15.41 | 27.92 | 28.48 | 0.142 | 0.114 (1.3) | 0 |

## Konfusionsmatrix tiny (Zeile = wahr, Spalte = gewaehlt)

| | standard | gross | assist | code |
|---|---|---|---|---|
| **standard** | 6 | 0 | 9 | 4 |
| **gross** | 0 | 57 | 0 | 0 |
| **assist** | 1 | 0 | 32 | 0 |
| **code** | 3 | 1 | 16 | 6 |
