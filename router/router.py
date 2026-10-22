#!/usr/bin/env python3
"""Skirnir Router - entry point. The code lives in the adjacent skirnir_router/ package (since 2026-09-09).

    python3 router.py /etc/skirnir-router/config.yaml
    python3 router.py --hash '<passwort>'
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from skirnir_router.auth import hash_password  # noqa: E402,F401  (Betriebsskripte ausserhalb des Repos importieren das von hier)
from skirnir_router.app import cli  # noqa: E402

if __name__ == "__main__":
    cli()
