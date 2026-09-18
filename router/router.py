#!/usr/bin/env python3
"""Ollama-Router - Einstiegspunkt. Der Code liegt im Paket ollama_router/ daneben (seit 2026-09-09).

    python3 router.py /etc/ollama-router/config.yaml
    python3 router.py --hash '<passwort>'
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ollama_router.auth import hash_password  # noqa: E402,F401  (deploy/ui_auth_setup.py importiert das von hier)
from ollama_router.app import cli  # noqa: E402

if __name__ == "__main__":
    cli()
