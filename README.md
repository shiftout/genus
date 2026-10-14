# Genus

Type a Greek word, Greek in Latin letters (`dentro`), an English word or a German word and get the
German noun with its article (`der Baum`). Typos get a "did you mean".

Lookup order: offline Wiktionary dictionary → fuzzy/phonetic match → free-tier
Gemini fallback (optional, cached, capped per day).

## Develop

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest
.venv/bin/python scripts/build_db.py --download      # builds data/words.db (~5 GB download, once)
.venv/bin/uvicorn app.main:app --reload
```

## Deploy on the QNAP

Same pipeline as hoi/dentro/memento: GitHub Actions + self-hosted runner on the
NAS, container `genus` on the shared `memento-edge` network, tunnel origin
`genus:8000`, hostname `genus.mysynnefo.com`, public (no Cloudflare Access).
Full steps and checklist in [DEPLOY.md](DEPLOY.md).

## Gemini key

- Create the key in Google AI Studio; on the NAS it lives in `/share/Container/genus/.env`, never in git.
- Rotate: create new key → edit that `.env` → `docker restart genus` → delete the old key (DEPLOY.md).
- Without a key the app runs dictionary-only.
- Use a **free-tier key with no billing** on its Google Cloud project: abuse then means
  429s until tomorrow, never a bill.
- `LLM_DAILY_CAP` limits calls per day, `LLM_DAILY_CAP_PER_IP` per visitor; answers are
  cached in `cache.db` next to `words.db`, and cache hits cost nothing.
  The model name in `.env.example` is a default — check AI Studio for the current free-tier model.

## Rebuilding the dictionary

`scripts/build_db.py` reads the kaikki.org Wiktionary extracts (English, German,
Greek editions' JSONL) and writes `data/words.db`. Greek↔German pairs are joined
through the translation tables of the English entries ("tree" → de: Baum, el: δέντρο).

Dictionary data © Wiktionary contributors, CC BY-SA 4.0, via kaikki.org.
