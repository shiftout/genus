"""Lookup pipeline: exact -> phonetic key -> fuzzy suggestion, for nouns and verbs."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from rapidfuzz import fuzz, process
from rapidfuzz.distance import OSA as Levenshtein   # edit distance; a swap counts as 1

from . import normalize as nz

ARTICLE = {"m": "der", "f": "die", "n": "das"}

# Ordered by how often each case comes up, not the textbook N-G-D-A order.
CASES = [("nominative", "Nominativ"), ("accusative", "Akkusativ"),
         ("dative", "Dativ"), ("genitive", "Genitiv")]
DEFINITE = {  # article per case, in CASES order
    "m": ("der", "den", "dem", "des"),
    "f": ("die", "die", "der", "der"),
    "n": ("das", "das", "dem", "des"),
    "pl": ("die", "die", "den", "der"),
}

PERSONS = ("ich", "du", "er/sie/es", "wir", "ihr", "sie/Sie")
# Helpers for the compound tenses, which the dictionary stores only as their parts.
AUX = {
    "haben": {"present": ("habe", "hast", "hat", "haben", "habt", "haben"),
              "preterite": ("hatte", "hattest", "hatte", "hatten", "hattet", "hatten")},
    "sein": {"present": ("bin", "bist", "ist", "sind", "seid", "sind"),
             "preterite": ("war", "warst", "war", "waren", "wart", "waren")},
}
WERDEN = ("werde", "wirst", "wird", "werden", "werdet", "werden")
# (key, label), in display order; simple tenses come from the dictionary
TENSES = [("present", "Präsens"), ("preterite", "Präteritum"), ("perfect", "Perfekt"),
          ("pluperfect", "Plusquamperfekt"), ("future", "Futur I"),
          ("subjunctive-i", "Konjunktiv I"), ("subjunctive-ii", "Konjunktiv II"),
          ("imperative", "Imperativ")]

# A fuzzy hit is applied automatically only when it is this good and
# clearly better than the runner-up; otherwise it is only suggested.
SUGGEST_THRESHOLD = 70
AUTO_THRESHOLD = 90      # similarity score, or a single edit (short words score low)
AUTO_MARGIN = 5
CROSS_SCRIPT_THRESHOLD = 78   # Latin input matched against Greek keys
MAX_RESULTS = 8


@dataclass
class Result:
    article: Optional[str]            # None for verbs
    noun: str                         # the noun, or a verb's infinitive
    plural: Optional[str] = None
    gloss: Optional[str] = None
    from_word: Optional[str] = None   # Greek lemma, shown when several words sound alike
    declension: Optional[dict] = None  # {"singular": [{case, article, forms}], "plural": [...]}
    alternatives: List[str] = field(default_factory=list)  # other articles, if several genders
    example: Optional[dict] = None     # {"text", "translation"} from Wiktionary
    pos: str = "noun"                  # "noun" | "verb"
    conjugation: Optional[dict] = None  # verbs: {"parts": {...}, "tenses": [{name, rows}]}

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Lookup:
    results: List[Result]
    matched_from: Optional[str] = None      # the word actually looked up, if corrected
    suggestions: List[str] = field(default_factory=list)
    source: str = "dictionary"
    input_lang: Optional[str] = None        # "de" when the input was already German

    def as_dict(self) -> dict:
        return {
            "results": [r.as_dict() for r in self.results],
            "matched_from": self.matched_from,
            "suggestions": self.suggestions,
            "source": self.source,
            "input_lang": self.input_lang,
        }


def _english_variants(word: str) -> List[str]:
    q = re.sub(r"^(the|a|an|to)\s+", "", " ".join(word.casefold().split()))
    out = [q]
    if q.endswith("ies") and len(q) > 4:
        out.append(q[:-3] + "y")
    if q.endswith("es") and len(q) > 3:
        out.append(q[:-2])
    if q.endswith("s") and len(q) > 3:
        out.append(q[:-1])
    return out


def _declension_table(raw: Optional[str], gender: str) -> Optional[dict]:
    if not raw:
        return None
    data = json.loads(raw)
    table = {}
    for number, key in (("singular", gender), ("plural", "pl")):
        cases = data.get(number)
        if not cases:
            continue
        table[number] = [
            {"case": label, "article": DEFINITE[key][i], "forms": cases[name]}
            for i, (name, label) in enumerate(CASES)
            if name in cases
        ]
    return table or None


def _conjugation_table(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    data = json.loads(raw)
    tenses = dict(data.get("tenses", {}))
    inf, part = data.get("infinitive"), data.get("participle2")
    if part:
        # "haben or sein" (rennen, fahren): sein is the everyday intransitive use, so it leads
        auxes = sorted(data.get("auxiliary") or ["haben"], key=lambda a: a != "sein")
        for key, aux_tense in (("perfect", "present"), ("pluperfect", "preterite")):
            tenses[key] = [[f"{AUX[a][aux_tense][i]} {part}" for a in auxes] for i in range(len(PERSONS))]
    if inf:
        tenses["future"] = [[f"{w} {inf}"] for w in WERDEN]
    out = []
    for key, label in TENSES:
        rows = [{"person": PERSONS[i], "forms": forms}
                for i, forms in enumerate(tenses.get(key) or []) if forms]
        if rows:
            out.append({"name": label, "rows": rows})
    if not out:
        return None

    def third(key):   # er/sie/es form
        slots = tenses.get(key) or []
        return slots[2][0] if len(slots) > 2 and slots[2] else None
    parts = {"present": third("present"), "preterite": third("preterite"), "perfect": third("perfect")}
    return {"parts": {k: v for k, v in parts.items() if v}, "tenses": out}


def _verb_results(rows, gloss: Optional[str] = None) -> List[Result]:
    return [
        Result(
            article=None,
            noun=row["lemma"],
            gloss=gloss or row["gloss"],
            pos="verb",
            conjugation=_conjugation_table(row["conjugation"]),
            example=json.loads(row["example"]) if row["example"] else None,
        )
        for row in rows
    ]


def _dedup(results: List[Result]) -> List[Result]:
    seen, out = set(), []
    for r in results:
        if (r.article, r.noun) not in seen:
            seen.add((r.article, r.noun))
            out.append(r)
    return out


def _noun_results(rows, gloss: Optional[str] = None) -> List[Result]:
    """`gloss` overrides the noun's own gloss (used when joined through a sense)."""
    out = []
    for row in rows:
        genders = [g for g in row["genders"].split(",") if g in ARTICLE]
        if not genders:
            continue
        out.append(
            Result(
                article=ARTICLE[genders[0]],
                noun=row["lemma"],
                plural=row["plural"],
                gloss=gloss or row["gloss"],
                alternatives=[ARTICLE[g] for g in genders[1:]],
                declension=_declension_table(row["declension"], genders[0]),
                # words.db built before the example column existed: just no example
                example=json.loads(row["example"])
                if "example" in row.keys() and row["example"] else None,
            )
        )
    return out


