"""SQLite schema shared by the build script, the app and the tests."""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS de_noun (
    id      INTEGER PRIMARY KEY,
    lemma   TEXT NOT NULL,
    key     TEXT NOT NULL,
    genders TEXT NOT NULL,          -- comma separated subset of m,f,n
    plural  TEXT,
    gloss   TEXT,
    declension TEXT,                -- JSON: {singular|plural: {case: [forms]}}
    weight  REAL NOT NULL DEFAULT 0   -- log10 of the Wiktionary entry size (commonness proxy)
);
CREATE INDEX IF NOT EXISTS de_noun_lemma ON de_noun(lemma);
CREATE INDEX IF NOT EXISTS de_noun_key   ON de_noun(key);

-- Every inflected form of a German noun -> its lemma row (Bäume -> Baum).
CREATE TABLE IF NOT EXISTS de_form (
    form    TEXT NOT NULL,
    noun_id INTEGER NOT NULL REFERENCES de_noun(id)
);
CREATE INDEX IF NOT EXISTS de_form_form ON de_form(form);

-- Every Greek surface form (lemma or inflected) that leads to a lemma
-- which has at least one German translation.
CREATE TABLE IF NOT EXISTS el_form (
    form  TEXT NOT NULL,
    key   TEXT NOT NULL,
    lemma TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 0   -- log10 of the Wiktionary entry size (commonness proxy)
);
CREATE INDEX IF NOT EXISTS el_form_form ON el_form(form);
CREATE INDEX IF NOT EXISTS el_form_key  ON el_form(key);

-- One row per sense of an English entry ("tree": woody plant ...);
-- Greek and German words are joined through it.
CREATE TABLE IF NOT EXISTS sense (
    id       INTEGER PRIMARY KEY,
    en_lemma TEXT NOT NULL,
    gloss    TEXT
);
CREATE TABLE IF NOT EXISTS translation (
    sense_id INTEGER NOT NULL REFERENCES sense(id),
    lang     TEXT NOT NULL,         -- 'de' | 'el'
    word     TEXT NOT NULL,
    rank     REAL NOT NULL DEFAULT 0      -- lower = better match
);
CREATE INDEX IF NOT EXISTS translation_word  ON translation(lang, word);
CREATE INDEX IF NOT EXISTS translation_sense ON translation(sense_id, lang);

-- English word (as used in German entries' glosses) -> German noun.
CREATE TABLE IF NOT EXISTS en_gloss (
    gloss   TEXT NOT NULL,
    noun_id INTEGER NOT NULL REFERENCES de_noun(id),
    rank    REAL NOT NULL                -- lower = better
);
CREATE INDEX IF NOT EXISTS en_gloss_gloss ON en_gloss(gloss);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def connect(path: str, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    else:
        conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
