#!/usr/bin/env python3
"""Repraesentativer Skirnir-Datensatz fuer die Decision Engine: Anfragen -> Rolle (standard | gross | assist | code).

Quelle sind Vorlagen aus dem echten Betrieb (HA-Sprachbefehle, Node-RED-Aufgaben, Log-Triage, Coding ueber Agenten-Frameworks,
lange Analysen), jede mit Platzhaltern und Varianten. Jede Vorlage ist eine **Gruppe**: alle Auspraegungen einer Vorlage
landen im selben Split (train/validation/test), sonst misst die Auswertung Auswendiglernen statt Verstehen (Abschnitt 9).
Deutsch und Englisch gemischt, Schweizer Schreibweise (ss statt ß) wie im Haushalt ueblich.

    python build_dataset.py [--out data] [--seed 7] [--val 0.15] [--test 0.15]
Schreibt data/{train,validation,test}.jsonl im Jevlike-Format ({context, options, label}) plus group/role je Zeile und
data/summary.json. Optional --capture <decisions.jsonl>: echte, anonymisierte Client-Labels aus dem Router dazu mischen.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import random

OPTIONS = ["standard", "gross", "assist", "code"]

# Platzhalter fuer die Vorlagen
FILL = {
    "raum": ["Wohnzimmer", "Kueche", "Schlafzimmer", "Buero", "Bad", "Flur", "Garage", "Kinderzimmer", "Esszimmer"],
    "geraet": ["Licht", "Deckenlampe", "Stehlampe", "Steckdose", "Ventilator", "Fernseher", "Radio", "Kaffeemaschine"],
    "zeit": ["10 Minuten", "eine halbe Stunde", "morgen um 7", "in 20 Minuten", "um 18 Uhr", "in einer Stunde"],
    "prozent": ["20", "35", "50", "70", "100"],
    "grad": ["19", "20", "21", "22", "23"],
    "sprache": ["Python", "TypeScript", "Go", "Bash", "PowerShell", "Rust", "SQL", "JavaScript", "C#", "Kotlin"],
    "ding": ["CSV-Datei", "JSON-Datei", "Log-Datei", "YAML-Konfiguration", "Liste von Terminen", "MQTT-Nachricht", "Tabelle"],
    "thema": ["Waermepumpe vs. Gasheizung", "Photovoltaik mit Speicher", "Homeoffice-Regelung", "Umstieg auf Linux im Buero",
              "Elektroauto fuer die Familie", "Glasfaser oder Kabel", "Balkonkraftwerk", "Wechsel der Krankenkasse"],
    "text": ["den folgenden Absatz", "diese E-Mail", "den Artikel unten", "diesen Text", "das Protokoll"],
    "person": ["meine Schwester", "meinen Chef", "den Vermieter", "die Lehrerin", "den Kundendienst"],
    "fehler": ["KeyError: 'model'", "TypeError: object is not iterable", "ConnectionRefusedError", "segmentation fault",
               "null pointer exception", "ECONNRESET", "ModuleNotFoundError: No module named yaml"],
    "n": ["10", "25", "100", "1000"],
}

# Vorlagen je Rolle: (Vorlage, Anzahl Varianten). Platzhalter in {geschweiften Klammern}.
TEMPLATES = {
    "assist": [
        ("Schalte das {geraet} im {raum} an.", 6), ("Mach das {geraet} im {raum} aus.", 6), ("{geraet} im {raum} auf {prozent} Prozent dimmen.", 6),
        ("Stell die Heizung im {raum} auf {grad} Grad.", 5), ("Wie warm ist es im {raum}?", 5), ("Ist das Garagentor offen?", 2),
        ("Mach das Garagentor zu.", 2), ("Stell einen Timer auf {zeit}.", 5), ("Wecke mich {zeit}.", 4), ("Erinnere mich {zeit} an den Muell.", 4),
        ("Setz Milch auf die Einkaufsliste.", 2), ("Fuege Brot und Butter zur Einkaufsliste hinzu.", 2), ("Wie spaet ist es?", 1),
        ("Wie wird das Wetter morgen?", 2), ("Regnet es heute noch?", 1), ("Rolllaeden im {raum} runter.", 5), ("Alle Lichter aus.", 1),
        ("Gute Nacht.", 1), ("Szene Gemuetlich im {raum}.", 4), ("Spiel Musik im {raum}.", 4), ("Lauter.", 1), ("Leiser bitte.", 1),
        ("Starte den Staubsauger.", 1), ("Schick den Maeher nach Hause.", 1), ("Wer ist zuhause?", 1), ("Ist die Waschmaschine fertig?", 1),
        ("Turn on the {geraet} in the {raum}.", 5), ("Turn off all lights downstairs.", 1), ("Set the thermostat to {grad} degrees.", 4),
        ("What's the temperature outside?", 1), ("Lock the front door.", 1), ("Dim the {raum} lights to {prozent} percent.", 4),
        ("Mach die {geraet} an", 4), ("Licht an", 1), ("Licht aus", 1), ("Heizung hoeher", 1), ("Fenster im {raum} offen?", 3),
        ("Kaffee kochen", 1), ("Wieviel Strom brauchen wir gerade?", 1), ("Schalte den Fernseher ein und mach das Licht gemuetlich.", 1),
    ],
    "code": [
        ("Schreib eine {sprache}-Funktion, die eine {ding} einliest und die Zeilen zaehlt.", 8),
        ("Warum bekomme ich hier {fehler}? Code:\n```\nfor row in data['rows']:\n    print(row.name)\n```", 6),
        ("Refactor diese Funktion in {sprache}, sie ist zu lang und hat drei verschachtelte Schleifen.", 6),
        ("Schreib Unit-Tests fuer den folgenden {sprache}-Code mit pytest.", 5), ("Erklaere diesen Regex: ^(?:[a-z0-9]+\\.)*[a-z]{2,}$", 1),
        ("Implementiere in {sprache} einen LRU-Cache mit maximal {n} Eintraegen.", 6), ("Wandle dieses Bash-Skript in {sprache} um.", 5),
        ("Wie parse ich eine {ding} in {sprache} ohne externe Bibliothek?", 6), ("Schreib ein Dockerfile fuer eine Flask-App mit gunicorn.", 1),
        ("Erstelle eine SQL-Abfrage, die die {n} groessten Kunden nach Umsatz im letzten Quartal liefert.", 3),
        ("Fix the bug: the function returns None instead of the list when the input is empty.", 1),
        ("Write a {sprache} script that watches a folder and uploads new files via SFTP.", 5),
        ("Add type hints and docstrings to this {sprache} module.", 4), ("Review this pull request for race conditions:\n```\nasync def handler(): ...\n```", 1),
        ("Convert this YAML to JSON schema and validate it in {sprache}.", 4), ("git rebase hat einen Konflikt in {ding}, wie loese ich das?", 3),
        ("Schreib eine Home-Assistant-Automation in YAML, die bei Bewegung im {raum} das Licht fuer {zeit} einschaltet.", 6),
        ("Node-RED Function-Node: msg.payload ist ein Array von Objekten, gib nur die mit status == 'open' zurueck.", 1),
        ("Optimiere diese {sprache}-Schleife, sie braucht bei {n}000 Elementen 40 Sekunden.", 5),
        ("Was macht `awk -F, '{{print $3}}' datei.csv | sort | uniq -c`?", 1), ("Schreib ein PowerShell-Skript, das alle Dienste mit Startart Automatisch listet, die nicht laufen.", 1),
        ("Implement a REST endpoint in {sprache} that returns paginated results.", 4), ("Warum wirft mein aiohttp-Server 'Cannot write to closing transport' beim Herunterfahren?", 1),
        ("Schreibe einen Traceback-Parser in {sprache}, der die letzte Exception-Zeile extrahiert.", 3),
        ("def fib(n): return fib(n-1)+fib(n-2)  # warum StackOverflow bei n=5000?", 1),
        ("Generate a regex that matches Swiss phone numbers with or without +41.", 1), ("Mach aus dieser Funktion eine Klasse mit sauberer Schnittstelle ({sprache}).", 4),
    ],
    "gross": [
        ("Analysiere ausfuehrlich die Vor- und Nachteile von {thema} fuer ein Einfamilienhaus in der Schweiz, mit Kosten ueber 15 Jahre, Risiken und einer Empfehlung.", 8),
        ("Erstelle ein detailliertes Konzept fuer {thema}: Ausgangslage, Optionen, Bewertungskriterien, Entscheidungsmatrix, Umsetzungsplan in Phasen.", 8),
        ("Vergleiche schrittweise drei Architekturen fuer ein Smart-Home-Backend (monolithisch, Microservices, ereignisgesteuert) und begruende, welche fuer einen Haushalt mit 300 Geraeten passt.", 1),
        ("Schreib ein Gutachten von etwa 1500 Woertern zu {thema}, mit Quellenlage, Gegenargumenten und Fazit.", 6),
        ("Plane eine Roadmap ueber 12 Monate fuer die Migration unserer Hausautomation auf ein neues System. Beruecksichtige Abhaengigkeiten, Risiken, Rueckfallwege und Meilensteine.", 1),
        ("Leite Schritt fuer Schritt her, warum eine Waermepumpe bei Aussentemperaturen unter -7 Grad an Effizienz verliert, und rechne ein Beispiel mit Zahlen durch.", 1),
        ("Write an in-depth comparison of {thema}: total cost of ownership, failure modes, regulatory constraints in Switzerland, and a recommendation with confidence levels.", 6),
        ("Entwirf eine Strategie fuer die naechsten drei Jahre: wie sollte ein kleiner Verein seine IT modernisieren? Betrachte Budget, Freiwilligenarbeit, Datenschutz und Nachfolge.", 1),
        ("Fasse den folgenden 40-seitigen Bericht zusammen und arbeite die fuenf wichtigsten Thesen heraus; bewerte jede These kritisch:\n" + "Lorem ipsum dolor sit amet. " * 60, 1),
        ("Erklaere mir ausfuehrlich und mit Beispielen die Unterschiede zwischen Transformer-Encodern und -Decodern, wann welche Architektur besser ist, und was das fuer Klassifikationsaufgaben bedeutet.", 1),
        ("Beurteile Pro und Contra, ob wir {thema} jetzt oder in zwei Jahren angehen sollten. Beruecksichtige Foerderung, Preisentwicklung und Lebensdauer.", 6),
        ("Schreib einen mehrstufigen Plan mit Abwaegungen, wie ein Homelab ohne Cloud-Abhaengigkeit hochverfuegbar wird: Stromausfall, Hardwaredefekt, Backup, Wiederanlauf.", 1),
        ("Analyze this incident timeline thoroughly and produce a blameless postmortem with root cause, contributing factors and a prioritized action list:\n" + "12:01 alert fired; 12:03 on-call paged; " * 20, 1),
        ("Recherchiere und vergleiche detailliert die Datenschutzlage bei {thema}, inklusive DSGVO/DSG-Bewertung.", 5),
        ("Erstelle eine umfassende Entscheidungsvorlage fuer den Vorstand zu {thema} (Executive Summary, Analyse, Optionen, Empfehlung, Anhang).", 6),
        ("Prove or refute: a router that picks the warm node first always minimizes mean latency. Give a formal argument and counterexamples.", 1),
        ("Give me a thorough analysis of {thema} for a Swiss household: costs over 15 years, risks, alternatives and a clear recommendation.", 6),
        ("Draft a detailed concept for {thema}: current situation, options, evaluation criteria, decision matrix and a phased implementation plan.", 6),
        ("Write a long, well-structured report on {thema} with pros and cons, counterarguments and a conclusion; about 1500 words.", 5),
        ("Plan a 12-month roadmap for {thema}, including dependencies, risks, fallback paths and milestones.", 5),
        ("Walk me through, step by step and with numbers, why {thema} pays off or not over ten years.", 5),
    ],
    "standard": [
        ("Fasse {text} in drei Saetzen zusammen.", 5), ("Uebersetze {text} ins Englische.", 5), ("Uebersetze ins Franzoesische: Guten Morgen, wie geht es dir?", 1),
        ("Korrigiere Rechtschreibung und Grammatik in {text}.", 5), ("Formuliere eine hoefliche Absage an {person}.", 5),
        ("Schreib eine kurze E-Mail an {person}: Termin am Freitag verschieben.", 5), ("Was ist der Unterschied zwischen Wetter und Klima?", 1),
        ("Erklaere in zwei Saetzen, was ein Router macht.", 1), ("Gib mir fuenf Ideen fuer ein Geburtstagsgeschenk fuer {person}.", 5),
        ("Wer war Niklaus Wirth?", 1), ("Nenne drei Vorteile von Sauerteigbrot.", 1), ("Wie viele Kalorien hat eine Banane?", 1),
        ("Summarize the following text in one paragraph.", 1), ("Rewrite this sentence to sound more formal.", 1), ("Translate to German: The meeting is postponed to Monday.", 1),
        ("Mach aus diesen Stichworten einen Fliesstext: Zug, verspaetet, Anschluss verpasst, Hotel gebucht.", 1),
        ("Bewerte die folgenden Log-Zeilen aus Home Assistant und sag in einem Satz, ob etwas Ernstes dabei ist:\nWARNING shadow entity not found\nERROR Setup of zha failed", 1),
        ("Was bedeutet der Fehlercode E24 bei einer Bosch-Spuelmaschine?", 1), ("Schreib einen Vierzeiler ueber den Herbst.", 1),
        ("Welche Zutaten brauche ich fuer Zuercher Geschnetzeltes?", 1), ("Erklaere einem Kind, warum der Himmel blau ist.", 1),
        ("Liste die Hauptstaedte der Nachbarlaender der Schweiz.", 1), ("Formuliere diese Aussage neutraler: Das Update war eine Katastrophe.", 1),
        ("Was ist der Plural von Status?", 1), ("Gib mir eine Packliste fuer ein Wochenende in den Bergen.", 1),
        ("Ordne diese Aufgaben nach Dringlichkeit: Steuererklaerung, Zahnarzt, Geschenk kaufen, Auto waschen.", 1),
        ("Schreib eine Nachricht an die Familiengruppe: Abendessen um 19 Uhr, bitte puenktlich.", 1),
        ("Kannst du mir kurz erklaeren, was MQTT ist?", 1), ("Wie heisst 'Rolladen' korrekt geschrieben?", 1),
        ("Nenne mir ein Synonym fuer 'ausfuehrlich'.", 1), ("Wie lange muss ein Ei kochen, damit es weich ist?", 1),
        ("How many calories are in {ding}?", 3), ("What is the capital of Austria?", 1), ("Give me three gift ideas for {person}.", 4),
        ("Write a short email to {person} moving Friday's meeting to Monday.", 4), ("Explain in two sentences what a heat pump does.", 1),
        ("Proofread {text} and fix grammar.", 3), ("Suggest a name for a grey kitten.", 1), ("How long should I boil an egg for a soft yolk?", 1),
    ],
}


def expand(template: str, k: int, rng: random.Random) -> list[str]:
    """k Auspraegungen einer Vorlage: Platzhalter zufaellig (ohne Wiederholung, soweit moeglich) fuellen."""
    keys = [k_ for k_ in FILL if "{" + k_ + "}" in template]
    if not keys:
        return [template]
    combos = list(itertools.product(*[FILL[k_] for k_ in keys]))
    rng.shuffle(combos)
    out = []
    for combo in combos[:k]:
        s = template
        for key, val in zip(keys, combo, strict=True):
            s = s.replace("{" + key + "}", val)
        out.append(s)
    return out


def build(seed: int) -> list[dict]:
    rng = random.Random(seed)
    rows = []
    for role, templates in TEMPLATES.items():
        for tpl, k in templates:
            group = hashlib.sha1(f"{role}|{tpl}".encode()).hexdigest()[:10]
            variants = 4 if role in ("standard", "gross") else 2   # die beiden Rollen haben weniger Vorlagen mit Platzhaltern
            for text in expand(tpl, k * 2, rng):
                seen = set()
                for variant in range(variants):   # Original + Oberflaechenvarianten je Auspraegung
                    ctx = text if variant == 0 else augment(text, rng)
                    if ctx in seen:
                        continue
                    seen.add(ctx)
                    rows.append({"context": ctx, "options": OPTIONS, "label": OPTIONS.index(role), "role": role, "group": group, "source": "template"})
    return rows


def load_capture(path: str) -> list[dict]:
    """Echte Client-Labels aus dem Router (decision capture, anonymisiert). Pseudo-Labels der Engines bleiben draussen."""
    out = []
    for line in open(path, encoding="utf-8"):
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("label_source") != "client" or r["options"] != OPTIONS:
            continue
        out.append({"context": r["context"], "options": OPTIONS, "label": r["label"], "role": OPTIONS[r["label"]], "group": "cap-" + r["group"], "source": "capture"})
    return out


def split_by_group(rows: list[dict], val: float, test: float, seed: int):
    """Gruppen (Vorlagen) je Rolle getrennt auf die Splits verteilen, damit jede Rolle in jedem Split vorkommt und keine
    Vorlage zwei Splits beruehrt."""
    rng = random.Random(seed)
    parts = {"train": [], "validation": [], "test": []}
    for role in sorted({r["role"] for r in rows}):
        groups = sorted({r["group"] for r in rows if r["role"] == role})
        rng.shuffle(groups)
        n = len(groups)
        n_test, n_val = max(1, round(n * test)), max(1, round(n * val))
        test_g, val_g = set(groups[:n_test]), set(groups[n_test:n_test + n_val])
        for r in rows:
            if r["role"] != role:
                continue
            parts["test" if r["group"] in test_g else "validation" if r["group"] in val_g else "train"].append(r)
    return parts


PREFIX = ["", "", "", "Bitte ", "Kannst du ", "Hey, ", "Ok, ", "Kurze Frage: ", "Please ", "Hi! "]
SUFFIX = ["", "", "", " Danke.", " bitte", "!", " Merci.", " Thanks."]


def augment(text: str, rng: random.Random) -> str:
    """Kleine Oberflaechenvarianz (Anrede, Dank), wie sie im Sprach- und Chat-Alltag vorkommt; Bedeutung bleibt."""
    pre, suf = rng.choice(PREFIX), rng.choice(SUFFIX)
    if pre and text[:1].isupper() and not text.startswith(("Turn", "Set", "What", "Lock", "Dim", "Write", "Fix", "Add", "Review", "Convert",
                                                           "Implement", "Generate", "Analyze", "Prove", "Summarize", "Rewrite", "Translate")):
        text = text[0].lower() + text[1:]
    return f"{pre}{text}{suf}".strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--val", type=float, default=0.15)
    ap.add_argument("--test", type=float, default=0.15)
    ap.add_argument("--capture", default=None)
    a = ap.parse_args()
    rows = build(a.seed)
    if a.capture and os.path.exists(a.capture):
        rows += load_capture(a.capture)
    parts = split_by_group(rows, a.val, a.test, a.seed)
    os.makedirs(a.out, exist_ok=True)
    summary = {"total": len(rows), "options": OPTIONS, "per_role": {o: sum(1 for r in rows if r["role"] == o) for o in OPTIONS},
               "groups": len({r["group"] for r in rows}), "splits": {}}
    for name, part in parts.items():
        random.Random(a.seed).shuffle(part)
        with open(os.path.join(a.out, f"{name}.jsonl"), "w", encoding="utf-8") as f:
            for r in part:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        summary["splits"][name] = {"n": len(part), "per_role": {o: sum(1 for r in part if r["role"] == o) for o in OPTIONS},
                                   "groups": len({r["group"] for r in part})}
    json.dump(summary, open(os.path.join(a.out, "summary.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print(json.dumps(summary, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
