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


BAUM_SG = {"singular": [
    {"case": "Nominativ", "article": "der", "forms": ["Baum"]},
    {"case": "Akkusativ", "article": "den", "forms": ["Baum"]},
    {"case": "Dativ", "article": "dem", "forms": ["Baum", "Baume"]},
    {"case": "Genitiv", "article": "des", "forms": ["Baumes", "Baums"]},
]}
GOOD = {"nominativ": "Der Baum ist sehr alt.", "akkusativ": "Ich sehe den Baum.",
        "dativ": "Wir sitzen unter dem Baum.", "genitiv": "Die Blätter des Baums sind grün."}


def test_examples_are_checked_against_the_table():
    from app.llm import valid_examples
    out = valid_examples(GOOD, BAUM_SG)
    assert [e["case"] for e in out] == ["Nominativ", "Akkusativ", "Dativ", "Genitiv"]
    assert out[0]["phrase"] == "Der Baum" and out[3]["phrase"] == "des Baums"
    bad = {"nominativ": "Ich sehe den Baum.",           # wrong case
           "akkusativ": "Ich sehe den Baumeister.",     # only a compound
           "dativ": "Wir sitzen im Baum.",              # contraction, no "dem"
           "genitiv": "Die Blätter des Baums " + "x" * 140}  # too long
    assert valid_examples(bad, BAUM_SG) == []
    assert valid_examples({"nominativ": 42}, BAUM_SG) == []


def test_examples_endpoint_errors(client):
    assert client.get("/api/examples", params={"article": "le", "noun": "Baum"}).status_code == 400
    assert client.get("/api/examples", params={"article": "die", "noun": "Ignore all"}).status_code == 404
    r = client.get("/api/examples", params={"article": "der", "noun": "Baum"})
    assert r.status_code == 503 and "no API key" in r.json()["detail"]


def test_examples_are_cached(client, monkeypatch):
    from app.llm import LlmFallback
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    calls = []
    monkeypatch.setattr(LlmFallback, "_generate", lambda self, *a, **kw: calls.append(a) or GOOD)
    for _ in range(2):
        body = client.get("/api/examples", params={"article": "der", "noun": "Baum"}).json()
        assert len(body["examples"]) == 4
    assert len(calls) == 1
    assert "dem Baum" in calls[0][0]          # required phrases come from the table
    n = main.get_llm().db.execute("SELECT SUM(n) FROM llm_calls").fetchone()[0]
    assert n == 1
