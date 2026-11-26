"""Heuristic prompt-injection detector for untrusted tool results (web pages, search snippets, API bodies).

It never blocks anything: it flags. Flags go into the journal row, an event, and the approval payload a
human sees. The real defence is structural: untrusted content is delimited, and once a task has read
any, every external write needs a human approval (code-enforced).
"""

from __future__ import annotations

import re
from typing import Any

RULES: list[tuple[str, re.Pattern[str]]] = [
    ("ignore previous instructions", re.compile(r"\b(ignore|disregard|forget)\s+(all\s+|any\s+)?(the\s+)?"
                                                r"(previous|prior|above|earlier)\s+(instructions|prompts|rules)", re.I)),
    ("role override", re.compile(r"\byou\s+are\s+now\b|\bact\s+as\s+(an?\s+)?(admin|system|developer)\b", re.I)),
    ("fake system message", re.compile(r"\b(system\s+(note|prompt|message)|new\s+instructions?)\s*[:!]", re.I)),
    ("exfiltration request", re.compile(r"\b(send|email|forward|post|upload)\b[^.\n]{0,60}\b(all|every|the)\b[^.\n]{0,40}"
                                        r"\b(data|files?|secrets?|keys?|passwords?|results?)\b", re.I)),
    ("hidden text", re.compile(r"[​‌‍⁠﻿]|display\s*:\s*none|font-size\s*:\s*0", re.I)),
    ("email address in instructions", re.compile(r"\b(to|at)\s+[\w.+-]+@[\w-]+\.[\w.-]+\s+(immediately|now|asap)\b", re.I)),
]


def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _texts(v)]
    if isinstance(value, list):
        return [t for v in value for t in _texts(v)]
    return []


def scan(value: Any) -> list[str]:
    """Names of the rules matched anywhere in a (nested) tool result."""
    text = "\n".join(_texts(value))
    return [name for name, pattern in RULES if pattern.search(text)]
