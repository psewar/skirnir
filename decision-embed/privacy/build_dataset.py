#!/usr/bin/env python3
"""Training and validation data for the privacy head (router privacy.py): voice-assistant requests in German, labelled
personal (1) or neutral (0). Built from templates and slot lists, so the split into train and validation is by TEMPLATE,
not by sentence (a validation sentence never shares a template with a training sentence). The test set is separate and
hand-written (test.jsonl, not generated here), so the reported numbers are about unseen phrasings.

Personal = it carries or asks for something private about a person: health, money, credentials, relationships, law,
contact data, where someone is or when someone is away, appointments, private lists and messages.
Neutral = general knowledge, weather, cooking, units, time, jokes, and the state or control of devices and rooms.
Names are invented and generic; nothing here comes from a real household.

    python build_dataset.py   -> data/train.jsonl, data/validation.jsonl
"""
import itertools
import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
RNG = random.Random(17)

S = {
    "person": ["meine Frau", "mein Mann", "meine Tochter", "mein Sohn", "meine Mutter", "mein Vater", "Anna", "Lukas",
               "meine Schwester", "mein Bruder", "Oma", "Opa", "meine Freundin", "mein Partner", "der Nachbar", "Frau Keller"],
    "name": ["Anna", "Lukas", "Mia", "Noah", "Lea", "Jonas", "Sophie", "Elias", "Frau Keller", "Herr Brunner"],
    "ill": ["Grippe", "Migräne", "Diabetes", "Bluthochdruck", "eine Depression", "Corona", "eine Allergie", "Rückenschmerzen",
            "einen Bandscheibenvorfall", "Fieber"],
    "med": ["Ibuprofen", "das Blutdruckmittel", "die Antibiotika", "das Insulin", "die Schlaftabletten", "die Pille", "Antidepressiva"],
    "doc": ["Zahnarzt", "Hausarzt", "Frauenarzt", "Psychiater", "Kinderarzt", "Physiotherapeuten", "Hautarzt"],
    "money": ["Kontostand", "Lohn", "Gehalt", "Schulden", "Kreditkartenabrechnung", "Steuererklärung", "Hypothek", "Erbe"],
    "secret": ["das WLAN-Passwort", "den PIN der Bankkarte", "das Passwort fürs E-Mail", "die Zugangsdaten fürs Bankkonto",
               "den Code der Alarmanlage", "das Passwort vom Laptop"],
    "list": ["Einkaufsliste", "Familienaufgaben", "Wunschliste", "Packliste", "To-do-Liste"],
    "place": ["Arbeit", "Schule", "Ferien", "Spital", "Grossmutter", "Training", "Bahnhof"],
    "device": ["das Licht in der Küche", "die Stehlampe", "die Heizung im Bad", "den Fernseher", "die Kaffeemaschine",
               "die Storen im Wohnzimmer", "den Ventilator", "das Radio", "die Weihnachtsbeleuchtung", "den Staubsauger"],
    "room": ["Wohnzimmer", "Bad", "Schlafzimmer", "Büro", "Küche", "Keller", "Hobbyraum", "Garten", "Kinderzimmer"],
    "city": ["Zürich", "Bern", "Basel", "Luzern", "Olten", "Aarau", "Lugano", "Genf"],
    "dish": ["Risotto", "Rösti", "Zopf", "Lasagne", "Älplermagronen", "Birchermüesli", "Gulasch", "Apfelkuchen"],
    "topic": ["die Fotosynthese", "die Relativitätstheorie", "das Matterhorn", "den Gotthardtunnel", "die Römer",
              "schwarze Löcher", "den Klimawandel", "die Schweizer Bundesverfassung", "Vulkane"],
    "unit": ["100 Fahrenheit in Celsius", "5 Meilen in Kilometer", "3 Tassen in Milliliter", "2 Pfund in Kilo",
             "20 Euro in Franken", "1 Liter in Gallonen"],
    "num": ["5", "10", "15", "20", "25", "30", "45"],
    "temp": ["20", "21", "22", "23", "19"],
    "animal": ["ein Elefant", "eine Katze", "ein Pinguin", "eine Biene", "ein Wal", "ein Adler", "eine Schnecke", "ein Murmeltier"],
    "country": ["Kanada", "Japan", "Brasilien", "Norwegen", "Ägypten", "Australien", "Island", "Mexiko"],
    "word": ["Serendipität", "Gemütlichkeit", "Fernweh", "Algorithmus", "Photon", "Föhn", "Allmend", "Zeitgeist"],
    "appliance": ["der Geschirrspüler", "die Waschmaschine", "der Tumbler", "der Backofen", "der Saugroboter", "die Wärmepumpe"],
    "event": ["Ostern", "Pfingsten", "der Nationalfeiertag", "die nächste Sonnenfinsternis", "der Sommeranfang", "Weihnachten"],
    "music": ["Jazz", "Rockmusik", "Klassik", "Radio SRF 3", "etwas Ruhiges", "die Charts", "Mani Matter"],
}

