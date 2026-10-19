#!/usr/bin/env python3
"""Build data/words.db from the kaikki.org Wiktionary extracts.

    python scripts/build_db.py --download      # fetch the three JSONL dumps into raw/
    python scripts/build_db.py                 # build from raw/ (default paths)

Greek and German are joined through the translation tables of English noun and
verb entries ("tree": de Baum, el δέντρο; "run": de laufen, el τρέχω), grouped by
translation sense.
The dumps are untrusted data: they are only ever parsed as JSON.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app import normalize as nz  # noqa: E402

BASE = "https://kaikki.org/dictionary"
SOURCES = {
    "en": f"{BASE}/English/kaikki.org-dictionary-English.jsonl",
    "de": f"{BASE}/German/kaikki.org-dictionary-German.jsonl",
    "el": f"{BASE}/Greek/kaikki.org-dictionary-Greek.jsonl",
}

GENDER_TAGS = {"masculine": "m", "feminine": "f", "neuter": "n"}
# "A n (strong, ...)", "Baum m (strong, ...)", "See m or f", "Band m or n"
_EXPANSION_GENDER = re.compile(r"\s([mfn](?:(?:,|\s+or|\s+and)\s*[mfn])*)(?=\s*(?:\(|$|\|))")
_GREEK_CASE_TAGS = {"nominative", "genitive", "accusative", "vocative"}


def download(dest_dir: Path) -> None:
    for name, url in SOURCES.items():
        d = dest_dir / name
        d.mkdir(parents=True, exist_ok=True)
        target = d / f"{name}.jsonl"
        if target.exists():
            print(f"have {target}")
            continue
        print(f"downloading {url}")
        urllib.request.urlretrieve(url, target)


def read_jsonl(path: Path, needle: str = ""):
    """Yield parsed lines; `needle` is a cheap substring pre-filter."""
    with open(path, encoding="utf-8") as f:
        for line in f:
            if needle and needle not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            entry["_size"] = len(line)      # long Wiktionary entries = common words
            yield entry


# --- pass 1: English entries -> senses with de/el translations ----------------

POS = ("noun", "verb")


def collect_senses(path: Path):
    """Return {pos: {(en_word, sense): {'de': set, 'el': set, 'order': n}}} for nouns and verbs.

    `order` is the sense's position in its entry: "to ingest" comes before "to worry".
    """
    groups = {pos: {} for pos in POS}
    for entry in read_jsonl(path, '"translations"'):
        pos = entry.get("pos")
        if entry.get("lang_code") != "en" or pos not in groups:
            continue
        # Most entries keep their translation tables per sense, a few at the top level.
        tables = entry.get("translations", []) + [
            t for s in entry.get("senses", []) for t in s.get("translations", [])]
        order = {}
        for t in tables:
            lang = t.get("lang_code")
            word = (t.get("word") or "").strip()
            if lang not in ("de", "el") or not word or " " in word:
                continue
            sense = (t.get("sense") or "").split(" — ")[0].strip()   # "to ingest — see also consume"
            order.setdefault(sense, len(order))
            g = groups[pos].setdefault((entry["word"], sense),
                                       {"de": set(), "el": set(), "order": order[sense]})
            g[lang].add(word)
    return {pos: {k: v for k, v in g.items() if v["de"] and v["el"]} for pos, g in groups.items()}


# --- pass 2: German nouns ---------------------------------------------------------

_PAREN = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_LEADING = re.compile(r"^(a|an|the)\s+")
_LEADING_VERB = re.compile(r"^(to)\s+")


def gloss_keys(glosses, verb: bool = False) -> list:
    """Short, comparable English gloss strings, best (first sense) first."""
    leading = _LEADING_VERB if verb else _LEADING
    out = []
    for g in glosses:
        for part in re.split(r"[;,]", _PAREN.sub("", g)):
            part = leading.sub("", part.strip().lower().rstrip("."))
            if (2 if verb else 3) <= len(part) <= 30 and len(part.split()) <= 3 and part not in out:
                out.append(part)
    return out


# Senses with these tags are poor translations ("Fiffi" for dog, dialect words ...).
_MARKED = {"colloquial", "slang", "dialectal", "archaic", "obsolete", "rare", "vulgar", "dated",
           "regional", "informal", "derogatory", "poetic", "Switzerland", "Austria", "Bavaria",
           "Low-German", "humorous", "pejorative", "figuratively", "jargon", "historical"}


def all_glosses(entry) -> list:
    senses = [s for s in entry.get("senses", []) if "form-of" not in s.get("tags", [])]
    plain = [s for s in senses if not (set(s.get("tags", [])) & _MARKED)]
    # sein: the auxiliary sense comes first in Wiktionary, "to be" is what people look for
    plain.sort(key=lambda s: "auxiliary" in s.get("tags", []))
    # a sense nested under a heading has glosses ["As a copulative verb:", "to be"]
    glosses = [next((g for g in s.get("glosses", []) if not g.endswith(":")), None)
               for s in (plain or senses)]
    return [g for g in glosses if g]


_EXAMPLE_LEN = (12, 100)


def pick_example(entry, decl=None, forms=()):
    """First short, translated usage example of a plain sense that mentions the word.

    `decl` is a noun's declension table, `forms` a verb's conjugated forms.
    Quotations are skipped: they are mostly 19th-century literature in old spelling.
    """
    forms = {entry["word"], *forms}
    for cases in (decl or {}).values():
        for variants in cases.values():
            forms.update(variants)
    words = re.compile(r"\b(?:%s)\b" % "|".join(map(re.escape, sorted(forms, key=len, reverse=True))))
    senses = [s for s in entry.get("senses", []) if "form-of" not in s.get("tags", [])]
    plain = [s for s in senses if not (set(s.get("tags", [])) & _MARKED)]
    for s in plain:
        for ex in s.get("examples", []):
            text = (ex.get("text") or "").strip()
            translation = (ex.get("translation") or ex.get("english") or "").strip()
            if (ex.get("type") == "example"
                    and _EXAMPLE_LEN[0] <= len(text) <= _EXAMPLE_LEN[1]
                    # a full sentence, not a set phrase like "Wasser lassen" (to urinate)
                    and text[0].isupper() and text[-1] in ".!?"
                    and translation and not translation.startswith("(please add")
                    and words.search(text)):
                return {"text": text, "translation": translation}
    return None


def parse_genders(entry) -> list:
    genders = []
    for ht in entry.get("head_templates", []):
        m = _EXPANSION_GENDER.search(ht.get("expansion", ""))
        if m:
            genders += re.findall(r"[mfn]", m.group(1))
    if not genders:
        for s in entry.get("senses", []):
            genders += [GENDER_TAGS[t] for t in s.get("tags", []) if t in GENDER_TAGS]
    return list(dict.fromkeys(genders))


def parse_plural(entry):
    forms = entry.get("forms", [])
    for f in forms:
        if f.get("tags") == ["plural"] and f.get("form") not in ("-", ""):
            return f["form"]
    for f in forms:
        tags = f.get("tags", [])
        if "nominative" in tags and "plural" in tags and f.get("form") not in ("-", ""):
            return f["form"]
    return None


_VARIANT_GLOSS = re.compile(
    r"(obsolete|archaic|dated|nonstandard|alternative|superseded|rare|misspelling|eye dialect)\b", re.I)
_CASES = ("nominative", "genitive", "dative", "accusative")
_ODD_FORMS = {"colloquial", "dialectal", "archaic", "dated", "rare", "poetic", "obsolete",
              "Switzerland", "Austria", "nonstandard", "proscribed"}


def parse_declension(entry):
    """{'singular': {'nominative': ['Baum'], ...}, 'plural': {...}} or None."""
    table = {"singular": {}, "plural": {}}
    for f in entry.get("forms", []):
        tags = set(f.get("tags", []))
        form = (f.get("form") or "").strip()
        if f.get("source") != "declension" or not form or form == "-" or tags & _ODD_FORMS:
            continue
        case = next((c for c in _CASES if c in tags), None)
        number = "singular" if "singular" in tags else "plural" if "plural" in tags else None
        if case and number:
            slot = table[number].setdefault(case, [])
            if form not in slot:
                slot.append(form)
    # Some entries carry a stray second table (Frau: singular genitive "Frauen");
    # drop singular variants that are really the plural form.
    for case, forms in table["singular"].items():
        plural = set(table["plural"].get(case, []))
        kept = [f for f in forms if f not in plural]
        if kept:
            table["singular"][case] = kept
    table = {n: c for n, c in table.items() if c}
    return table or None


def german_nouns(path: Path):
    for entry in read_jsonl(path, '"pos": "noun"'):
        if entry.get("lang_code") != "de" or entry.get("pos") != "noun":
            continue
        senses = entry.get("senses", [])
        if not senses or all("form-of" in s.get("tags", []) for s in senses):
            continue
        genders = parse_genders(entry)
        word = entry["word"]
        if not genders or " " in word or not word[:1].isupper():
            continue
        glosses = all_glosses(entry)
        if glosses and _VARIANT_GLOSS.match(glosses[0]):
            continue                       # "obsolete form of Haus" etc.
        decl = parse_declension(entry)
        yield (word, genders, parse_plural(entry), (glosses[0] if glosses else None),
               gloss_keys(glosses), entry["_size"], decl, pick_example(entry, decl))


# --- pass 2b: German verbs ---------------------------------------------------------------

# Slot order of every tense list: ich, du, er/sie/es, wir, ihr, sie/Sie.
PERSONS = [(p, n) for n in ("singular", "plural")
           for p in ("first-person", "second-person", "third-person")]
# Simple tenses only; Perfekt, Plusquamperfekt and Futur are put together by the app
# from the auxiliary, participle and infinitive.
SIMPLE_TENSES = {   # name: (tags that must be present, tags that must be absent)
    "present": ({"indicative", "present"}, set()),
    "preterite": ({"indicative", "preterite"}, set()),
    "subjunctive-i": ({"subjunctive-i"}, {"future"}),
    "subjunctive-ii": ({"subjunctive-ii"}, {"future"}),
    "imperative": ({"imperative"}, set()),
}
_VERB_ODD_FORMS = _ODD_FORMS | {"formal", "Northwest-German", "Southern-German", "regional"}


def parse_conjugation(entry):
    """{'infinitive', 'zu', 'participle1', 'participle2', 'auxiliary': [...],
        'tenses': {name: [[forms] per PERSONS slot]}, 'subordinate': [forms]} or None.

    'subordinate' holds the joined forms of separable verbs (dass er anfängt): searchable,
    not shown.
    """
    out = {"infinitive": entry["word"], "auxiliary": [], "tenses": {}, "subordinate": []}
    odd = {}   # same shape as tenses, for colloquial/rare forms used only when nothing else exists
    for f in entry.get("forms", []):
        tags = set(f.get("tags", []))
        form = " ".join((f.get("form") or "").split())
        if not form or form == "-" or "multiword-construction" in tags:
            continue
        if "auxiliary" in tags:
            if not tags & _VERB_ODD_FORMS:
                out["auxiliary"] += [a for a in re.split(r"\s+or\s+", form)
                                     if a in ("haben", "sein") and a not in out["auxiliary"]]
            continue
        if f.get("source") != "conjugation":
            continue
        if "participle" in tags:
            key = "participle2" if "past" in tags else "participle1" if "present" in tags else None
            if key and key not in out:
                out[key] = form
            continue
        if "infinitive-zu" in tags:
            out.setdefault("zu", form)
            continue
        if "subordinate-clause" in tags:
            if form not in out["subordinate"]:
                out["subordinate"].append(form)
            continue
        slot = next((i for i, (p, n) in enumerate(PERSONS) if p in tags and n in tags), None)
        if slot is None:
            continue
        for name, (need, avoid) in SIMPLE_TENSES.items():
            if need <= tags and not tags & avoid:
                target = odd if tags & _VERB_ODD_FORMS else out["tenses"]
                forms = target.setdefault(name, [[] for _ in PERSONS])[slot]
                if form not in forms:
                    forms.append(form)
                break
    for name, slots in odd.items():
        mine = out["tenses"].setdefault(name, [[] for _ in PERSONS])
        for i, forms in enumerate(slots):
            if not mine[i]:
                mine[i] = forms
    if not out["tenses"].get("present"):
        return None
    return out


def conjugated_forms(conj) -> list:
    """Every form a user might type for this verb."""
    if not conj:
        return []
    forms = {conj["infinitive"], *conj["subordinate"]}
    forms |= {conj[k] for k in ("zu", "participle1", "participle2") if k in conj}
    for slots in conj["tenses"].values():
        for variants in slots:
            forms.update(variants)
    return sorted(forms)


def german_verbs(path: Path):
    for entry in read_jsonl(path, '"pos": "verb"'):
        if entry.get("lang_code") != "de" or entry.get("pos") != "verb":
            continue
        senses = entry.get("senses", [])
        if not senses or all("form-of" in s.get("tags", []) for s in senses):
            continue
        word = entry["word"]
        if " " in word or not word[:1].islower():
            continue
        glosses = all_glosses(entry)
        if not glosses or _VARIANT_GLOSS.match(glosses[0]):
            continue
        conj = parse_conjugation(entry)
        if not conj:                       # spelling variants ("ässe") and stubs: no table
            continue
        forms = conjugated_forms(conj)
        # separable verbs: "fängt an" -> also look for "fängt" in example sentences
        words = {w for f in forms for w in f.split()[:1]}
        yield (word, glosses[0], gloss_keys(glosses, verb=True), entry["_size"], conj, forms,
               pick_example(entry, forms=words))


# --- pass 3: Greek forms -------------------------------------------------------------

_GREEK_VERB_SKIP = {"romanization", "table-tags", "inflection-template"}


def greek_words(path: Path, pos: str):
    """Return (lemmas, form_links) for Greek nouns or verbs.

    lemmas:     {lemma: (gloss keys, [inflected forms], entry size)}
    form_links: [(inflected form, lemma)] from "form-of" entries
    """
    lemmas, links = {}, []
    for entry in read_jsonl(path, f'"pos": "{pos}"'):
        if entry.get("lang_code") != "el" or entry.get("pos") != pos:
            continue
        word = entry["word"]
        for s in entry.get("senses", []):
            for fo in s.get("form_of", []):
                if fo.get("word"):
                    links.append((word, fo["word"]))
        glosses = all_glosses(entry)
        if not glosses or " " in word:
            continue
        forms = [
            f["form"] for f in entry.get("forms", [])
            if f.get("form") and " " not in f["form"] and (
                set(f.get("tags", [])) & _GREEK_CASE_TAGS if pos == "noun"
                else f.get("source") == "conjugation" and not set(f.get("tags", [])) & _GREEK_VERB_SKIP)
        ]
        keys = gloss_keys(glosses, verb=pos == "verb")
        prev = lemmas.get(word)
        lemmas[word] = (
            (prev[0] + [g for g in keys if g not in prev[0]], prev[1] + forms,
             prev[2] + entry["_size"])
            if prev else (keys, forms, entry["_size"])
        )
    return lemmas, links


MAX_RANK = 4          # sum of the gloss positions in the Greek and German entries
MAX_PER_GREEK = 8     # German candidates kept per Greek lemma


def pair(curated, el_words, de_lemmas, de_by_gloss, de_weight):
    """Greek lemma -> {German lemma: (rank, gloss)}, for one part of speech."""
    # (a) curated pairs from the English entries' translation tables: rank about 0,
    #     earlier senses and common words first
    pairs = {}
    for (en, sense), langs in curated.items():
        for el in langs["el"]:
            for de in langs["de"] & de_lemmas:
                rank = 0.1 * min(langs["order"], 5) - 0.15 * math.log10(max(de_weight.get(de, 1), 10))
                if de not in pairs.get(el, {}) or rank < pairs[el][de][0]:
                    pairs.setdefault(el, {})[de] = (rank, sense or en)

    # (b) shared English gloss between a Greek lemma and a German word
    for el, (keys, _forms, _size) in el_words.items():
        found = {}  # german lemma -> (best position sum, shared gloss count, gloss)
        for el_pos, k in enumerate(keys):
            for de, de_pos in de_by_gloss.get(k, ()):
                pos = el_pos + de_pos + 1
                cur = found.get(de)
                if cur is None:
                    found[de] = (pos, 1, k)
                else:
                    found[de] = (min(cur[0], pos), cur[1] + 1, cur[2] if cur[0] <= pos else k)
        for de, (pos, shared, k) in found.items():
            # several shared glosses = better match; longer entries (common words) win ties
            rank = pos - 0.5 * (shared - 1) - 0.15 * math.log10(max(de_weight.get(de, 1), 10))
            if pos <= MAX_RANK and (de not in pairs.get(el, {}) or rank < pairs[el][de][0]):
                pairs.setdefault(el, {})[de] = (rank, k)
    return pairs


def build(args) -> None:
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    conn = db.connect(str(tmp))
    db.init_schema(conn)
    # per part of speech: German lemmas, gloss key -> [(lemma, position)], lemma -> entry size
    de_lemmas = {pos: set() for pos in POS}
    de_by_gloss = {pos: {} for pos in POS}
    de_weight = {pos: {} for pos in POS}

    print("pass 1/4: German nouns ...")
    de_rows, de_forms, en_rows = [], [], []
    for noun_id, (lemma, genders, plural, gloss, keys, size, decl, example) in enumerate(
            german_nouns(Path(args.de)), start=1):
        de_rows.append((noun_id, lemma, nz.german_key(lemma), ",".join(genders), plural, gloss,
                        json.dumps(decl, ensure_ascii=False) if decl else None,
                        math.log10(max(size, 10)),
                        json.dumps(example, ensure_ascii=False) if example else None))
        for forms in (decl or {}).values():
            for variants in forms.values():
                de_forms += [(v, noun_id) for v in variants]
        de_lemmas["noun"].add(lemma)
        for pos, k in enumerate(keys):
            de_by_gloss["noun"].setdefault(k, []).append((lemma, pos))
            en_rows.append((k, noun_id, 0.5 * pos - 0.8 * math.log10(max(size, 10))))   # common words first
        de_weight["noun"][lemma] = max(de_weight["noun"].get(lemma, 0), size)
    conn.executemany(
        "INSERT INTO de_noun(id, lemma, key, genders, plural, gloss, declension, weight, example) "
        "VALUES (?,?,?,?,?,?,?,?,?)", de_rows
    )
    conn.executemany("INSERT INTO de_form VALUES (?,?)", sorted(set(de_forms)))
    conn.executemany("INSERT INTO en_gloss VALUES (?,?,?)", en_rows)
    print(f"  {len(de_rows):,} nouns")

    print("pass 2/4: German verbs ...")
    verb_rows, verb_forms, en_rows = [], [], []
    for verb_id, (lemma, gloss, keys, size, conj, forms, example) in enumerate(
            german_verbs(Path(args.de)), start=1):
        verb_rows.append((verb_id, lemma, nz.german_key(lemma), gloss,
                          json.dumps(conj, ensure_ascii=False) if conj else None,
                          math.log10(max(size, 10)),
                          json.dumps(example, ensure_ascii=False) if example else None))
        verb_forms += [(f, verb_id) for f in forms]
        de_lemmas["verb"].add(lemma)
        for pos, k in enumerate(keys):
            de_by_gloss["verb"].setdefault(k, []).append((lemma, pos))
            en_rows.append((k, verb_id, 0.5 * pos - 0.8 * math.log10(max(size, 10))))
        de_weight["verb"][lemma] = max(de_weight["verb"].get(lemma, 0), size)
    conn.executemany(
        "INSERT INTO de_verb(id, lemma, key, gloss, conjugation, weight, example) "
        "VALUES (?,?,?,?,?,?,?)", verb_rows
    )
    conn.executemany("INSERT INTO de_verb_form VALUES (?,?)", sorted(set(verb_forms)))
    conn.executemany("INSERT INTO en_verb_gloss VALUES (?,?,?)", en_rows)
    print(f"  {len(verb_rows):,} verbs")

    print("pass 3/4: Greek nouns and verbs ...")
    el_words = {pos: greek_words(Path(args.el), pos) for pos in POS}
    for pos in POS:
        print(f"  {len(el_words[pos][0]):,} {pos} lemmas")

    print("pass 4/4: pairing via English translation tables and shared glosses ...")
    curated = collect_senses(Path(args.en))
    el_lemmas, forms, el_weight = set(), set(), {}
    for pos in POS:
        lemmas, form_links = el_words[pos]
        pairs = pair(curated[pos], lemmas, de_lemmas[pos], de_by_gloss[pos], de_weight[pos])
        for el, cands in pairs.items():
            best = sorted(cands.items(), key=lambda kv: (kv[1][0], kv[0]))[:MAX_PER_GREEK]
            for de, (rank, gloss) in best:
                sid = conn.execute("INSERT INTO sense(id, en_lemma, gloss) VALUES (NULL,?,?)", (gloss, gloss)).lastrowid
                conn.execute("INSERT INTO translation VALUES (?,?,?,?)", (sid, "el", el, rank))
                conn.execute("INSERT INTO translation VALUES (?,?,?,?)", (sid, "de", de, rank))
        print(f"  {len(pairs):,} Greek {pos} lemmas with German matches")

        forms |= {(el, el) for el in pairs}
        for el in pairs:
            if el in lemmas:
                forms |= {(f, el) for f in lemmas[el][1]}
                el_weight[el] = max(el_weight.get(el, 0), lemmas[el][2])
        forms |= {(f, l) for f, l in form_links if l in pairs}
        el_lemmas |= set(pairs)
    conn.executemany(
        "INSERT INTO el_form VALUES (?,?,?,?)",
        [(f, nz.greek_key(f), l, math.log10(max(el_weight.get(l, 10), 10))) for f, l in sorted(forms)],
    )
    print(f"  {len(forms):,} forms")

    conn.execute("INSERT OR REPLACE INTO meta VALUES ('source', 'kaikki.org Wiktionary extracts')")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    tmp.replace(out)
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--download", action="store_true", help="fetch dumps into raw/ first")
    ap.add_argument("--raw", default=str(ROOT / "raw"))
    for k in ("en", "de", "el"):
        ap.add_argument(f"--{k}", help=f"path to the {k} JSONL dump")
    ap.add_argument("--out", default=str(ROOT / "data" / "words.db"))
    args = ap.parse_args()
    raw = Path(args.raw)
    if args.download:
        download(raw)
    for k in ("en", "de", "el"):
        if getattr(args, k) is None:
            setattr(args, k, str(raw / k / f"{k}.jsonl"))
    build(args)


if __name__ == "__main__":
    main()
