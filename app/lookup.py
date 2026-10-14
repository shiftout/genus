"""Lookup pipeline: exact -> phonetic key -> fuzzy suggestion."""
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

# A fuzzy hit is applied automatically only when it is this good and
# clearly better than the runner-up; otherwise it is only suggested.
SUGGEST_THRESHOLD = 70
AUTO_THRESHOLD = 90      # similarity score, or a single edit (short words score low)
AUTO_MARGIN = 5
CROSS_SCRIPT_THRESHOLD = 78   # Latin input matched against Greek keys
MAX_RESULTS = 8


@dataclass
class Result:
    article: str
    noun: str
    plural: Optional[str] = None
    gloss: Optional[str] = None
    from_word: Optional[str] = None   # Greek lemma, shown when several words sound alike
    declension: Optional[dict] = None  # {"singular": [{case, article, forms}], "plural": [...]}
    alternatives: List[str] = field(default_factory=list)  # other articles, if several genders
    example: Optional[dict] = None     # {"text", "translation"} from Wiktionary

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
        self._load_keys()

    def _load_keys(self) -> None:
        for r in self.conn.execute("SELECT key, lemma, weight FROM de_noun"):
            self._de_keys.setdefault(r["key"], []).append(r["lemma"])
            self._de_weight[r["lemma"]] = max(self._de_weight.get(r["lemma"], 0), r["weight"])
        for r in self.conn.execute("SELECT DISTINCT key, lemma, weight FROM el_form"):
            self._el_keys.setdefault(r["key"], []).append(r["lemma"])
            self._el_weight[r["lemma"]] = r["weight"]
        for r in self.conn.execute(
            "SELECT g.gloss, MAX(n.weight) AS w FROM en_gloss g "
            "JOIN de_noun n ON n.id = g.noun_id GROUP BY g.gloss"
        ):
            self._en_keys[r["gloss"]] = [r["gloss"]]
            self._en_weight[r["gloss"]] = r["w"]

    # -- exact helpers -------------------------------------------------------

    def german_exact(self, word: str) -> List[Result]:
        rows = self.conn.execute(
            "SELECT * FROM de_noun WHERE lemma = ? COLLATE NOCASE ORDER BY id", (word,)
        ).fetchall()
        if not rows:  # an inflected form: Bäume -> Baum
            rows = self.conn.execute(
                "SELECT DISTINCT n.* FROM de_form f JOIN de_noun n ON n.id = f.noun_id "
                "WHERE f.form = ? COLLATE NOCASE ORDER BY n.id", (word,)
            ).fetchall()
        if not rows:  # umlaut / ß spelling variants
            rows = self.conn.execute(
                "SELECT * FROM de_noun WHERE key = ? ORDER BY id", (nz.german_key(word),)
            ).fetchall()
        return _noun_results(rows)

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
        """English word -> (results, the English form that matched)."""
        for form in _english_variants(word):
            rows = self.conn.execute(
                "SELECT n.* FROM en_gloss g JOIN de_noun n ON n.id = g.noun_id "
                "WHERE g.gloss = ? ORDER BY g.rank, n.id", (form,)
            ).fetchall()
            res, seen = [], set()
            for r in _noun_results(rows):
                if (r.article, r.noun) not in seen:
                    seen.add((r.article, r.noun))
                    res.append(r)
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
        rows = self.conn.execute(
            """
            SELECT DISTINCT s.gloss AS gloss, n.*
            FROM translation el
            JOIN translation de ON de.sense_id = el.sense_id AND de.lang = 'de'
            JOIN sense s ON s.id = el.sense_id
            JOIN de_noun n ON n.lemma = de.word
            WHERE el.lang = 'el' AND el.word = ?
            ORDER BY de.rank, el.sense_id, n.id
            """,
            (lemma,),
        ).fetchall()
        results, seen = [], set()
        for row in rows:
            for res in _noun_results([row], gloss=row["gloss"]):
                k = (res.article, res.noun)
                if k not in seen:
                    seen.add(k)
                    results.append(res)
        return results

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
            seen, results = set(), []
            for r in combined:
                if (r.article, r.noun) not in seen:
                    seen.add((r.article, r.noun))
                    results.append(r)
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
