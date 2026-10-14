"""Free-tier Gemini fallback, used only when the dictionary has no answer.

Results are validated, cached, and capped per day so a bug or leaked URL
cannot burn through the free quota.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from typing import List, Optional

import httpx

from .lookup import Result
from .ratelimit import RateLimiter

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
ARTICLES = {"der", "die", "das"}

PROMPT = (
    "The user typed a single word that may be Greek, Greek written in Latin letters "
    "(Greeklish), English, or German, possibly misspelled. Identify the intended noun and give "
    "its German translation with the correct definite article (der/die/das) and the "
    "German plural. If the word is not a noun or you are not sure, set found=false. "
    "Word: {word!r}"
)

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "found": {"type": "BOOLEAN"},
        "article": {"type": "STRING", "enum": ["der", "die", "das"]},
        "noun": {"type": "STRING"},
        "plural": {"type": "STRING"},
        "meaning": {"type": "STRING"},
    },
    "required": ["found"],
}

_NOUN_RE = re.compile(r"^[A-ZÄÖÜ][a-zäöüßA-ZÄÖÜ-]{1,39}$")

EXAMPLES_PROMPT = (
    "Write one short, natural, everyday German sentence (at most 12 words) for each grammatical "
    "case of the noun “{article} {noun}” in the singular. Each sentence must contain the exact "
    "phrase given below, unchanged, with that definite article. Do not use contractions such as "
    "im, am, zum, zur, beim, vom or ins. Plain modern German, suitable for a learner.\n{required}"
)

EXAMPLES_KEY = "ex:"   # llm_cache prefix for example sentences
EXAMPLES_SCHEMA = {
    "type": "OBJECT",
    "properties": {c: {"type": "STRING"} for c in ("nominativ", "akkusativ", "dativ", "genitiv")},
    "required": ["nominativ", "akkusativ", "dativ", "genitiv"],
}
MAX_SENTENCE = 140


def valid_examples(data: dict, table: dict) -> List[dict]:
    """Keep only sentences that really contain the article + form for their case.

    The LLM writes the sentence; the dictionary decides whether its grammar is right.
    """
    out = []
    for row in table.get("singular", []):
        sentence = data.get(row["case"].casefold())
        if not isinstance(sentence, str):
            continue
        sentence = " ".join(sentence.split())
        if not sentence or len(sentence) > MAX_SENTENCE:
            continue
        for form in row["forms"]:
            phrase = f"{row['article']} {form}"
            m = re.search(r"\b%s\b" % re.escape(phrase), sentence, re.IGNORECASE)
            if m:
                out.append({"case": row["case"], "sentence": sentence, "phrase": m.group(0)})
                break
    return out


class LlmUnavailable(Exception):
    pass


class LlmFallback:
    def __init__(self, cache_path: str, daily_cap: int = 100, per_client_cap: int = 10):
        self.api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        self.model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        self.daily_cap = daily_cap
        # The app is public: one visitor must not be able to spend the whole daily budget.
        # In memory, so a restart forgives everyone; the global cap above is persistent.
        self.per_client = RateLimiter(limit=per_client_cap, window=24 * 3600)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(cache_path, check_same_thread=False)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS llm_cache (q TEXT PRIMARY KEY, payload TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS llm_calls (day TEXT PRIMARY KEY, n INTEGER);
            """
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    # -- cache ---------------------------------------------------------------

    def _cached(self, q: str) -> Optional[dict]:
        row = self.db.execute("SELECT payload FROM llm_cache WHERE q = ?", (q,)).fetchone()
        return json.loads(row[0]) if row else None

    def forget(self, q: str) -> None:
        """Drop a cached fallback answer ("Wrong?" button). Example sentences are off limits."""
        if q.casefold().startswith(EXAMPLES_KEY):
            return
        with self._lock:
            self.db.execute("DELETE FROM llm_cache WHERE q = ?", (q.casefold(),))
            self.db.commit()

    def _take_budget(self, client: str) -> None:
        """Charge one Gemini call to `client` and to today's global budget, or raise."""
        # Per-client first, so a client that is over its own limit can't drain the global one.
        if not self.per_client.allow(client):
            raise LlmUnavailable("your daily AI limit is reached, try again tomorrow")
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self._lock:
            row = self.db.execute("SELECT n FROM llm_calls WHERE day = ?", (day,)).fetchone()
            n = row[0] if row else 0
            if n >= self.daily_cap:
                raise LlmUnavailable("daily limit reached")
            self.db.execute(
                "INSERT INTO llm_calls(day, n) VALUES (?, 1) "
                "ON CONFLICT(day) DO UPDATE SET n = n + 1",
                (day,),
            )
            self.db.commit()

    def _store(self, q: str, payload) -> None:
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO llm_cache VALUES (?,?,?)", (q, json.dumps(payload), time.time())
            )
            self.db.commit()

    def _generate(self, prompt: str, schema: dict, max_tokens: int, temperature: float) -> dict:
        try:
            resp = httpx.post(
                API_URL.format(model=self.model),
                headers={"x-goog-api-key": self.api_key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": temperature,
                        "maxOutputTokens": max_tokens,
                        "responseMimeType": "application/json",
                        "responseSchema": schema,
                        # 2.5 models think by default and that eats the output budget
                        "thinkingConfig": {"thinkingBudget": 0},
                    },
                },
                timeout=15,
            )
            resp.raise_for_status()
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise LlmUnavailable(f"upstream error: {type(exc).__name__}") from exc

    # -- public --------------------------------------------------------------

    def examples(self, article: str, noun: str, table: dict, client: str = "unknown") -> List[dict]:
        """One example sentence per singular case, checked against the declension table.

        `article`/`noun` come from the dictionary, never from raw user input.
        """
        if not self.enabled:
            raise LlmUnavailable("no API key configured")
        q = f"{EXAMPLES_KEY}{article} {noun}".casefold()
        cached = self._cached(q)
        if cached is not None:
            return cached
        self._take_budget(client)

        required = "\n".join(
            f"- {row['case']}: must contain “{row['article']} {row['forms'][0]}”"
            for row in table.get("singular", [])
        )
        data = self._generate(
            EXAMPLES_PROMPT.format(article=article, noun=noun, required=required),
            EXAMPLES_SCHEMA, max_tokens=400, temperature=0.7,
        )
        examples = valid_examples(data, table)
        if examples:   # a total miss stays retryable
            self._store(q, examples)
        return examples

    def lookup(self, word: str, client: str = "unknown") -> List[Result]:
        if not self.enabled:
            raise LlmUnavailable("no API key configured")
        q = word.casefold()
        cached = self._cached(q)
        if cached is not None:
            return self._to_results(cached)
        self._take_budget(client)

        data = self._generate(PROMPT.format(word=word), SCHEMA, max_tokens=200, temperature=0)
        results = self._to_results(data)
        # Cache hits and clean misses alike, so repeated junk costs nothing.
        self._store(q, data if results else {"found": False})
        return results

    @staticmethod
    def _to_results(data: dict) -> List[Result]:
        if not data.get("found"):
            return []
        article, noun = data.get("article"), (data.get("noun") or "").strip()
        if article not in ARTICLES or not _NOUN_RE.match(noun):
            return []
        plural = (data.get("plural") or "").strip()
        return [
            Result(
                article=article,
                noun=noun,
                plural=plural if _NOUN_RE.match(plural) else None,
                gloss=(data.get("meaning") or "")[:80] or None,
            )
        ]
