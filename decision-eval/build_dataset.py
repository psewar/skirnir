#!/usr/bin/env python3
"""Representative Skirnir dataset for the Decision Engine: requests -> role (standard | large | assist | code).

Source is synthetic templates for typical request types (HA voice commands, Node-RED tasks, log triage, coding via agent
frameworks, long analyses), each with placeholders and variants. Each template is a **group**: all variants of a template
land in the same split (train/validation/test), otherwise evaluation measures memorization instead of understanding (Section 9).
Mixed German and English; the German templates use the standard spelling (ä, ö, ü, ß) and vocabulary that does not
point to a region, and nothing in them refers to a particular country or household.

    python build_dataset.py [--out data] [--seed 7] [--val 0.15] [--test 0.15]
Writes data/{train,validation,test}.jsonl in Jevlike format ({context, options, label}) plus group/role per line and
data/summary.json. Optional --capture <decisions.jsonl>: mix in real, anonymized client labels from the router.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import random

OPTIONS = ["standard", "gross", "assist", "code"]

# Placeholders for the templates. Each value fits every template that uses it (article and case included), and English
# templates take English values: a filled sentence reads like something a person would say.
FILL = {
    "raum": ["im Wohnzimmer", "in der Küche", "im Schlafzimmer", "im Büro", "im Bad", "im Flur", "in der Garage", "im Kinderzimmer",
             "im Esszimmer"],
    "geraet": ["das Licht", "die Deckenlampe", "die Stehlampe", "die Steckdose", "den Ventilator", "den Fernseher", "das Radio",
               "die Kaffeemaschine"],
    "dauer": ["10 Minuten", "eine halbe Stunde", "fünf Minuten", "20 Minuten", "eine Stunde", "90 Sekunden"],
    "wann": ["morgen um 7", "in 20 Minuten", "um 18 Uhr", "in einer Stunde", "heute Abend um neun", "in zehn Minuten"],
    "room": ["living room", "kitchen", "bedroom", "office", "bathroom", "hallway", "garage", "kids' room", "dining room"],
    "device": ["light", "ceiling lamp", "floor lamp", "socket", "fan", "TV", "radio", "coffee machine"],
    "topic": ["a heat pump vs. gas heating", "solar panels with a battery", "a home-office policy", "moving the office to Linux",
              "an electric car for the family", "fibre or cable internet", "a balcony solar kit", "switching health insurers"],
    "recipient": ["my sister", "my boss", "the landlord", "the teacher", "customer service"],
    "doc": ["the following paragraph", "this email", "the article below", "this text", "the minutes"],
    "food": ["a banana", "an apple", "a slice of pizza", "a croissant", "a glass of milk", "a boiled egg", "a portion of fries"],
    "prozent": ["20", "35", "50", "70", "100"],
    "grad": ["19", "20", "21", "22", "23"],
    "sprache": ["Python", "TypeScript", "Go", "Bash", "PowerShell", "Rust", "SQL", "JavaScript", "C#", "Kotlin"],
    "ding": ["CSV-Datei", "JSON-Datei", "Log-Datei", "YAML-Konfiguration", "Liste von Terminen", "MQTT-Nachricht", "Tabelle"],
    "thema": ["Wärmepumpe vs. Gasheizung", "Photovoltaik mit Speicher", "Homeoffice-Regelung", "Umstieg auf Linux im Büro",
              "Elektroauto für die Familie", "Glasfaser oder Kabel", "Balkonkraftwerk", "Wechsel der Krankenkasse"],
    "text": ["den folgenden Absatz", "diese E-Mail", "den Artikel unten", "diesen Text", "das Protokoll"],
    "person": ["meine Schwester", "meinen Chef", "den Vermieter", "die Lehrerin", "den Kundendienst"],
    "fehler": ["KeyError: 'model'", "TypeError: object is not iterable", "ConnectionRefusedError", "segmentation fault",
               "null pointer exception", "ECONNRESET", "ModuleNotFoundError: No module named yaml"],
    "n": ["10", "25", "100", "1000"],
}

# Templates per role: (template, number of variants). Placeholders in {curly braces}.
TEMPLATES = {
    "assist": [
        ("Schalte {geraet} {raum} an.", 6), ("Mach {geraet} {raum} aus.", 6), ("{geraet} {raum} auf {prozent} Prozent dimmen.", 6),
        ("Stell die Heizung {raum} auf {grad} Grad.", 5), ("Wie warm ist es {raum}?", 5), ("Ist das Garagentor offen?", 2),
        ("Mach das Garagentor zu.", 2), ("Stell einen Timer auf {dauer}.", 5), ("Wecke mich {wann}.", 4), ("Erinnere mich {wann} an den Müll.", 4),
        ("Setz Milch auf die Einkaufsliste.", 2), ("Füge Brot und Butter zur Einkaufsliste hinzu.", 2), ("Wie spät ist es?", 1),
        ("Wie wird das Wetter morgen?", 2), ("Regnet es heute noch?", 1), ("Rollläden {raum} runter.", 5), ("Alle Lichter aus.", 1),
        ("Gute Nacht.", 1), ("Szene Gemütlich {raum}.", 4), ("Spiel Musik {raum}.", 4), ("Lauter.", 1), ("Leiser bitte.", 1),
        ("Starte den Staubsauger.", 1), ("Schick den Mäher nach Hause.", 1), ("Wer ist zuhause?", 1), ("Ist die Waschmaschine fertig?", 1),
        ("Turn on the {device} in the {room}.", 5), ("Turn off all lights downstairs.", 1), ("Set the thermostat to {grad} degrees.", 4),
        ("What's the temperature outside?", 1), ("Lock the front door.", 1), ("Dim the {room} lights to {prozent} percent.", 4),
        ("Mach {geraet} an", 4), ("Licht an", 1), ("Licht aus", 1), ("Heizung höher", 1), ("Fenster {raum} offen?", 3),
        ("Kaffee kochen", 1), ("Wie viel Strom brauchen wir gerade?", 1), ("Schalte den Fernseher ein und mach das Licht gemütlich.", 1),
    ],
    "code": [
        ("Schreib eine {sprache}-Funktion, die eine {ding} einliest und die Zeilen zählt.", 8),
        ("Warum bekomme ich hier {fehler}? Code:\n```\nfor row in data['rows']:\n    print(row.name)\n```", 6),
        ("Refactor diese Funktion in {sprache}, sie ist zu lang und hat drei verschachtelte Schleifen.", 6),
        ("Schreib Unit-Tests für den folgenden {sprache}-Code mit pytest.", 5), ("Erkläre diesen Regex: ^(?:[a-z0-9]+\\.)*[a-z]{2,}$", 1),
        ("Implementiere in {sprache} einen LRU-Cache mit maximal {n} Einträgen.", 6), ("Wandle dieses Bash-Skript in {sprache} um.", 5),
        ("Wie parse ich eine {ding} in {sprache} ohne externe Bibliothek?", 6), ("Schreib ein Dockerfile für eine Flask-App mit gunicorn.", 1),
        ("Erstelle eine SQL-Abfrage, die die {n} größten Kunden nach Umsatz im letzten Quartal liefert.", 3),
        ("Fix the bug: the function returns None instead of the list when the input is empty.", 1),
        ("Write a {sprache} script that watches a folder and uploads new files via SFTP.", 5),
        ("Add type hints and docstrings to this {sprache} module.", 4), ("Review this pull request for race conditions:\n```\nasync def handler(): ...\n```", 1),
        ("Convert this YAML to JSON schema and validate it in {sprache}.", 4), ("git rebase hat einen Konflikt in {ding}, wie löse ich das?", 3),
        ("Schreib eine Home-Assistant-Automation in YAML, die bei Bewegung {raum} das Licht für {dauer} einschaltet.", 6),
        ("Node-RED Function-Node: msg.payload ist ein Array von Objekten, gib nur die mit status == 'open' zurück.", 1),
        ("Optimiere diese {sprache}-Schleife, sie braucht bei {n}000 Elementen 40 Sekunden.", 5),
        ("Was macht `awk -F, '{{print $3}}' datei.csv | sort | uniq -c`?", 1), ("Schreib ein PowerShell-Skript, das alle Dienste mit Startart Automatisch listet, die nicht laufen.", 1),
        ("Implement a REST endpoint in {sprache} that returns paginated results.", 4), ("Warum wirft mein aiohttp-Server 'Cannot write to closing transport' beim Herunterfahren?", 1),
        ("Schreibe einen Traceback-Parser in {sprache}, der die letzte Exception-Zeile extrahiert.", 3),
        ("def fib(n): return fib(n-1)+fib(n-2)  # warum StackOverflow bei n=5000?", 1),
        ("Generate a regex that matches phone numbers in E.164 format, with or without the leading +.", 1), ("Mach aus dieser Funktion eine Klasse mit sauberer Schnittstelle ({sprache}).", 4),
    ],
    "gross": [
        ("Analysiere ausführlich die Vor- und Nachteile von {thema} für ein Einfamilienhaus, mit Kosten über 15 Jahre, Risiken und einer Empfehlung.", 8),
        ("Erstelle ein detailliertes Konzept für {thema}: Ausgangslage, Optionen, Bewertungskriterien, Entscheidungsmatrix, Umsetzungsplan in Phasen.", 8),
        ("Vergleiche schrittweise drei Architekturen für ein Smart-Home-Backend (monolithisch, Microservices, ereignisgesteuert) und begründe, welche für einen Haushalt mit einigen hundert Geräten passt.", 1),
        ("Schreib ein Gutachten von etwa 1500 Wörtern zu {thema}, mit Quellenlage, Gegenargumenten und Fazit.", 6),
        ("Plane eine Roadmap über 12 Monate für die Migration unserer Hausautomation auf ein neues System. Berücksichtige Abhängigkeiten, Risiken, Rückfallwege und Meilensteine.", 1),
        ("Leite Schritt für Schritt her, warum eine Wärmepumpe bei Außentemperaturen unter -7 Grad an Effizienz verliert, und rechne ein Beispiel mit Zahlen durch.", 1),
        ("Write an in-depth comparison of {topic}: total cost of ownership, failure modes, regulatory constraints, and a recommendation with confidence levels.", 6),
        ("Entwirf eine Strategie für die nächsten drei Jahre: wie sollte ein kleiner Verein seine IT modernisieren? Betrachte Budget, Freiwilligenarbeit, Datenschutz und Nachfolge.", 1),
        ("Fasse den folgenden 40-seitigen Bericht zusammen und arbeite die fünf wichtigsten Thesen heraus; bewerte jede These kritisch:\n" + "Lorem ipsum dolor sit amet. " * 60, 1),
        ("Erkläre mir ausführlich und mit Beispielen die Unterschiede zwischen Transformer-Encodern und -Decodern, wann welche Architektur besser ist, und was das für Klassifikationsaufgaben bedeutet.", 1),
        ("Beurteile Pro und Contra, ob wir {thema} jetzt oder in zwei Jahren angehen sollten. Berücksichtige Förderung, Preisentwicklung und Lebensdauer.", 6),
        ("Schreib einen mehrstufigen Plan mit Abwägungen, wie ein Homelab ohne Cloud-Abhängigkeit hochverfügbar wird: Stromausfall, Hardwaredefekt, Backup, Wiederanlauf.", 1),
        ("Analyze this incident timeline thoroughly and produce a blameless postmortem with root cause, contributing factors and a prioritized action list:\n" + "12:01 alert fired; 12:03 on-call paged; " * 20, 1),
        ("Recherchiere und vergleiche detailliert die Datenschutzlage bei {thema}, inklusive DSGVO-Bewertung.", 5),
        ("Erstelle eine umfassende Entscheidungsvorlage für den Vorstand zu {thema} (Kurzfassung, Analyse, Optionen, Empfehlung, Anhang).", 6),
        ("Prove or refute: a router that picks the warm node first always minimizes mean latency. Give a formal argument and counterexamples.", 1),
        ("Give me a thorough analysis of {topic} for a family household: costs over 15 years, risks, alternatives and a clear recommendation.", 6),
        ("Draft a detailed concept for {topic}: current situation, options, evaluation criteria, decision matrix and a phased implementation plan.", 6),
        ("Write a long, well-structured report on {topic} with pros and cons, counterarguments and a conclusion; about 1500 words.", 5),
        ("Plan a 12-month roadmap for {topic}, including dependencies, risks, fallback paths and milestones.", 5),
        ("Walk me through, step by step and with numbers, why {topic} pays off or not over ten years.", 5),
    ],
    "standard": [
        ("Fasse {text} in drei Sätzen zusammen.", 5), ("Übersetze {text} ins Englische.", 5), ("Übersetze ins Französische: Guten Morgen, wie geht es dir?", 1),
        ("Korrigiere Rechtschreibung und Grammatik für {text}.", 5), ("Formuliere eine höfliche Absage an {person}.", 5),
        ("Schreib eine kurze E-Mail an {person}: Termin am Freitag verschieben.", 5), ("Was ist der Unterschied zwischen Wetter und Klima?", 1),
        ("Erkläre in zwei Sätzen, was ein Router macht.", 1), ("Gib mir fünf Ideen für ein Geburtstagsgeschenk für {person}.", 5),
        ("Wer war Ada Lovelace?", 1), ("Nenne drei Vorteile von Sauerteigbrot.", 1), ("Wie viele Kalorien hat eine Banane?", 1),
        ("Summarize the following text in one paragraph.", 1), ("Rewrite this sentence to sound more formal.", 1), ("Translate to German: The meeting is postponed to Monday.", 1),
        ("Mach aus diesen Stichworten einen Fließtext: Zug, verspätet, Anschluss verpasst, Hotel gebucht.", 1),
        ("Bewerte die folgenden Log-Zeilen aus Home Assistant und sag in einem Satz, ob etwas Ernstes dabei ist:\nWARNING shadow entity not found\nERROR Setup of zha failed", 1),
        ("Was bedeutet der Fehlercode E24 bei einer Spülmaschine?", 1), ("Schreib einen Vierzeiler über den Herbst.", 1),
        ("Welche Zutaten brauche ich für eine Gemüselasagne?", 1), ("Erkläre einem Kind, warum der Himmel blau ist.", 1),
        ("Liste die Hauptstädte der skandinavischen Länder.", 1), ("Formuliere diese Aussage neutraler: Das Update war eine Katastrophe.", 1),
        ("Was ist der Plural von Status?", 1), ("Gib mir eine Packliste für ein Wochenende in den Bergen.", 1),
        ("Ordne diese Aufgaben nach Dringlichkeit: Steuererklärung, Zahnarzt, Geschenk kaufen, Auto waschen.", 1),
        ("Schreib eine Nachricht an die Familiengruppe: Abendessen um 19 Uhr, bitte pünktlich.", 1),
        ("Kannst du mir kurz erklären, was MQTT ist?", 1), ("Wie heißt 'Rolladen' korrekt geschrieben?", 1),
        ("Nenne mir ein Synonym für 'ausführlich'.", 1), ("Wie lange muss ein Ei kochen, damit es weich ist?", 1),
        ("How many calories are in {food}?", 3), ("What is the capital of Austria?", 1), ("Give me three gift ideas for {recipient}.", 4),
        ("Write a short email to {recipient} moving Friday's meeting to Monday.", 4), ("Explain in two sentences what a heat pump does.", 1),
        ("Proofread {doc} and fix grammar.", 3), ("Suggest a name for a grey kitten.", 1), ("How long should I boil an egg for a soft yolk?", 1),
    ],
}


def expand(template: str, k: int, rng: random.Random) -> list[str]:
    """k variants of a template: fill placeholders randomly (without repetition, where possible)."""
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
        out.append(s[:1].upper() + s[1:] if template.startswith("{") else s)   # "Die Stehlampe ...", not "die Stehlampe ..."
    return out


def build(seed: int) -> list[dict]:
    rng = random.Random(seed)
    rows = []
    for role, templates in TEMPLATES.items():
        for tpl, k in templates:
            group = hashlib.sha1(f"{role}|{tpl}".encode()).hexdigest()[:10]
            variants = 4 if role in ("standard", "gross") else 2   # the two roles have fewer templates with placeholders
            for text in expand(tpl, k * 2, rng):
                seen = set()
                for variant in range(variants):   # the filled sentence plus surface variants of it
                    ctx = text if variant == 0 else augment(text, rng)
                    if ctx in seen:
                        continue
                    seen.add(ctx)
                    rows.append({"context": ctx, "options": OPTIONS, "label": OPTIONS.index(role), "role": role, "group": group, "source": "template"})
    return rows


def load_capture(path: str) -> list[dict]:
    """Real client labels from the router (decision capture, anonymized). Pseudo-labels from the engines are excluded."""
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
    """Distribute groups (templates) per role across splits separately, so each role appears in every split and no
    template touches two splits."""
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


# Surface variants in the language of the sentence. A prefix in LOWER_AFTER continues the sentence (the first letter is
# lowered); the others stand before it. "!" replaces a closing full stop, "bitte"/"please" goes before the closing mark.
PREFIX = {"de": ["", "", "", "Bitte ", "Hey, ", "Ok, ", "Kurze Frage: ", "Hallo! "],
          "en": ["", "", "", "Please ", "Hey, ", "Ok, ", "Quick question: ", "Hi! "]}
SUFFIX = {"de": ["", "", "", " Danke.", " bitte", "!", " Vielen Dank."],
          "en": ["", "", "", " Thanks.", " please", "!", " Thank you."]}
LOWER_AFTER = ("Bitte ", "Please ", "Hey, ", "Ok, ")
POLITE = ("Bitte ", "Please ")                    # not before a question, and not twice ("Bitte ... bitte")
KEEP_CASE = ("Rollläden", "Fenster", "Szene", "Heizung", "Licht", "Kaffee", "Node-RED")   # German nouns at the start
EN_WORDS = {"the", "a", "to", "of", "and", "for", "with", "is", "this", "my", "me", "what", "how", "that", "it", "on", "or",
            "are", "should", "i", "do", "all", "when", "into", "about", "give", "write", "why", "off", "your"}
DE_WORDS = {"der", "die", "das", "und", "ist", "mir", "mich", "ich", "du", "im", "auf", "für", "ein", "eine", "einen", "wie",
            "was", "nicht", "mit", "zu", "den", "dem", "des", "von", "bei", "warum", "diese", "dieses", "diesen", "es", "wir",
            "uns", "noch", "aus", "hier", "nur", "macht", "schreib", "mach"}


def language(text: str) -> str:
    """'en' or 'de' by counting common function words; German when it is a tie (short voice commands)."""
    words = [w.strip(".,:;!?()'`\"").lower() for w in text.split()]
    return "en" if sum(w in EN_WORDS for w in words) > sum(w in DE_WORDS for w in words) else "de"


def augment(text: str, rng: random.Random) -> str:
    """Minor surface variation (greetings, thanks), as seen in voice and chat daily life; meaning remains."""
    lang = language(text)
    pre, suf = rng.choice(PREFIX[lang]), rng.choice(SUFFIX[lang])
    if pre in POLITE and text.endswith("?"):
        pre = ""
    if pre in POLITE and suf in (" bitte", " please"):
        suf = ""
    if pre in LOWER_AFTER and text[:1].isupper() and not text.startswith(KEEP_CASE):
        text = text[0].lower() + text[1:]
    if suf == "!":
        text = text[:-1] + "!" if text.endswith(".") else text if text.endswith(("?", "!", "`")) or "\n" in text else text + "!"
        suf = ""
    elif suf in (" bitte", " please") and text[-1:] in ".?!":
        text, suf = text[:-1] + suf + text[-1], ""
    elif suf[1:2].isupper() and text[-1:] not in ".?!`":
        text += "."   # "Licht an. Danke." rather than "Licht an Danke."
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
        with open(os.path.join(a.out, f"{name}.jsonl"), "w", encoding="utf-8", newline="\n") as f:   # same bytes on every OS
            for r in part:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        summary["splits"][name] = {"n": len(part), "per_role": {o: sum(1 for r in part if r["role"] == o) for o in OPTIONS},
                                   "groups": len({r["group"] for r in part})}
    with open(os.path.join(a.out, "summary.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(summary, f, indent=1, ensure_ascii=False)
    print(json.dumps(summary, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
