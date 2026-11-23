"""Locale packs for the content check before a cloud tier (router 0.42.0, ADR-0119).

A pack bundles what depends on the user's country and language: the patterns that recognise contact data and national
identifiers in what the user wrote, the keyword groups, the refusal sentence and the marker that replaces a removed tool
result. `router.cloud.privacy_check.locale` chooses one; the operator's `terms` and `refusal` still apply on top.

`ch-de` is the default and holds exactly the values from before router 0.42.0 (Swiss phone and AHV numbers, German street
suffixes, German and English keyword starts, German refusal and marker). `en` is a neutral English pack: e-mail, phone
numbers in international and common national shapes, IBAN, English keyword starts; no national identifier, no street
address. A pack is added only together with test sentences that its patterns must hit and must not hit
(`test/selftest_privacy_redactors.py`).

Packs live in the repository and are chosen by configuration (ADR-0118); there is no way to load one from a file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

DEFAULT_LOCALE = "ch-de"


@dataclass(frozen=True)
class LocalePack:
    name: str
    patterns: dict          # rule name -> compiled pattern, checked in this order
    keywords: dict          # keyword group -> alternation of word starts (case-insensitive)
    refusal: str            # the answer when the request must stay local and no local node can answer
    marker: str             # replaces the whole result of a tool in redact_tools
    keyword_rx: dict = field(default=None, compare=False)


def _pack(name, patterns, keywords, refusal, marker):
    return LocalePack(name, patterns, keywords, refusal, marker,
                      keyword_rx={k: re.compile(rf"(?i)\b(?:{v})") for k, v in keywords.items()})


# --- ch-de: the values of router 0.13.0 up to 0.42.0, unchanged (the default) ---

# Built-in keyword groups: word starts (German and English), case-insensitive. A hit keeps the request local - false
# positives cost a cloud answer, false negatives cost privacy, so the lists lean wide.
CH_DE_KEYWORDS = {
    "health": r"arzt|ärzt|krank|diagnos|medikament|tablette|therapie|therapeut|psycholog|psychiat|depress|schwanger|"
              r"operation|blutdruck|blutzucker|symptom|schmerz|kopfweh|schwindl|übelkeit|erbrech|fieber|husten|spital|krankenhaus|klinik|impf|allergi|verletz|"
              r"doctor|illness|medication|pregnan|therapy|diagnos",
    "finances": r"kontostand|bankkonto|konto\b|lohn\b|löhne|gehalt|schulden|kredit|steuererkl|steuern\b|einkommen|vermögen|"
                r"hypothek|versicherungsnummer|salary|debt|bank account",
    "credentials": r"passw|kennwort|pin\b|pin-|zugangsdaten|login|benutzername|password|credential",
    "relationships": r"beziehung|scheidung|trennung|liebesleben|sex|affäre|verliebt|divorce|relationship",
    "law": r"anwalt|anwält|gericht|polizei|strafanzeige|strafe\b|vorstrafe|lawyer|police|court",
}
CH_DE_PATTERNS = {
    "email": re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b"),
    "phone": re.compile(r"(?:\+|00)\d{2}[\s/.-]?\d{2}[\s/.-]?\d{3}[\s/.-]?\d{2}[\s/.-]?\d{2}|\b0\d{2}[\s/.-]?\d{3}[\s/.-]?\d{2}[\s/.-]?\d{2}\b"),
    "iban": re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}\b"),
    "ahv": re.compile(r"\b756[.\s]?\d{4}[.\s]?\d{4}[.\s]?\d{2}\b"),
    "address": re.compile(r"(?i)\b[\wäöü-]+(?:strasse|straße|str\.|weg|gasse|platz|allee|ring)\s+\d+[a-z]?\b"),
}
CH_DE_REFUSAL = "Das beantworte ich nur lokal, und der lokale Rechner ist gerade nicht erreichbar."
CH_DE_MARKER = "[fuer die Cloud entfernt]"

# --- en: neutral English pack (contact data and bank accounts, no national identifier) ---

EN_KEYWORDS = {
    "health": r"doctor|illness|ill\b|sick|medication|medicine|pill|pregnan|therapy|therapist|psycholog|psychiatr|depress|"
              r"diagnos|symptom|surgery|blood pressure|blood sugar|pain|headache|dizz|nausea|vomit|fever|cough|hospital|"
              r"clinic|vaccin|allerg|injur",
    "finances": r"account balance|bank account|salary|salaries|wage|debt|loan|credit card|tax return|taxes\b|income|"
                r"mortgage|social security",
    "credentials": r"passw|passcode|pin\b|pin-|login|username|credential",
    "relationships": r"relationship|divorce|separation|love life|sex|affair|in love|boyfriend|girlfriend",
    "law": r"lawyer|attorney|court|police|criminal|lawsuit|conviction",
}
EN_PATTERNS = {
    "email": CH_DE_PATTERNS["email"],
    # international (+ or 00, country code, at least seven further digits), North American (202) 555-0123 /
    # 202-555-0143, and national numbers with a leading 0 (0161 496 0000, 020 7946 0000)
    "phone": re.compile(r"(?:\+|\b00)[1-9]\d{0,2}(?:[\s/.-]?\d){7,12}\b"
                        r"|(?:\(\d{3}\)\s?|\b\d{3}[\s.-])\d{3}[\s.-]\d{4}\b"
                        r"|\b0\d{2,4}[\s/.-]?\d{3,4}[\s/.-]?\d{3,4}\b"),
    "iban": CH_DE_PATTERNS["iban"],
}
EN_REFUSAL = "I can only answer that locally, and the local computer is not reachable right now."
EN_MARKER = "[removed for the cloud]"

PACKS = {
    "ch-de": _pack("ch-de", CH_DE_PATTERNS, CH_DE_KEYWORDS, CH_DE_REFUSAL, CH_DE_MARKER),
    "en": _pack("en", EN_PATTERNS, EN_KEYWORDS, EN_REFUSAL, EN_MARKER),
}
