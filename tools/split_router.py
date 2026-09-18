#!/usr/bin/env python3
"""Stufe 0: zerlegt router/router.py in das Paket router/ollama_router/.

Rein mechanisch und wiederholbar:
  1. Jede Top-Level-Anweisung wird per Name einem Modul zugeordnet (MODULES). Fehlt ein Name, bricht das
     Werkzeug ab - nichts faellt still unter den Tisch.
  2. Querverweise auf Namen anderer Module werden ueber den Syntaxbaum modul-qualifiziert
     (`CFG` -> `state.CFG`, `Node` -> `nodes.Node`). Lokale Namen (Parameter, Zuweisungen, Closures) bleiben
     unangetastet; Namen aus `common` (Konstanten, log, reine Helfer) werden per Name importiert.
  3. `main()` verliert sein `global`, die Zuweisungen gehen an `state.*`. Der `__main__`-Block wird `app.cli()`.
  4. Imports pro Modul nur, was das Modul wirklich benutzt.

Danach prueft das Werkzeug selbst: alle Module kompilieren, das Paket importiert, und es meldet zwei Risiken,
die ein rein textueller Schnitt uebersieht - Lokalvariablen, die wie ein Modul heissen (`tunnel = ...` in einem
Modul, das `tunnel.Tunnel` braucht), und Querverweise auf Modulebene (Import-Zyklen zur Ladezeit).

Aufruf:  python tools/split_router.py           (schreibt Paket + Einstieg, Original nach tools/router.py.orig)
"""
import ast
import io
import os
import py_compile
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "router", "router.py")
PKG = os.path.join(ROOT, "router", "ollama_router")
BACKUP = os.path.join(ROOT, "tools", "router.py.orig")

