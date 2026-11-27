"""PII + secret detectors (Part 12) with validation, not just regex.

Every finding: type, span, confidence. Validation keeps false positives down:
  Aadhaar = 12 digits, first digit 2-9, Verhoeff checksum; card = 13-19 digits, Luhn; PAN = AAAAA9999A with a valid
  4th-character entity code; IPs through `ipaddress`; generic secrets need a key/secret/token word nearby + entropy.
An optional Presidio adapter (names, addresses) plugs in through `EXTRA_DETECTORS` (off by default).
"""

from __future__ import annotations

import ipaddress
import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    type: str
    start: int
    end: int
    value: str
    confidence: float


# --- checksums ---------------------------------------------------------------------------------
_V_D = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
        [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
        [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
        [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]
_V_P = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
        [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
        [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]]
_V_INV = [0, 4, 3, 2, 1, 5, 6, 7, 8, 9]


def verhoeff_ok(digits: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _V_D[c][_V_P[i % 8][int(ch)]]
    return c == 0


def verhoeff_check_digit(digits: str) -> str:
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _V_D[c][_V_P[(i + 1) % 8][int(ch)]]
    return str(_V_INV[c])


def luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def entropy(s: str) -> float:
    counts = Counter(s)
    return -sum(n / len(s) * math.log2(n / len(s)) for n in counts.values()) if s else 0.0


# --- patterns ----------------------------------------------------------------------------------
EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}\b")
PHONE_IN = re.compile(r"(?<![\w+])(?:\+91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")
PHONE_E164 = re.compile(r"(?<![\w+])\+[1-9]\d{7,14}(?!\d)")
AADHAAR = re.compile(r"(?<!\d)[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}(?!\d)")
PAN = re.compile(r"\b[A-Z]{3}[PCHFATBLJG][A-Z]\d{4}[A-Z]\b")
CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
IFSC = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")
PASSPORT = re.compile(r"\b[A-PR-WY][1-9]\d{6}\b")
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
IPV6 = re.compile(r"(?<![:\w])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![:\w])")  # ipaddress validates
SECRETS: list[tuple[str, re.Pattern[str]]] = [
    ("SECRET", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("SECRET", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("SECRET", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    ("SECRET", re.compile(r"gh[pousr]_[A-Za-z0-9]{36}")),
    ("SECRET", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("SECRET", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("SECRET", re.compile(r"af_[A-Za-z0-9_\-]{40,}")),  # AgentFlow user keys
]
GENERIC_SECRET = re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|access[_-]?key)\b\s*[:=]\s*['\"]?"
                            r"([A-Za-z0-9_\-/+=.]{16,})")

# Higher number wins when spans overlap (a card number is not also a phone number).
PRIORITY = {"SECRET": 9, "AADHAAR": 8, "CARD": 7, "PAN": 6, "EMAIL": 5, "IFSC": 4, "PASSPORT": 3, "PHONE": 2,
            "IP": 1}
EXTRA_DETECTORS: list[Callable[[str], list[Finding]]] = []  # e.g. a Presidio adapter (names, addresses)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def detect(text: str) -> list[Finding]:
    if not text:
        return []
    found: list[Finding] = []
    for t, pat in SECRETS:
        found += [Finding(t, m.start(), m.end(), m.group(0), 0.99) for m in pat.finditer(text)]
    for m in GENERIC_SECRET.finditer(text):
        if entropy(m.group(1)) >= 3.5:
            found.append(Finding("SECRET", m.start(1), m.end(1), m.group(1), 0.8))
    found += [Finding("EMAIL", m.start(), m.end(), m.group(0), 0.99) for m in EMAIL.finditer(text)]
    for m in AADHAAR.finditer(text):
        if verhoeff_ok(_digits(m.group(0))):
            found.append(Finding("AADHAAR", m.start(), m.end(), m.group(0), 0.95))
    found += [Finding("PAN", m.start(), m.end(), m.group(0), 0.9) for m in PAN.finditer(text)]
    for m in CARD.finditer(text):
        d = _digits(m.group(0))
        if 13 <= len(d) <= 19 and luhn_ok(d) and len(set(d)) > 1:
            found.append(Finding("CARD", m.start(), m.end(), m.group(0), 0.9))
    found += [Finding("IFSC", m.start(), m.end(), m.group(0), 0.85) for m in IFSC.finditer(text)]
    found += [Finding("PASSPORT", m.start(), m.end(), m.group(0), 0.6) for m in PASSPORT.finditer(text)]
    for pat in (PHONE_IN, PHONE_E164):
        found += [Finding("PHONE", m.start(), m.end(), m.group(0), 0.8) for m in pat.finditer(text)]
    for pat in (IPV4, IPV6):
        for m in pat.finditer(text):
            try:
                ipaddress.ip_address(m.group(0))
            except ValueError:
                continue
            found.append(Finding("IP", m.start(), m.end(), m.group(0), 0.9))
    for extra in EXTRA_DETECTORS:
        found += extra(text)
    # resolve overlaps: highest priority, then longest
    found.sort(key=lambda f: (-PRIORITY.get(f.type, 0), -(f.end - f.start), f.start))
    kept: list[Finding] = []
    for f in found:
        if all(f.end <= k.start or f.start >= k.end for k in kept):
            kept.append(f)
    return sorted(kept, key=lambda f: f.start)


def types_in(text: str) -> list[str]:
    return sorted({f.type for f in detect(text)})
