"""Input normalization and phonetic search keys.

Greek words and Greeklish (Greek typed in Latin letters) are both reduced to
the same coarse phonetic key, so `δέντρο`, `δενδρο`, `dentro` and `dendro`
all collide. German words get a key that ignores case, umlauts and ß.
"""
from __future__ import annotations

import re
import unicodedata

MAX_QUERY_LEN = 50

_GREEK_RE = re.compile(r"[Ͱ-Ͽἀ-῿]")
_GERMAN_ONLY_RE = re.compile(r"[äöüßÄÖÜẞ]")


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return unicodedata.normalize(
        "NFC", "".join(c for c in decomposed if not unicodedata.combining(c))
    )


def clean(text: str) -> str:
    """Trim, collapse whitespace, cap length."""
    return " ".join(text.split())[:MAX_QUERY_LEN]


def detect_script(text: str) -> str:
    """'greek' | 'german' | 'latin'. 'latin' may be German or Greeklish."""
    if _GREEK_RE.search(text):
        return "greek"
    if _GERMAN_ONLY_RE.search(text):
        return "german"
    return "latin"


# --- German -----------------------------------------------------------------

_DE_TABLE = str.maketrans({"ä": "a", "ö": "o", "ü": "u"})


def german_key(text: str) -> str:
    t = text.casefold().replace("ß", "ss").translate(_DE_TABLE)
    t = t.replace("ae", "a").replace("oe", "o").replace("ue", "u")
    return re.sub(r"[^a-z]", "", t)


# --- Greek ------------------------------------------------------------------

# Longest match first.
_GREEK_RULES = [
    ("ου", "u"), ("ει", "i"), ("οι", "i"), ("υι", "i"), ("αι", "e"),
    ("αυ", "af"), ("ευ", "ef"),
    ("μπ", "b"), ("ντ", "d"), ("νδ", "d"), ("μβ", "b"), ("γκ", "g"), ("γγ", "ng"), ("τσ", "ts"), ("τζ", "dz"),
    ("α", "a"), ("β", "v"), ("γ", "g"), ("δ", "d"), ("ε", "e"), ("ζ", "z"),
    ("η", "i"), ("θ", "t"), ("ι", "i"), ("κ", "k"), ("λ", "l"), ("μ", "m"),
    ("ν", "n"), ("ξ", "ks"), ("ο", "o"), ("π", "p"), ("ρ", "r"), ("σ", "s"),
    ("ς", "s"), ("τ", "t"), ("υ", "i"), ("φ", "f"), ("χ", "h"), ("ψ", "ps"),
    ("ω", "o"),
]

# Greeklish: the same collapsing applied to Latin spelling.
_LATIN_RULES = [
    ("ou", "u"), ("ei", "i"), ("oi", "i"), ("ai", "e"), ("au", "af"), ("av", "af"),
    ("eu", "ef"), ("ev", "ef"),
    ("mp", "b"), ("mb", "b"), ("nt", "d"), ("nd", "d"), ("gk", "g"), ("gg", "ng"),
    ("ts", "ts"), ("tz", "dz"), ("th", "t"), ("ch", "h"), ("kh", "h"), ("ph", "f"),
    ("dh", "d"), ("gh", "g"), ("ps", "ps"), ("ks", "ks"),
    ("y", "i"), ("w", "o"), ("x", "ks"), ("c", "k"), ("q", "k"), ("j", "i"),
    ("8", "t"), ("3", "ks"), ("h", "i"),
]


def _apply(rules, text: str) -> str:
    out = []
    i = 0
    while i < len(text):
        for src, dst in rules:
            if text.startswith(src, i):
                out.append(dst)
                i += len(src)
                break
        else:
            ch = text[i]
            if ch.isalnum():
                out.append(ch)
            i += 1
    return "".join(out)


def greek_key(text: str) -> str:
    """Phonetic key for a Greek-script word."""
    return _collapse(_apply(_GREEK_RULES, strip_accents(text).casefold()))


def greeklish_key(text: str) -> str:
    """Phonetic key for Greek typed in Latin letters."""
    t = strip_accents(text).casefold()
    # 'h' alone is rare in Greeklish except for η/χ; digraphs are handled first.
    return _collapse(_apply(_LATIN_RULES, t))


def _collapse(key: str) -> str:
    """Collapse doubled letters (Greek geminates are not audible)."""
    return re.sub(r"(.)\1+", r"\1", key)


_LITERAL = {
    "α": "a", "β": "v", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "i", "θ": "th",
    "ι": "i", "κ": "k", "λ": "l", "μ": "m", "ν": "n", "ξ": "x", "ο": "o", "π": "p",
    "ρ": "r", "σ": "s", "ς": "s", "τ": "t", "υ": "y", "φ": "f", "χ": "ch", "ψ": "ps", "ω": "o",
}


def literal_latin(text: str) -> str:
    """Letter-by-letter Greeklish spelling, used to rank homophone candidates."""
    t = strip_accents(text).casefold()
    return "".join(_LITERAL.get(c, c) for c in t)