class Dictionary:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._de_keys: Dict[str, List[str]] = {}
        self._el_keys: Dict[str, List[str]] = {}
        self._el_weight: Dict[str, float] = {}
        self._de_weight: Dict[str, float] = {}
        self._en_keys: Dict[str, List[str]] = {}
        self._en_weight: Dict[str, float] = {}
        # words.db built before verbs were added: nouns only
        self.has_verbs = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'de_verb'").fetchone() is not None
        self._load_keys()

    def _load_keys(self) -> None:
        for r in self.conn.execute("SELECT key, lemma, weight FROM de_noun"):
            self._de_keys.setdefault(r["key"], []).append(r["lemma"])
            self._de_weight[r["lemma"]] = max(self._de_weight.get(r["lemma"], 0), r["weight"])
        for r in self.conn.execute("SELECT DISTINCT key, lemma, weight FROM el_form"):
            self._el_keys.setdefault(r["key"], []).append(r["lemma"])
            self._el_weight[r["lemma"]] = r["weight"]
        en_queries = ["SELECT g.gloss, MAX(n.weight) AS w FROM en_gloss g "
                      "JOIN de_noun n ON n.id = g.noun_id GROUP BY g.gloss"]
        if self.has_verbs:
            for r in self.conn.execute("SELECT key, lemma, weight FROM de_verb"):
                self._de_keys.setdefault(r["key"], []).append(r["lemma"])
                self._de_weight[r["lemma"]] = max(self._de_weight.get(r["lemma"], 0), r["weight"])
            en_queries.append("SELECT g.gloss, MAX(v.weight) AS w FROM en_verb_gloss g "
                              "JOIN de_verb v ON v.id = g.verb_id GROUP BY g.gloss")
        for query in en_queries:
            for r in self.conn.execute(query):
                self._en_keys[r["gloss"]] = [r["gloss"]]
                self._en_weight[r["gloss"]] = max(self._en_weight.get(r["gloss"], 0), r["w"])

    # -- exact helpers -------------------------------------------------------

    def german_exact(self, word: str) -> List[Result]:
        """German nouns and verbs: as typed, else an inflected form (Bäume -> Baum,
        läuft -> laufen), else an umlaut / ß spelling variant."""
        tables = [("de_noun", "de_form", "noun_id", _noun_results)]
        if self.has_verbs:
            tables.append(("de_verb", "de_verb_form", "verb_id", _verb_results))
        stages = [
            "SELECT * FROM {t} WHERE lemma = ? COLLATE NOCASE ORDER BY id",
            "SELECT DISTINCT w.* FROM {f} f JOIN {t} w ON w.id = f.{fk} "
            "WHERE f.form = ? COLLATE NOCASE ORDER BY w.id",
            "SELECT * FROM {t} WHERE key = ? ORDER BY id",
        ]
        for i, sql in enumerate(stages):
            arg = nz.german_key(word) if i == 2 else word
            found = [to_results(self.conn.execute(sql.format(t=t, f=f, fk=fk), (arg,)).fetchall())
                     for t, f, fk, to_results in tables]
            if any(found):
                break
        nouns, verbs = found[0], (found[1] if len(found) > 1 else [])
        # German nouns are capitalised: "Laufen" is the noun first, "laufen" the verb
        return nouns + verbs if word[:1].isupper() else verbs + nouns

    def _german_hit(self, res: List[Result], typed: str, **kw) -> Lookup:
        """Input was already German: just the article (and declension), no translation."""
        if len(res) == 1:
            res[0].gloss = None          # the gloss only matters to tell homographs apart
        lemma = res[0].noun
        return Lookup(
            results=res,
            matched_from=lemma if lemma.casefold() != typed.casefold() else None,
            input_lang="de",
            **kw,
        )

    def english_lookup(self, word: str):
        """English word -> (results, the English form that matched). "to run" means verbs only."""
        verbs_only = word.casefold().startswith("to ")
        for form in _english_variants(word):
            ranked = []   # (rank, result): nouns and verbs share one ranking scale
            if not verbs_only:
                for row in self.conn.execute(
                    "SELECT n.*, g.rank AS g_rank FROM en_gloss g JOIN de_noun n ON n.id = g.noun_id "
                    "WHERE g.gloss = ? ORDER BY g.rank, n.id", (form,)
                ):
                    ranked += [(row["g_rank"], r) for r in _noun_results([row])]
            if self.has_verbs:
                for row in self.conn.execute(
                    "SELECT v.*, g.rank AS g_rank FROM en_verb_gloss g JOIN de_verb v ON v.id = g.verb_id "
                    "WHERE g.gloss = ? ORDER BY g.rank, v.id", (form,)
                ):
                    ranked += [(row["g_rank"], r) for r in _verb_results([row])]
            ranked.sort(key=lambda x: x[0])
            res = _dedup([r for _, r in ranked])
            if res:
                return res[:MAX_RESULTS], form
        return [], None

    def declension_for(self, article: str, noun: str) -> Optional[dict]:
        """Table for a noun that did not come from the dictionary (LLM answers)."""
        gender = next((g for g, a in ARTICLE.items() if a == article), None)
        for row in self.conn.execute(
            "SELECT genders, declension FROM de_noun WHERE lemma = ? ORDER BY id", (noun,)
        ):
            if gender in row["genders"].split(","):
                return _declension_table(row["declension"], gender)
        return None

    def greek_lemmas_exact(self, word: str) -> List[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT lemma FROM el_form WHERE form = ? COLLATE NOCASE", (word,)
        ).fetchall()
        return [r["lemma"] for r in rows]

    def greek_lemmas_by_key(self, key: str) -> List[str]:
        return list(dict.fromkeys(self._el_keys.get(key, [])))

    def greek_to_german(self, lemma: str) -> List[Result]:
        sql = """
            SELECT DISTINCT s.gloss AS gloss, de.rank AS t_rank, el.sense_id AS t_sense, w.*
            FROM translation el
            JOIN translation de ON de.sense_id = el.sense_id AND de.lang = 'de'
            JOIN sense s ON s.id = el.sense_id
            JOIN {table} w ON w.lemma = de.word
            WHERE el.lang = 'el' AND el.word = ?
        """
        tables = [("de_noun", _noun_results)] + ([("de_verb", _verb_results)] if self.has_verbs else [])
        ranked = []
        for table, to_results in tables:
            for row in self.conn.execute(sql.format(table=table), (lemma,)):
                ranked += [((row["t_rank"], row["t_sense"], row["id"]), r)
                           for r in to_results([row], gloss=row["gloss"])]
        ranked.sort(key=lambda x: x[0])
        return _dedup([r for _, r in ranked])

    # -- fuzzy ---------------------------------------------------------------

    def _fuzzy(self, key: str, table: Dict[str, List[str]], cutoff: int = SUGGEST_THRESHOLD):
        if not key:
            return []
        hits = process.extract(
            key, list(table.keys()), scorer=fuzz.ratio, score_cutoff=cutoff, limit=40
        )
        weights = {id(self._de_keys): self._de_weight, id(self._el_keys): self._el_weight,
                   id(self._en_keys): self._en_weight}[id(table)]
        # best score first; among equals the more common word
        hits.sort(key=lambda h: (-h[1], -weights.get(table[h[0]][0], 0)))
        return [(table[k][0], score, k) for k, score, _ in hits[:8]]

    # -- public --------------------------------------------------------------

    def lookup(self, query: str) -> Lookup:
        q = nz.clean(query)
        if not q:
            return Lookup(results=[])
        script = nz.detect_script(q)

        de_res = self.german_exact(q) if script in ("german", "latin") else []
        en_res, en_form = self.english_lookup(q) if script == "latin" else ([], None)

        if de_res and en_res:
            known = {(r.article, r.noun) for r in de_res}
            en_new = [r for r in en_res if (r.article, r.noun) not in known]
            if en_new:  # "house": a German noun (House, the music genre) and an English word
                for r in de_res:
                    r.from_word = "German"
                for r in en_new:
                    r.from_word = f"English “{en_form}”"
                german_first = q[:1].isupper()      # German nouns are capitalised
                order = (de_res, en_new) if german_first else (en_new, de_res)
                return Lookup(results=(order[0] + order[1])[:MAX_RESULTS],
                              input_lang="de" if german_first else "en")
        if de_res:
            return self._german_hit(de_res, q)
        if en_res:
            if len(en_res) == 1:
                en_res[0].gloss = None
            return Lookup(
                results=en_res,
                matched_from=en_form if en_form != " ".join(q.casefold().split()) else None,
                input_lang="en",
            )

        if script == "greek":
            greek_key = nz.greek_key(q)
            lemmas = self.greek_lemmas_exact(q) or self.greek_lemmas_by_key(greek_key)
        elif script == "latin":
            greek_key = nz.greeklish_key(q)
            lemmas = self.greek_lemmas_by_key(greek_key)
        else:
            greek_key, lemmas = "", []

        # homophones (παιδί / παΐδι / πέδη): most similar spelling first, then most common word
        lemmas.sort(key=lambda l: (
            Levenshtein.distance(q.casefold(), nz.literal_latin(l)) if script == "latin" else 0,
            -self._el_weight.get(l, 0),
        ))

        combined, used = [], []
        for lemma in lemmas:
            res = self.greek_to_german(lemma)
            if res:
                used.append(lemma)
                for r in res:
                    r.from_word = lemma
                combined += res
        if combined:
            results = _dedup(combined)
            if len(used) == 1:
                for r in results:
                    r.from_word = None
            return Lookup(
                results=results[:MAX_RESULTS],
                matched_from=used[0] if len(used) == 1 and used[0] != q else None,
            )

        return self._suggest(q, script, greek_key)

    def _suggest(self, q: str, script: str, greek_key: str) -> Lookup:
        cands = []  # (display word, score, kind, edit distance)
        if script in ("german", "latin"):
            qk = nz.german_key(q)
            for w, s, k in self._fuzzy(qk, self._de_keys):
                cands.append((w, s, "de", Levenshtein.distance(qk, k)))
        if script in ("greek", "latin"):
            cutoff = CROSS_SCRIPT_THRESHOLD if script == "latin" else SUGGEST_THRESHOLD
            for w, s, k in self._fuzzy(greek_key, self._el_keys, cutoff):
                cands.append((w, s, "el", Levenshtein.distance(greek_key, k)))

        if script == "latin" and not q[:1].isupper():   # "Baun" is German, "tre" may be English
            ql = " ".join(q.casefold().split())
            for w, s, k in self._fuzzy(ql, self._en_keys):
                cands.append((w, s, "en", Levenshtein.distance(ql, k)))

        def commonness(c):
            return {"de": self._de_weight, "el": self._el_weight, "en": self._en_weight}[c[2]].get(c[0], 0)

        # fewest edits first; then the more common word
        cands.sort(key=lambda c: (c[3], -commonness(c), -c[1]))
        ordered, seen = [], set()
        for c in cands:
            if c[0] not in seen:
                seen.add(c[0])
                ordered.append(c)
        if not ordered:
            return Lookup(results=[])

        best = ordered[0]
        rivals = [c for c in ordered[1:] if c[3] <= best[3]]
        runner_up = max([c[1] for c in ordered[1:]], default=0)
        # Auto-correct only when the winner is unambiguous: a single candidate
        # one edit away, or a near-identical key that clearly beats the rest.
        confident = not rivals and (
            best[3] <= 1 or (best[1] >= AUTO_THRESHOLD and best[1] - runner_up >= AUTO_MARGIN)
        )
        if confident:
            word, _, kind, _ = best
            near = [o[0] for o in ordered[1:5]]
            if kind == "en":
                res, _form = self.english_lookup(word)
                if res:
                    return Lookup(results=res, matched_from=word, suggestions=near, input_lang="en")
            else:
                res = self.german_exact(word) if kind == "de" else self.greek_to_german(word)
                if res and kind == "de":
                    return self._german_hit(res, q, suggestions=near)
                if res:
                    return Lookup(results=res, matched_from=word, suggestions=near)
        return Lookup(results=[], suggestions=[o[0] for o in ordered[:6]])