PERSONAL = [
    "Wann ist mein Termin beim {doc}?", "Erinnere mich, {med} zu nehmen.", "Hat {person} ihre Tabletten genommen?",
    "Was hilft gegen {ill}?  Ich habe das seit einer Woche.", "{person} hat {ill}, was soll ich tun?",
    "Wie hoch ist mein {money}?", "Wann wird der {money} überwiesen?", "Wie viel habe ich diesen Monat ausgegeben?",
    "Sag mir {secret}.", "Wie lautet {secret}?", "Speichere {secret} als 4711.",
    "Was steht auf der {list}?", "Lies mir die {list} vor.", "Was hat {person} auf die {list} geschrieben?",
    "Wo ist {person} gerade?", "Ist {person} schon zu Hause?", "Wann kommt {person} von der {place} zurück?",
    "Ist gerade jemand zu Hause?", "Wer ist alles im Haus?", "Wann sind wir alle in den Ferien weg?",
    "Ruf {name} an.", "Wie ist die Nummer von {name}?", "Wo wohnt {name}?", "Schick {name} eine Nachricht, dass ich später komme.",
    "Was hat {name} mir geschrieben?", "Hat {name} angerufen?",
    "{person} und ich haben Streit, was soll ich machen?", "Ich glaube, {person} betrügt mich.", "Wie sage ich {person}, dass ich mich trennen will?",
    "Wann ist die Gerichtsverhandlung von {person}?", "Muss ich wegen der Busse zur Polizei?", "Was schreibe ich dem Anwalt wegen der Scheidung?",
    "Wie lange ist {person} schon krank?", "{person} ist schwanger, wann ist der Geburtstermin?", "Hat {person} morgen Therapie?",
    "Wann hat {name} Geburtstag?", "Wie alt ist {person}?", "Was wünscht sich {person} zum Geburtstag?",
    "Notiere, dass {person} {ill} hat.", "Wie viel wiegt {person} jetzt?", "Was hat der {doc} letzte Woche gesagt?",
    "Wann muss ich die Steuern bezahlen und wie viel?", "Wie viel Rente bekommt {person}?", "Was verdient {person}?",
    "Ist das Haus abgeschlossen, wenn alle weg sind?", "Wann war {person} zuletzt zu Hause?", "Um wie viel Uhr ist {person} heute gegangen?",
]
NEUTRAL = [
    "Schalte {device} ein.", "Mach {device} aus.", "Wie warm ist es im {room}?", "Stell die Heizung im {room} auf {temp} Grad.",
    "Ist das Licht im {room} noch an?", "Welche Lichter sind gerade an?", "Ist es im {room} wärmer als im Wohnzimmer?",
    "Wie wird das Wetter morgen in {city}?", "Regnet es heute noch?", "Brauche ich heute einen Schirm?",
    "Wie mache ich {dish}?", "Wie lange muss {dish} in den Ofen?", "Was brauche ich für {dish}?",
    "Rechne {unit} um.", "Wie viel sind {unit}?", "Was ist 17 mal 23?",
    "Stell einen Timer auf {num} Minuten.", "Wecke mich in {num} Minuten.", "Wie spät ist es?", "Welcher Tag ist heute?",
    "Erklär mir {topic}.", "Was weisst du über {topic}?", "Wer hat {topic} entdeckt?",
    "Spiel {music}.", "Mach die Musik leiser.", "Was läuft gerade im Radio?",
    "Erzähl mir einen Witz.", "Wie hoch ist der Säntis?", "Wann geht heute die Sonne unter?",
    "Wie viel Strom verbraucht die Wärmepumpe gerade?", "Wie voll ist der Akku vom Mähroboter?", "Ist die Waschmaschine fertig?",
    "Fahr die Storen im {room} runter.", "Starte den Staubsauger.", "Ist das Garagentor zu?",
    "Wie ist die Luftfeuchtigkeit im {room}?", "Wie viel Solarstrom produzieren wir gerade?", "Wann ist der nächste Vollmond?",
    "Wie weit ist es von {city} nach Bern?", "Was ist die Hauptstadt von Kanada?", "Übersetze guten Morgen ins Italienische.",
    "Wie schreibt man Rhythmus?", "Gib mir eine Idee für das Abendessen.", "Welche Zutaten passen zu {dish}?",
    "Wie funktioniert eine Wärmepumpe?", "Warum ist der Himmel blau?", "Wie viele Einwohner hat {city}?",
    "Wie viel wiegt {animal}?", "Wie alt wird {animal}?", "Was frisst {animal}?", "Wie schnell ist {animal}?",
    "Was ist die Hauptstadt von {country}?", "Welche Sprache spricht man in {country}?", "Wie spät ist es jetzt in {country}?",
    "Was bedeutet {word}?", "Woher kommt das Wort {word}?", "Wie buchstabiert man {word}?",
    "Ist {appliance} schon fertig?", "Wie lange läuft {appliance} noch?", "Wie viel Strom braucht {appliance} gerade?",
    "Wann ist {event}?", "Auf welchen Wochentag fällt {event}?", "Sag mir einen Witz über {animal}.",
    "Was ist {num} Prozent von 250?", "Was ist die Wurzel aus {num}?", "Wie viele Tage hat der Februar in einem Schaltjahr?",
    "Erzähl mir etwas über die Geschichte von {city}.", "Was kann man in {city} unternehmen?", "Wie ist die Luftqualität in {city}?",
]


def fill(tpl, n):
    keys = [k for k in S if "{" + k + "}" in tpl]
    combos = list(itertools.product(*[S[k] for k in keys])) if keys else [()]
    RNG.shuffle(combos)
    return [tpl.format(**dict(zip(keys, c, strict=True))).replace("  ", " ") for c in combos[:n]]


def main():
    rows = {"train": [], "validation": []}
    for label, templates in ((1, PERSONAL), (0, NEUTRAL)):
        order = list(range(len(templates)))
        RNG.shuffle(order)
        val = set(order[: len(templates) // 5])   # a fifth of the templates only in validation
        for i, tpl in enumerate(templates):
            split = "validation" if i in val else "train"
            for text in fill(tpl, 12):
                rows[split].append({"text": text, "label": label})
    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    for split, r in rows.items():
        RNG.shuffle(r)
        with open(os.path.join(HERE, "data", f"{split}.jsonl"), "w", encoding="utf-8", newline="\n") as f:
            for x in r:
                f.write(json.dumps(x, ensure_ascii=False) + "\n")
        print(split, len(r), "personal", sum(x["label"] for x in r))


if __name__ == "__main__":
    main()
