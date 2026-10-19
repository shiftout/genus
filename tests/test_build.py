from scripts.build_db import all_glosses, conjugated_forms, gloss_keys, parse_conjugation, pick_example

DECL = {"singular": {"dative": ["Baum", "Baume"]}, "plural": {"dative": ["Bäumen"]}}


def entry(*senses):
    return {"word": "Baum", "senses": list(senses)}


def ex(text, translation="a translation", type_="example"):
    return {"text": text, "translation": translation, "type": type_}


def test_takes_first_good_example_including_declined_forms():
    e = entry({"glosses": ["tree"], "examples": [ex("Baum!"),   # too short
                                                 ex("Vögel nisten gern in Bäumen."),
                                                 ex("Der Baum ist alt.")]})
    assert pick_example(e, DECL) == {"text": "Vögel nisten gern in Bäumen.", "translation": "a translation"}


def test_skips_quotations_untranslated_and_unrelated():
    e = entry({"glosses": ["tree"], "examples": [
        ex("Der Baum stand im alten Garten.", type_="quotation"),
        ex("Der Baum stand im alten Garten.", translation="(please add an English translation)"),
        ex("Der Baum stand im alten Garten.", translation=""),
        ex("Die Vögel singen heute laut."),            # doesn't mention the noun
        ex("Ein Baumhaus ist kein Haus."),             # only as part of a compound
        ex("Baum fällen lassen"),                      # set phrase, not a sentence
        ex("sich hinter einem Baum verstecken."),      # infinitive phrase
    ]})
    assert pick_example(e, DECL) is None


def test_skips_marked_senses():
    e = entry({"glosses": ["idiot"], "tags": ["colloquial"], "examples": [ex("Du bist ein Baum, echt.")]})
    assert pick_example(e, DECL) is None


def form(text, *tags, source="conjugation"):
    return {"form": text, "tags": list(tags), "source": source}


ANFANGEN = {"word": "anfangen", "forms": [
    form("fängt an", "present", "singular", "third-person", source=None),   # head line, not the table
    form("haben", "auxiliary", source=None),
    form("sein", "Northwest-German", "auxiliary", "colloquial", source=None),
    form("haben or sein", "auxiliary"),                                       # joined, untagged
    form("sein", "Northwest-German", "auxiliary", "colloquial"),
    form("angefangen", "participle", "past"),
    form("anzufangen", "infinitive", "infinitive-zu"),
    form("fängt an", "indicative", "present", "singular", "third-person"),
    form("anfängt", "indicative", "present", "singular", "subordinate-clause", "third-person"),
    form("fing an", "indicative", "preterite", "singular", "third-person"),
    form("finge an", "formal", "rare", "singular", "subjunctive", "subjunctive-ii", "third-person"),
    form("ist angefangen", "indicative", "multiword-construction", "perfect", "singular", "third-person"),
    form("würde anfangen", "future", "future-i", "multiword-construction", "singular",
         "subjunctive", "subjunctive-ii", "third-person"),
    form("fang an", "imperative", "second-person", "singular"),
]}


def test_parse_conjugation():
    c = parse_conjugation(ANFANGEN)
    assert c["auxiliary"] == ["haben"]                 # colloquial "sein" dropped
    assert c["participle2"] == "angefangen" and c["zu"] == "anzufangen"
    assert c["tenses"]["present"][2] == ["fängt an"]   # main clause only
    assert c["subordinate"] == ["anfängt"]
    assert c["tenses"]["preterite"][2] == ["fing an"]
    assert c["tenses"]["subjunctive-ii"][2] == ["finge an"]   # rare, but the only form there is
    assert c["tenses"]["imperative"][1] == ["fang an"]
    assert "ist angefangen" not in conjugated_forms(c)   # compound tenses are built by the app
    assert {"anfangen", "fängt an", "anfängt", "angefangen"} <= set(conjugated_forms(c))


def test_parse_conjugation_needs_a_present_tense():
    assert parse_conjugation({"word": "ässe", "forms": []}) is None


def test_verb_glosses():
    e = {"word": "sein", "senses": [
        {"glosses": ["forms the present perfect"], "tags": ["auxiliary"]},
        {"glosses": ["As a copulative verb:", "to be"], "tags": ["copulative"]},
    ]}
    assert all_glosses(e) == ["to be", "forms the present perfect"]
    assert gloss_keys(all_glosses(e), verb=True)[0] == "be"

