from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db
from . import normalize as nz
from .llm import LlmFallback, LlmUnavailable
from .lookup import Dictionary
from .ratelimit import RateLimiter

ROOT = Path(__file__).resolve().parent.parent
WORDS_DB = os.environ.get("WORDS_DB", str(ROOT / "data" / "words.db"))
CACHE_DB = os.environ.get("CACHE_DB", str(ROOT / "data" / "cache.db"))

app = FastAPI(title="Genus", docs_url=None, redoc_url=None, openapi_url=None)
limiter = RateLimiter(limit=int(os.environ.get("RATE_LIMIT_PER_MIN", "30")))
# "Wrong?" is rare for a real user; wiping the cache in a loop would force fresh Gemini calls.
forget_limiter = RateLimiter(limit=5, window=24 * 3600)

_dictionary = None
_llm = None


def get_dictionary() -> Dictionary:
    global _dictionary
    if _dictionary is None:
        _dictionary = Dictionary(db.connect(WORDS_DB, readonly=True))
    return _dictionary


def get_llm() -> LlmFallback:
    global _llm
    if _llm is None:
        _llm = LlmFallback(CACHE_DB, int(os.environ.get("LLM_DAILY_CAP", "100")),
                           int(os.environ.get("LLM_DAILY_CAP_PER_IP", "10")))
    return _llm


def client_ip(request: Request) -> str:
    # Behind the Cloudflare tunnel every connection comes from cloudflared.
    return request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else "unknown"
    )


@app.get("/api/lookup")
def lookup(request: Request, q: str = Query(..., max_length=200)):
    if not limiter.allow(client_ip(request)):
        raise HTTPException(429, "Too many requests, slow down.")
    word = nz.clean(q)
    out = get_dictionary().lookup(word)
    if out.results or not word:
        return out.as_dict()

    # Dictionary has nothing (not even a confident correction): ask the LLM.
    try:
        results = get_llm().lookup(word, client_ip(request))
    except LlmUnavailable:
        results = []
    if results:
        for r in results:
            r.declension = get_dictionary().declension_for(r.article, r.noun)
        out.results, out.source = results, "llm"
    return out.as_dict()


@app.get("/api/examples")
def examples(request: Request, article: str = Query(..., max_length=3),
             noun: str = Query(..., max_length=60)):
    """'Beispiele' button: one LLM sentence per case, only for nouns in the dictionary."""
    if not limiter.allow(client_ip(request)):
        raise HTTPException(429, "Too many requests, slow down.")
    if article not in ("der", "die", "das"):
        raise HTTPException(400, "Unknown article.")
    table = get_dictionary().declension_for(article, noun)
    if not table or not table.get("singular"):
        raise HTTPException(404, "No declension table for this noun.")
    try:
        return {"examples": get_llm().examples(article, noun, table, client_ip(request))}
    except LlmUnavailable as exc:
        raise HTTPException(503, f"Examples unavailable: {exc}.")


@app.post("/api/forget")
def forget(request: Request, q: str = Query(..., max_length=200)):
    """'This answer is wrong' button: drop a cached LLM answer."""
    ip = client_ip(request)
    if not limiter.allow(ip) or not forget_limiter.allow(ip):
        raise HTTPException(429, "Too many requests, slow down.")
    get_llm().forget(nz.clean(q))
    return {"ok": True}


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
