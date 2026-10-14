from scripts.build_db import pick_example

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