# Modul -> Top-Level-Namen. Innerhalb eines Moduls bleibt die Quellreihenfolge erhalten.
MODULES = {
    "common": ["VERSION", "GIB", "log", "PROXY_METHODS", "INFER_PATHS", "FORBIDDEN_PATHS", "ollama_error",
               "parse_keep_alive", "openai_error", "split_listen", "safe_node_name"],
    "state": ["REG", "PENDING", "HA_PUB", "CFG", "NODES", "MQTT_DIRTY", "MEASURING", "PERF", "PERF_DIRTY",
              "BENCHING", "SESSION", "DECISIONS", "CAPS", "remember"],
    "config": ["Config", "cert_fingerprint", "read_overrides", "write_overrides", "roles_as_config"],
    "tunnel": ["TUN_REQ", "TUN_RESP", "TUN_DATA", "TUN_END", "TUN_ERR", "TUN_CANCEL", "TUN_HB", "TUN_HBACK",
               "tun_frame", "TunnelResponse", "TunnelRequest", "Tunnel"],
    "nodes": ["NodeNotReady", "nreq", "Node"],
    "registry": ["NodeRegistry", "read_mqtt_password", "provision_bundle", "node_spec_from_entry", "activate_node",
                 "attach_tunnel", "send_ctl", "approve_node", "revoke_node", "push_config", "handle_tunnel_v2",
                 "registry_view", "handle_nodes", "handle_node_action", "handle_node_delete", "handle_tunnel"],
    "perf": ["perf_path", "perf_load", "perf_save", "perf_stats", "perf_record", "BENCH_PROMPT", "bench_model",
             "handle_bench", "measure_model", "handle_measure"],
    "poll": ["poll_node", "fetch_caps", "evaluate", "unload_big_models", "prewarm", "poll_loop", "tick_loop",
             "apply_heartbeat", "handle_heartbeat"],
    "wol": ["send_magic_packet", "wake"],
    "scheduler": ["known_concrete", "resolve_tiers", "candidates_for", "rank", "choose", "wakeable_for",
                  "role_possible_when_free", "any_online_node_with"],
    "proxy": ["handle_infer", "route_request", "dispatch", "handle_tags", "tags_list", "handle_show", "handle_ps",
              "handle_version", "handle_forbidden", "handle_root"],
    "openai_api": ["oa_id", "oa_tool_calls", "oa_finish", "oa_usage", "oa_messages_to_native", "oa_options",
                   "oa_think", "oa_format", "oa_chat_to_native", "OpenAIChatShape", "OpenAICompletionShape",
                   "OpenAIEmbedShape", "oa_model_entry", "handle_oa_models", "handle_oa_model", "_oa_body",
                   "_include_usage", "handle_oa_chat", "handle_oa_completions", "handle_oa_embeddings",
                   "handle_oa_unknown"],
    "auth": ["hash_password", "verify_password", "basic_auth_middleware"],
    "ha": ["ha_snapshot", "handle_ha", "HAPublisher"],
    "admin": ["UI_PATH", "handle_ui", "handle_config_get", "handle_config_put", "handle_try", "catalog_effective",
              "handle_state"],
    "app": ["build_apps", "main"],
}
DOC = {
    "common": "Konstanten, Logger und reine Helfer ohne Zustand. Werden per Name importiert.",
    "state": "Der gemeinsame Laufzeitzustand des Routers. Immer als `state.X` ansprechen - CFG, SESSION, REG und\nHA_PUB werden beim Start neu gebunden, ein `from .state import CFG` saehe den alten Wert.",
    "config": "config.yaml + roles.yaml (UI-Overrides) einlesen, validieren, live neu laden.",
    "tunnel": "Agent -> Router: WebSocket-Tunnel mit Binaerrahmen (REQ/RESP/DATA/END/ERR/CANCEL/HB/HBACK).",
    "nodes": "Ein GPU-Knoten: Zustand, VRAM-Rechnung, Baseline, und `nreq` als einziger Weg zu seinem Ollama.",
    "registry": "Knotenregister mit Schluessel-Identitaet: Anmeldung, Freigabe, Sperren, Provisionierung.",
    "perf": "Leistungsdaten (passiv + Benchmark) und das Vermessen von Modellen fuer den Katalog.",
    "poll": "Polling der Knoten, Zustandsautomat free/busy, prewarm und Sicherheitsnetz.",
    "wol": "Wake-on-LAN.",
    "scheduler": "Auswahl: Rolle -> Tiers -> passende Knoten -> Rang.",
    "proxy": "Die Ollama-API nach aussen: Anfragen annehmen, zuweisen, durchreichen.",
    "openai_api": "OpenAI-kompatibles /v1 als Uebersetzung auf die native Ollama-API.",
    "auth": "Passwort-Hashes und Basic Auth fuer die Control-Plane.",
    "ha": "Home Assistant: Zustandsbild und MQTT-Discovery.",
    "admin": "Control-Plane: UI, Zustand, Konfiguration, Probelauf.",
    "app": "Zusammenbau der beiden aiohttp-Apps, Hauptschleife, Kommandozeile.",
}
IMPORTS = [  # (Name im Code, Importzeile)
    ("asyncio", "import asyncio"), ("base64", "import base64"), ("hashlib", "import hashlib"), ("hmac", "import hmac"),
    ("json", "import json"), ("logging", "import logging"), ("os", "import os"), ("secrets", "import secrets"),
    ("signal", "import signal"), ("socket", "import socket"), ("ssl", "import ssl"), ("sys", "import sys"),
    ("threading", "import threading"), ("time", "import time"), ("Counter", "from collections import Counter"),
    ("yaml", "import yaml"),
]
AIOHTTP = ["ClientError", "ClientSession", "ClientTimeout", "Fingerprint", "web"]
GUARDED = {
    "Ed25519PublicKey": ("try:\n    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # Debian: python3-cryptography\n"
                         "except ImportError:  # pragma: no cover\n    Ed25519PublicKey = None\n"),
    "mqtt": ("try:\n    import paho.mqtt.client as mqtt  # Debian: python3-paho-mqtt (1.6); optional\n"
             "except ImportError:  # pragma: no cover\n    mqtt = None\n"),
}


