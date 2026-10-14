import sqlite3

import pytest

from app import db, normalize as nz
from app.lookup import Dictionary


def test_greek_greeklish_collide():
    keys = {nz.greek_key("δέντρο"), nz.greek_key("δενδρο"),
            nz.greeklish_key("dentro"), nz.greeklish_key("dendro")}
    assert len(keys) == 1


def test_german_key_variants():
    assert nz.german_key("Müller") == nz.german_key("Mueller") == nz.german_key("muller")
    assert nz.german_key("Straße") == nz.german_key("strasse")


def test_detect_script():
    assert nz.detect_script("δέντρο") == "greek"
    assert nz.detect_script("Tür") == "german"
    assert nz.detect_script("dentro") == "latin"


@pytest.mark.parametrize("query", ["δέντρο", "δεντρο", "δέντρα", "dentro", "dendro", "δενδρο"])
def test_greek_and_greeklish_to_baum(dictionary, query):
    out = dictionary.lookup(query)
    assert [(r.article, r.noun) for r in out.results] == [("der", "Baum")]


def test_german_exact(dictionary):
    assert dictionary.lookup("haus").results[0].article == "das"
    assert dictionary.lookup("Haus").results[0].noun == "Haus"


def test_german_umlaut_variants(dictionary):
    assert dictionary.lookup("Tuer").results[0].noun == "Tür"
    assert dictionary.lookup("strasse").results[0].noun == "Straße"


def test_multi_gender_returns_all(dictionary):
    r = dictionary.lookup("See").results[0]
    assert r.article == "der" and r.alternatives == ["die"]


def test_greek_typo_autocorrects(dictionary):
    out = dictionary.lookup("σπιτη")
    assert out.results and out.results[0].noun == "Haus"


def test_german_typo_autocorrects(dictionary):
    out = dictionary.lookup("Baun")
    assert out.results[0].noun == "Baum"
    assert out.matched_from == "Baum"


def test_nothing_found(dictionary):
    out = dictionary.lookup("qqqqzzzz")
    assert out.results == [] and out.suggestions == []


def test_empty_and_long_input(dictionary):
    assert dictionary.lookup("   ").results == []
    assert dictionary.lookup("x" * 500).results == []


def test_declension_table_order_and_articles(dictionary):
    r = dictionary.lookup("Baum").results[0]
    sg = r.declension["singular"]
    assert [row["case"] for row in sg] == ["Nominativ", "Akkusativ", "Dativ", "Genitiv"]
    assert [row["article"] for row in sg] == ["der", "den", "dem", "des"]
    assert sg[3]["forms"] == ["Baumes", "Baums"]
    pl = r.declension["plural"]
    assert [row["article"] for row in pl] == ["die", "die", "den", "der"]
    assert pl[2]["forms"] == ["Bäumen"]


def test_german_input_is_article_only(dictionary):
    out = dictionary.lookup("Baum")
    assert out.input_lang == "de"
    assert out.results[0].gloss is None and out.matched_from is None


def test_german_inflected_form_finds_lemma(dictionary):
    out = dictionary.lookup("Bäumen")
    assert [(r.article, r.noun) for r in out.results] == [("der", "Baum")]
    assert out.matched_from == "Baum" and out.input_lang == "de"


def test_greek_input_also_gets_declension(dictionary):
    r = dictionary.lookup("δέντρο").results[0]
    assert r.declension["singular"][1]["article"] == "den"
    assert dictionary.lookup("δέντρο").input_lang is None


@pytest.mark.parametrize("query", ["tree", "Tree", "the tree", "trees", "a tree"])
def test_english_input(dictionary, query):
    out = dictionary.lookup(query)
    assert [(r.article, r.noun) for r in out.results] == [("der", "Baum")]
    assert out.input_lang == "en"
    assert out.results[0].declension["singular"][1]["article"] == "den"


def test_english_typo_autocorrects(dictionary):
    out = dictionary.lookup("doro")
    assert out.results and out.results[0].noun == "Tür" and out.matched_from == "door"


def test_declension_for_llm_answer(dictionary):
    table = dictionary.declension_for("der", "Baum")
    assert table["plural"][2]["forms"] == ["Bäumen"]
    assert dictionary.declension_for("die", "Baum") is None   # wrong gender: no table
    assert dictionary.declension_for("der", "Nichtwort") is None


def test_word_that_is_german_and_english(dictionary):
    import sqlite3
    c = dictionary.conn
    nid = c.execute("INSERT INTO de_noun(lemma,key,genders,plural,gloss,declension,weight) "
                    "VALUES ('House','house','n',NULL,'house music',NULL,1)").lastrowid
    c.execute("INSERT INTO de_form VALUES ('House',?)", (nid,))
    lower = dictionary.lookup("house")
    assert [r.noun for r in lower.results] == ["Haus", "House"] and lower.input_lang == "en"
    upper = dictionary.lookup("House")
    assert [r.noun for r in upper.results] == ["House", "Haus"] and upper.input_lang == "de"


def test_wiktionary_example(dictionary):
    assert dictionary.lookup("Baum").results[0].example == {
        "text": "Vögel nisten gern in Bäumen.", "translation": "Birds like to nest in trees."}
    assert dictionary.lookup("Haus").results[0].example is None


def test_old_words_db_without_example_column():
    """A words.db built before the column existed must keep working (deploy order)."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    conn.execute("ALTER TABLE de_noun DROP COLUMN example")
    conn.execute("INSERT INTO de_noun(lemma, key, genders, plural, weight) VALUES ('Baum','baum','m','Bäume',3)")
    r = Dictionary(conn).lookup("Baum").results[0]
    assert (r.article, r.noun, r.example) == ("der", "Baum", None)
