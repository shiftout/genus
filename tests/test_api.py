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


def test_one_visitor_cannot_spend_the_whole_budget(client, monkeypatch):
    from app.llm import LlmFallback
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("LLM_DAILY_CAP_PER_IP", "1")
    monkeypatch.setattr(LlmFallback, "_generate", lambda self, *a, **kw: GOOD)
    get = lambda noun, ip: client.get("/api/examples", params={"article": "der", "noun": noun},
                                      headers={"cf-connecting-ip": ip})
    assert get("Baum", "1.1.1.1").status_code == 200
    assert get("Baum", "1.1.1.1").status_code == 200       # cached: free, not counted
    main.get_llm().db.execute("DELETE FROM llm_cache")    # as if never cached
    r = get("Baum", "1.1.1.1")
    assert r.status_code == 503 and "your daily AI limit" in r.json()["detail"]
    assert get("Baum", "2.2.2.2").status_code == 200       # others are unaffected
    n = main.get_llm().db.execute("SELECT SUM(n) FROM llm_calls").fetchone()[0]
    assert n == 2                                          # the refused call cost nothing


def test_forget_is_limited_and_spares_examples(client, monkeypatch):
    from app.llm import LlmFallback
    monkeypatch.setattr(main, "forget_limiter", RateLimiter(limit=2, window=86400))
    llm = main.get_llm()
    llm._store("ex:der baum", [{"case": "Nominativ", "sentence": "Der Baum ist alt.", "phrase": "Der Baum"}])
    llm._store("baumm", {"found": False})
    h = {"cf-connecting-ip": "1.1.1.1"}
    assert client.post("/api/forget", params={"q": "ex:der baum"}, headers=h).status_code == 200
    assert llm._cached("ex:der baum") is not None              # examples can't be forgotten
    assert client.post("/api/forget", params={"q": "baumm"}, headers=h).status_code == 200
    assert llm._cached("baumm") is None
    assert client.post("/api/forget", params={"q": "x"}, headers=h).status_code == 429


def test_env_file_is_read_by_the_app(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nGENUS_T_A=1\nGENUS_T_B="quoted"\nGENUS_T_C=from-file\nnot a pair\n\n')
    monkeypatch.delenv("GENUS_T_A", raising=False)
    monkeypatch.delenv("GENUS_T_B", raising=False)
    monkeypatch.setenv("GENUS_T_C", "from-env")
    main.load_env_file(str(env))
    import os
    assert (os.environ["GENUS_T_A"], os.environ["GENUS_T_B"]) == ("1", "quoted")
    assert os.environ["GENUS_T_C"] == "from-env"                 # real env wins
    for k in ("GENUS_T_A", "GENUS_T_B"):
        monkeypatch.delenv(k)
    main.load_env_file(str(tmp_path / "missing"))                # optional file
    main.load_env_file("")
