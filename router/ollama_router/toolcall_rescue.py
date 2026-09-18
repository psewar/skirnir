"""Holt Tool-Calls zurueck, die das Modell im falschen Dialekt geschrieben hat.

Ollama beschraenkt den `tools`-Pfad nicht: es erzeugt frei und parst hinterher.
Faellt der Parse durch, landet der Aufruf als Text in `message.content` und ist fuer
den Client verloren - stumm, mit `done_reason: "stop"` und ohne jede Fehlermeldung
(ollama/ollama#17274). llama.cpp erzwingt an dieser Stelle eine Grammatik und kann
den Fall gar nicht erst haben; Ollama kann es nicht, also raeumen wir hinterher auf.

Gemessen am 2026-09-15 (Messreihe local-toolcall-context): `qwen3-coder:30b`
verliert oberhalb ~12k Prompt-Token **80 %** seiner Aufrufe auf diesem Weg - obwohl
Werkzeugname und Argumente jedes Mal korrekt waren. `gemma4:26b`, `glm-4.7-flash`,
`granite4.2:8b` und `qwen3.6:35b-a3b` waren in derselben Messung fehlerfrei. Die Stufe
ist also nicht fuer den Normalfall da, sondern fuer den einen Ausreisser - und fuer den
naechsten, den wir noch nicht kennen.

GRUNDSATZ: lieber ein verlorener Aufruf als ein erfundener. Ein faelschlich geretteter
Call fuehrt beim Client ein Werkzeug aus, das niemand wollte - im Haus heisst das ein
Rollladen, ein Schloss oder ein Shell-Kommando. Darum greift die Stufe nur unter engen
Bedingungen und laesst im Zweifel alles unveraendert.
"""
import json
import re

# Der Aufruf muss den ueberwiegenden Teil der Antwort ausmachen. Ein Modell, das erst
# einen Absatz schreibt und dann etwas Call-aehnliches erwaehnt, wollte keinen Aufruf.
MIN_ANTEIL = 0.8

# Puffergrenze im Streaming: laenger als das ist kein Werkzeugaufruf mehr, sondern Prosa.
MAX_PUFFER = 8192

# qwen3-coder-Dialekt: <function=name><parameter=key>wert</parameter></function>
RE_XML_FN = re.compile(r"<function\s*=\s*([A-Za-z_][\w.-]*)\s*>(.*?)</function\s*>", re.I | re.S)
RE_XML_PARAM = re.compile(r"<parameter\s*=\s*([A-Za-z_][\w.-]*)\s*>(.*?)</parameter\s*>", re.I | re.S)
# Maentel, die verschiedene Familien um den Aufruf legen. Sie zaehlen nicht als Prosa,
# sonst faellt ein sauberer Aufruf nur wegen seiner Verpackung durch die Anteilsregel.
RE_MANTEL = re.compile(r"</?\s*(tool_call|tool_calls|tools|function_call)\s*>|"
                       r"<\|/?\s*(tool_call|python_tag)\s*\|>|\[/?TOOL_CALLS?\]", re.I)
# Woran wir im Stream erkennen, dass da ein Aufruf KOMMEN koennte (Praefix, nicht Vollbild)
RE_KANDIDAT = re.compile(r"^\s*(<\s*t|<\s*f|\{|\[TOOL|<\|tool)", re.I)


def werkzeugnamen(tools):
    """Die Namen, die der Client ueberhaupt angeboten hat. Alles andere ist erfunden."""
    out = set()
    for t in tools or ():
        f = (t or {}).get("function") or {}
        if f.get("name"):
            out.add(str(f["name"]))
    return out


def _schema(tools, name):
    for t in tools or ():
        f = (t or {}).get("function") or {}
        if f.get("name") == name:
            return ((f.get("parameters") or {}).get("properties")) or {}
    return {}


