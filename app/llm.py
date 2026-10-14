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


class LlmUnavailable(Exception):
    pass


class LlmFallback:
    def __init__(self, cache_path: str, daily_cap: int = 100):
        self.api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        self.model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        self.daily_cap = daily_cap
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
        with self._lock:
            self.db.execute("DELETE FROM llm_cache WHERE q = ?", (q.casefold(),))
            self.db.commit()

    def _take_budget(self) -> bool:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self._lock:
            row = self.db.execute("SELECT n FROM llm_calls WHERE day = ?", (day,)).fetchone()
            n = row[0] if row else 0
            if n >= self.daily_cap:
                return False
            self.db.execute(
                "INSERT INTO llm_calls(day, n) VALUES (?, 1) "
                "ON CONFLICT(day) DO UPDATE SET n = n + 1",
                (day,),
            )
            self.db.commit()
            return True

    # -- public --------------------------------------------------------------

    def lookup(self, word: str) -> List[Result]:
        if not self.enabled:
            raise LlmUnavailable("no API key configured")
        q = word.casefold()
        cached = self._cached(q)
        if cached is not None:
            return self._to_results(cached)
        if not self._take_budget():
            raise LlmUnavailable("daily limit reached")

        try:
            resp = httpx.post(
                API_URL.format(model=self.model),
                headers={"x-goog-api-key": self.api_key},
                json={
                    "contents": [{"parts": [{"text": PROMPT.format(word=word)}]}],
                    "generationConfig": {
                        "temperature": 0,
                        "maxOutputTokens": 200,
                        "responseMimeType": "application/json",
                        "responseSchema": SCHEMA,
                    },
                },
                timeout=15,
            )
            resp.raise_for_status()
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(text)
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise LlmUnavailable(f"upstream error: {type(exc).__name__}") from exc

        results = self._to_results(data)
        # Cache hits and clean misses alike, so repeated junk costs nothing.
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO llm_cache VALUES (?,?,?)",
                (q, json.dumps(data if results else {"found": False}), time.time()),
            )
            self.db.commit()
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
