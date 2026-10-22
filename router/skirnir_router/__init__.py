r"""skirnir-router: role- and state-aware reverse proxy in front of multiple Ollama nodes.

Design: <repo>\design\routing-algorithm.md
Runs on router-host (Debian 12, Python 3.11, aiohttp 3.8). No other dependencies.

Packaged since 2026-09-09 (stage 0 of the expansion, see design/roadmap.md); entry point remains router.py.
"""
from .common import VERSION  # noqa: F401