def fail(msg):
    print("FEHLER:", msg)
    sys.exit(1)


def target_names(node):
    """Namen, die eine Top-Level-Anweisung definiert."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.Assign):
        out = []
        for t in node.targets:
            for n in ast.walk(t):
                if isinstance(n, ast.Name):
                    out.append(n.id)
        return out
    return []


class Scope:
    def __init__(self, locals_, globals_):
        self.locals, self.globals = locals_, globals_


def collect_locals(fn):
    """Parameter + alles, was in der Funktion (inkl. verschachtelter Bloecke) zugewiesen wird; `global`-Namen nicht."""
    names, declared_global = set(), set()
    a = fn.args
    for p in a.posonlyargs + a.args + a.kwonlyargs:
        names.add(p.arg)
    if a.vararg:
        names.add(a.vararg.arg)
    if a.kwarg:
        names.add(a.kwarg.arg)
    for n in ast.walk(fn):
        if isinstance(n, ast.Global):
            declared_global.update(n.names)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n is not fn:
            names.add(n.name)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for al in n.names:
                names.add((al.asname or al.name).split(".")[0])
    return names - declared_global, declared_global


def main():
    src = io.open(SRC, encoding="utf-8").read()
    if "ollama_router" in src[:400]:
        fail("router.py ist schon der Einstieg - Original aus tools/router.py.orig wiederherstellen, dann erneut")
    lines = src.split("\n")
    tree = ast.parse(src)

    owner = {}
    for mod, names in MODULES.items():
        for n in names:
            if n in owner:
                fail(f"{n} zweimal zugeordnet ({owner[n]}, {mod})")
            owner[n] = mod

    # --- Top-Level-Anweisungen zuordnen -------------------------------------------------------------------
    assigned = {m: [] for m in MODULES}        # mod -> [(start_line, end_line, node)]
    main_block = None
    prev_end = 0
    seen = set()
    for node in tree.body:
        start, end = prev_end + 1, node.end_lineno
        prev_end = end
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) and node.lineno < 10:
            continue                                                       # Modul-Docstring
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.Try) and all(isinstance(b, (ast.Import, ast.ImportFrom)) for b in node.body):
            continue                                                       # geschuetzter Import
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and "__main__" in ast.dump(node.test):
            main_block = (start, end, node)
            continue
        names = target_names(node)
        if not names:
            fail(f"Zeile {node.lineno}: Anweisung ohne Namen ({type(node).__name__}) - bitte zuordnen")
        mods = {owner.get(n) for n in names}
        if None in mods or len(mods) != 1:
            fail(f"Zeile {node.lineno}: {names} nicht (eindeutig) zugeordnet: {mods}")
        seen.update(names)
        assigned[mods.pop()].append((start, end, node))
    missing = set(owner) - seen
    if missing:
        fail(f"zugeordnet, aber nicht in der Quelle gefunden: {sorted(missing)}")
    if main_block is None:
        fail("__main__-Block nicht gefunden")

    # --- Querverweise umschreiben (byte-genau ueber ast-Positionen) -------------------------------------
    raw_lines = [l.encode("utf-8") for l in lines]
    edits = {}            # lineno -> [(col, end_col, text)]
    refs_by_mod = {m: set() for m in MODULES}   # Modul -> Module, auf die es wirklich per `mod.Name` zugreift
    shadow_report, toplevel_refs = set(), []

    def visit(node, mod, scopes):
        """Namen im Teilbaum umschreiben. scopes = Stapel von Scope (innerste zuletzt)."""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            loc, glob = collect_locals(node) if not isinstance(node, ast.Lambda) else (
                {p.arg for p in node.args.posonlyargs + node.args.args + node.args.kwonlyargs}
                | ({node.args.vararg.arg} if node.args.vararg else set()) | ({node.args.kwarg.arg} if node.args.kwarg else set()), set())
            shadow_report.update(loc & set(owner))
            inner = scopes + [Scope(loc, glob)]
            # Defaults/Dekoratoren gehoeren zum umgebenden Scope
            if not isinstance(node, ast.Lambda):
                for d in node.decorator_list:
                    visit(d, mod, scopes)
                for d in node.args.defaults + [x for x in node.args.kw_defaults if x is not None]:
                    visit(d, mod, scopes)
                for ret in ([node.returns] if node.returns else []):
                    visit(ret, mod, scopes)
                for b in node.body:
                    visit(b, mod, inner)
            else:
                visit(node.body, mod, inner)
            return
        if isinstance(node, ast.ClassDef):
            for d in node.decorator_list + node.bases + [k.value for k in node.keywords]:
                visit(d, mod, scopes)
            for b in node.body:
                visit(b, mod, scopes)     # Klassenkoerper: Methoden bringen ihren eigenen Scope mit
            return
        if isinstance(node, ast.Global):
            return
        if isinstance(node, ast.Name):
            nm = node.id
            own = owner.get(nm)
            if own is None or own == mod or own == "common":
                return
            local = any(nm in s.locals for s in scopes)
            declared = any(nm in s.globals for s in scopes)
            if local and not declared:
                return
            if isinstance(node.ctx, ast.Store) and not declared:
                return
            if not scopes:
                toplevel_refs.append((mod, nm, node.lineno))
            refs_by_mod[mod].add(own)
            edits.setdefault(node.lineno, []).append((node.col_offset, node.end_col_offset, f"{own}.{nm}"))
            return
        for child in ast.iter_child_nodes(node):
            visit(child, mod, scopes)

    for mod, items in assigned.items():
        for _, _, node in items:
            visit(node, mod, [])
    # __main__-Block gehoert zu app
    visit(main_block[2], "app", [Scope(set(), set())])

    def rendered(start, end):
        out = []
        for ln in range(start, end + 1):
            b = raw_lines[ln - 1]
            for col, ecol, text in sorted(edits.get(ln, []), key=lambda e: -e[0]):
                b = b[:col] + text.encode("utf-8") + b[ecol:]
            out.append(b.decode("utf-8"))
        return out

    # --- Module schreiben ------------------------------------------------------------------------------
    os.makedirs(PKG, exist_ok=True)
    written = {}
    clashes = []
    for mod, items in assigned.items():
        body = []
        for start, end, node in items:
            seg = rendered(start, end)
            seg = [l for l in seg if not re.match(r"^# -{5,}", l)]          # Sektionsmarker weg
            body.extend(seg)
        text = "\n".join(body)
        if mod == "app":
            ms, me, mnode = main_block
            cli = rendered(mnode.body[0].lineno, me)                        # Koerper des if-Blocks (schon 4 eingerueckt)
            text += ("\n\n\ndef cli():\n    \"\"\"Kommandozeile: `router.py <config.yaml>` oder `router.py --hash <passwort>`.\"\"\"\n"
                     + "\n".join(cli) + "\n")
            text = text.replace("    global state.CFG, state.SESSION, state.NODES, state.REG, state.HA_PUB\n", "")
            text = text.replace("    global CFG, SESSION, NODES, REG, HA_PUB\n", "")
            if "global " in text:
                fail("app: global-Zeile nicht entfernt")
        if mod == "admin":
            old = 'UI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")'
            if text.count(old) != 1:
                fail("admin: UI_PATH-Zeile nicht gefunden")
            text = text.replace(old, 'UI_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui.html")   # ui.html liegt neben router.py, eine Ebene ueber dem Paket')
        text = re.sub(r"\n{4,}", "\n\n\n", text).strip("\n") + "\n"
        # Imports: nur was vorkommt
        used = {n.id for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Name)}
        head = [f'"""{DOC[mod]}"""']
        std = [line for name, line in IMPORTS if name in used]
        if std:
            head += [""] + std
        aio = [n for n in AIOHTTP if n in used]
        if aio:
            head += ["", f"from aiohttp import {', '.join(aio)}"]
        for name, block in GUARDED.items():
            if name in used:
                head += ["", block.rstrip("\n")]
        # Modulbezuege aus den echten Umschreibungen - NICHT aus gleichlautenden Bezeichnern: ein Parameter
        # `nodes` in scheduler.rank() ist kein Bezug auf das Modul nodes
        rel_mods = sorted(refs_by_mod[mod] - {"common", mod})
        common_names = [n for n in MODULES["common"] if n in used] if mod != "common" else []
        if rel_mods or common_names:
            head.append("")
            if rel_mods:
                head.append(f"from . import {', '.join(rel_mods)}")
            if common_names:
                head.append(f"from .common import {', '.join(common_names)}")
        written[mod] = "\n".join(head) + "\n\n\n" + text
        # Lokalvariablen, die wie ein referenziertes Modul heissen -> `tunnel.Tunnel` griffe ins Leere
        for fn in [n for n in ast.walk(ast.parse(text)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            loc, _ = collect_locals(fn)
            clash = loc & set(rel_mods)
            if clash:
                clashes.append(f"{mod}.{fn.name}: {sorted(clash)}")
    if clashes:
        fail("lokale Namen kollidieren mit referenzierten Modulen (im Original umbenennen): " + "; ".join(clashes))

    doc = (ast.get_docstring(tree) or "ollama-router").strip()   # der urspruengliche Modul-Docstring, ohne Anfuehrungszeichen
    init = ('r"""' + doc + "\n\nSeit 2026-09-09 als Paket (Stufe 0 des Ausbaus, siehe design/roadmap.md); Einstieg bleibt router.py.\n\"\"\"\n"
            "from .common import VERSION  # noqa: F401\n")
    shim = ('#!/usr/bin/env python3\n'
            '"""Ollama-Router - Einstiegspunkt. Der Code liegt im Paket ollama_router/ daneben (seit 2026-09-09).\n\n'
            '    python3 router.py /etc/ollama-router/config.yaml\n    python3 router.py --hash \'<passwort>\'\n"""\n'
            'import os\nimport sys\n\nsys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n\n'
            'from ollama_router.auth import hash_password  # noqa: E402,F401  (deploy/ui_auth_setup.py importiert das von hier)\n'
            'from ollama_router.app import cli  # noqa: E402\n\nif __name__ == "__main__":\n    cli()\n')

    if not os.path.exists(BACKUP):
        io.open(BACKUP, "w", encoding="utf-8", newline="\n").write(src)
    for mod, text in written.items():
        io.open(os.path.join(PKG, mod + ".py"), "w", encoding="utf-8", newline="\n").write(text)
    io.open(os.path.join(PKG, "__init__.py"), "w", encoding="utf-8", newline="\n").write(init)
    io.open(SRC, "w", encoding="utf-8", newline="\n").write(shim)

    # --- Pruefen -----------------------------------------------------------------------------------------
    for f in sorted(os.listdir(PKG)):
        if f.endswith(".py"):
            py_compile.compile(os.path.join(PKG, f), doraise=True)
    r = subprocess.run([sys.executable, "-c", "import ollama_router.app, ollama_router.ha; print('import ok')"],
                       cwd=os.path.dirname(SRC), capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip()[-800:])
    if r.returncode:
        fail("Paket importiert nicht")
    sizes = {m: written[m].count("\n") for m in written}
    print("Module:", ", ".join(f"{m} {n}" for m, n in sizes.items()), f"| gesamt {sum(sizes.values())} Zeilen")
    if shadow_report:
        print("Hinweis - lokale Namen, die auch Top-Level-Namen sind (bleiben lokal, bitte gegenlesen):", sorted(shadow_report))
    if toplevel_refs:
        print("Hinweis - Querverweise auf Modulebene (Ladezeit, Zyklus-Risiko):", toplevel_refs)
    print("fertig")


main()
