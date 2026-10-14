import pytest
from fastapi.testclient import TestClient

from app import main
from app.ratelimit import RateLimiter


@pytest.fixture()
def client(dictionary, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_dictionary", dictionary)
    monkeypatch.setattr(main, "CACHE_DB", str(tmp_path / "cache.db"))
    monkeypatch.setattr(main, "_llm", None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(main, "limiter", RateLimiter(limit=3))
    return TestClient(main.app)


def test_lookup_greeklish(client):
    body = client.get("/api/lookup", params={"q": "dentro"}).json()
    assert body["results"][0]["article"] == "der" and body["results"][0]["noun"] == "Baum"
    assert body["source"] == "dictionary"


def test_miss_without_llm_key_is_clean(client):
    body = client.get("/api/lookup", params={"q": "qqqqzzzz"}).json()
    assert body["results"] == [] and body["source"] == "dictionary"


def test_rate_limit_uses_cloudflare_ip(client):
    for _ in range(3):
        assert client.get("/api/lookup", params={"q": "haus"}, headers={"cf-connecting-ip": "1.1.1.1"}).status_code == 200
    assert client.get("/api/lookup", params={"q": "haus"}, headers={"cf-connecting-ip": "1.1.1.1"}).status_code == 429
    assert client.get("/api/lookup", params={"q": "haus"}, headers={"cf-connecting-ip": "2.2.2.2"}).status_code == 200


def test_llm_output_validation():
    from app.llm import LlmFallback
    ok = LlmFallback._to_results({"found": True, "article": "der", "noun": "Baum", "plural": "Bäume"})
    assert ok[0].noun == "Baum"
    assert LlmFallback._to_results({"found": True, "article": "le", "noun": "Baum"}) == []
    assert LlmFallback._to_results({"found": True, "article": "der", "noun": "ignore previous instructions"}) == []