def _passend(wert, typ):
    """XML liefert alles als Text. Ein `number`-Parameter als String laesst manche
    Clients auflaufen, also einmal richtig typisieren - aber nur, wenn es aufgeht."""
    if typ in ("number", "integer"):
        try:
            return int(wert) if typ == "integer" else float(wert)
        except (TypeError, ValueError):
            return wert
    if typ == "boolean":
        if str(wert).strip().lower() in ("true", "ja", "1"):
            return True
        if str(wert).strip().lower() in ("false", "nein", "0"):
            return False
    return wert


def extract(text, erlaubt, tools=None):
    """(name, argumente) aus einem als Text gelandeten Aufruf - oder (None, None).

    `erlaubt` ist die Menge der vom Client angebotenen Werkzeugnamen. Ein Treffer
    ausserhalb davon wird verworfen: das waere ein halluzinierter Aufruf, und den
    wollen wir nicht durchreichen, nur weil er gut aussieht.
    """
    if not text or not erlaubt:
        return None, None
    roh = text.strip()
    if not roh:
        return None, None
    # Der Massstab fuer "war die Antwort nichts als ein Aufruf" ist der Text OHNE Mantel.
    nutz = max(len(RE_MANTEL.sub("", roh).strip()), 1)

    m = RE_XML_FN.search(roh)
    if m and m.group(1) in erlaubt:
        if len(m.group(0)) < MIN_ANTEIL * nutz:
            return None, None
        props = _schema(tools, m.group(1))
        args = {}
        for k, v in RE_XML_PARAM.findall(m.group(2)):
            typ = ((props.get(k) or {}).get("type"))
            args[k] = _passend(v.strip(), typ)
        return m.group(1), args

    # JSON mit echtem Parser statt Regex: Argumente sind verschachtelt, und eine
    # Regex, die auf die erste schliessende Klammer laeuft, schneidet sie ab.
    dec = json.JSONDecoder()
    i = roh.find("{")
    while i >= 0:
        try:
            o, ende = dec.raw_decode(roh, i)
        except ValueError:
            i = roh.find("{", i + 1)
            continue
        if isinstance(o, dict) and o.get("name") in erlaubt:
            if (ende - i) < MIN_ANTEIL * nutz:
                return None, None
            args = o.get("arguments")
            if args is None:
                args = o.get("parameters")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            return o["name"], (args if isinstance(args, dict) else {})
        i = roh.find("{", i + 1)

    return None, None


def rescue_message(msg, tools):
    """Hebt einen im Text gestrandeten Aufruf in `message.tool_calls`.

    Gibt den geretteten Namen zurueck (fuer Metrik und Log) oder None.
    Ruehrt die Nachricht nur an, wenn sie WIRKLICH keine tool_calls hat - eine
    Antwort, die beides enthaelt, ist schon in Ordnung und geht uns nichts an.
    """
    if not isinstance(msg, dict) or msg.get("tool_calls"):
        return None
    erlaubt = werkzeugnamen(tools)
    if not erlaubt:
        return None
    name, args = extract(msg.get("content") or "", erlaubt, tools)
    if not name:
        return None
    msg["tool_calls"] = [{"function": {"name": name, "arguments": args or {}}}]
    msg["content"] = ""
    return name


def verdacht(text):
    """Sah nach einem Aufruf aus, liess sich aber nicht herausloesen.

    Das ist der Fall, den diese Stufe NICHT heilt - ein Dialekt, den wir nicht kennen,
    oder ein Werkzeug, das gar nicht angeboten war. Er bleibt fuer den Client verloren,
    soll aber nicht laenger stumm bleiben: erst wenn dieser Zaehler sich bewegt, lohnt
    die naechste Ausbaustufe (erzwungenes Schema als zweiter Versuch). Vorher waere sie
    Code gegen eine Vermutung.
    """
    if not text:
        return False
    roh = text.strip()
    return bool(roh) and bool(RE_KANDIDAT.match(roh)) and ("function" in roh or "name" in roh
                                                           or "tool" in roh.lower())


