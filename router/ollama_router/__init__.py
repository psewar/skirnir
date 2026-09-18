r"""ollama-router: rollen- und zustandsbewusster Reverse-Proxy vor mehreren Ollama-Knoten.

Design: <repo>\design\routing-algorithm.md
Läuft auf router-host (Debian 12, Python 3.11, aiohttp 3.8). Keine weiteren Abhängigkeiten.

Seit 2026-09-09 als Paket (Stufe 0 des Ausbaus, siehe design/roadmap.md); Einstieg bleibt router.py.
"""
from .common import VERSION  # noqa: F401