def aktiv_fuer(body):
    """Wann die Stufe ueberhaupt zustaendig ist.

    Nicht bei `format`: dort beschraenkt Ollama die Ausgabe bereits per Grammatik,
    eine JSON-Antwort ist dann das gewollte Ergebnis und kein verungluckter Aufruf.
    """
    return bool((body or {}).get("tools")) and not (body or {}).get("format")


class StreamRescuer:
    """Dasselbe fuer den Stream - wo es schwieriger ist, weil der Text beim Client
    ist, bevor man weiss, dass er ein Aufruf war.

    Deshalb: solange der bisherige Text noch ein Aufruf WERDEN kann, halten wir ihn
    zurueck. Sobald klar ist, dass es Prosa ist, geben wir das Gehaltene am Stueck
    heraus und mischen uns nie wieder ein. Verzoegert wird also nur, was ohnehin
    verloren gewesen waere.
    """

    def __init__(self, tools):
        self.erlaubt = werkzeugnamen(tools)
        self.tools = tools
        self.puffer = ""
        self.haltend = bool(self.erlaubt)   # ohne Werkzeuge gar nicht erst anfangen
        self.gerettet = None
        self.verloren = False   # sah aus wie ein Aufruf, liess sich aber nicht herausloesen

    def chunk(self, j):
        """Nimmt einen Upstream-Chunk, gibt die Chunks zurueck, die raus duerfen."""
        if not self.haltend:
            return [j]
        msg = j.get("message") or {}
        if msg.get("tool_calls"):        # Ollama hat es doch selbst geschafft
            self.haltend = False
            return self._freigeben(j)
        stueck = msg.get("content") or ""
        if stueck:
            # Erst pruefen, DANN puffern - sonst zaehlt _freigeben dieses Stueck doppelt.
            kuenftig = self.puffer + stueck
            if not RE_KANDIDAT.match(kuenftig) or len(kuenftig) > MAX_PUFFER:
                self.haltend = False   # doch nur Prosa: alles Gehaltene nachreichen
                return self._freigeben(j)
            self.puffer = kuenftig
            j = dict(j)
            j["message"] = dict(msg, content="")   # Inhalt halten, Rest (done, Zaehler) durchlassen
        if j.get("done"):
            return self.finish(j)
        return [] if stueck else [j]

    def _freigeben(self, j):
        """Gehaltenen Text (samt dem dieses Chunks) vor den aktuellen Chunk schieben."""
        msg = j.get("message") or {}
        text = self.puffer + (msg.get("content") or "")
        self.puffer = ""
        if not text:
            return [j]
        # Bewusst minimal: die Zaehler (eval_count & Co.) bleiben am Abschluss-Chunk,
        # sonst verrechnet sich perf_record an einem Chunk, der gar kein Ende ist.
        vorher = {"model": j.get("model"), "message": {"role": "assistant", "content": text},
                  "done": False}
        if j.get("created_at"):
            vorher["created_at"] = j["created_at"]
        rest = dict(j)
        rest["message"] = dict(msg, content="")
        return [vorher, rest]

    def finish(self, j):
        """Letzter Chunk: entweder war es ein Aufruf, oder es war doch Text."""
        self.haltend = False
        if self.puffer:
            name, args = extract(self.puffer, self.erlaubt, self.tools)
            if name:
                self.gerettet = name
                self.puffer = ""
                j = dict(j)
                j["message"] = {"role": "assistant", "content": "",
                                "tool_calls": [{"function": {"name": name, "arguments": args or {}}}]}
                return [j]
            # nicht herausloesbar: der Text geht als Text hinaus (besser als verschlucken),
            # aber der Vorfall wird gemeldet statt still zu bleiben.
            self.verloren = verdacht(self.puffer)
        return self._freigeben(j)
