import json
import sqlite3

import pytest

from app import db, normalize as nz
from app.lookup import Dictionary

DECL = {
    "Baum": {"singular": {"nominative": ["Baum"], "genitive": ["Baumes", "Baums"],
                          "dative": ["Baum", "Baume"], "accusative": ["Baum"]},
             "plural": {"nominative": ["Bäume"], "genitive": ["Bäume"],
                        "dative": ["Bäumen"], "accusative": ["Bäume"]}},
}
ENGLISH = {"Baum": ["tree"], "Haus": ["house", "home"], "Tür": ["door"]}
EXAMPLES = {"Baum": {"text": "Vögel nisten gern in Bäumen.", "translation": "Birds like to nest in trees."}}
GERMAN = [("Baum", "m", "Bäume"), ("Haus", "n", "Häuser"), ("Tür", "f", "Türen"),
          ("See", "m,f", "Seen"), ("Straße", "f", "Straßen")]
# (english, gloss, [greek forms: first is the lemma], german)
SENSES = [
    ("tree", "woody plant", ["δέντρο", "δέντρα", "δένδρο"], ["Baum"]),
    ("house", "building", ["σπίτι", "σπίτια"], ["Haus"]),
    ("door", "entrance", ["πόρτα", "πόρτες"], ["Tür"]),
    ("run", "move fast", ["τρέχω", "τρέχει", "έτρεξα"], ["laufen"]),
]
LAUFEN = {
    "infinitive": "laufen", "participle1": "laufend", "participle2": "gelaufen", "auxiliary": ["sein"],
    "subordinate": [],
    "tenses": {"present": [["laufe"], ["läufst"], ["läuft"], ["laufen"], ["lauft"], ["laufen"]],
               "preterite": [["lief"], ["liefst"], ["lief"], ["liefen"], ["lieft"], ["liefen"]],
               "imperative": [[], ["lauf", "laufe"], [], [], ["lauft"], []]},
}
ANFANGEN = {
    "infinitive": "anfangen", "participle2": "angefangen", "auxiliary": ["haben", "sein"],
    "subordinate": ["anfängt"],
    "tenses": {"present": [["fange an"], ["fängst an"], ["fängt an"], ["fangen an"], ["fangt an"],
                           ["fangen an"]]},
}
# (lemma, gloss, conjugation, English glosses, example)
VERBS = [
    ("laufen", "to walk; to run", LAUFEN, ["run", "walk"],
     {"text": "Lasst uns etwas schneller laufen.", "translation": "Let's move a little faster."}),
    ("anfangen", "to begin", ANFANGEN, ["begin", "start"], None),
    ("heißen", "to be called", None, ["be called"], None),
]


@pytest.fixture()
def dictionary():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    for lemma, genders, plural in GERMAN:
        decl = DECL.get(lemma)
        nid = conn.execute(
            "INSERT INTO de_noun(lemma, key, genders, plural, gloss, declension, weight, example) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (lemma, nz.german_key(lemma), genders, plural, None,
             json.dumps(decl, ensure_ascii=False) if decl else None, 3.0,
             json.dumps(EXAMPLES[lemma], ensure_ascii=False) if lemma in EXAMPLES else None),
        ).lastrowid
        for forms in (decl or {}).values():
            for variants in forms.values():
                for v in variants:
                    conn.execute("INSERT INTO de_form VALUES (?,?)", (v, nid))
        for gloss in ENGLISH.get(lemma, []):
            conn.execute("INSERT INTO en_gloss VALUES (?,?,0)", (gloss, nid))
    for lemma, gloss, conj, english, example in VERBS:
        vid = conn.execute(
            "INSERT INTO de_verb(lemma, key, gloss, conjugation, weight, example) VALUES (?,?,?,?,?,?)",
            (lemma, nz.german_key(lemma), gloss, json.dumps(conj, ensure_ascii=False) if conj else None,
             3.0, json.dumps(example, ensure_ascii=False) if example else None),
        ).lastrowid
        forms = {f for slots in (conj or {}).get("tenses", {}).values() for v in slots for f in v}
        for f in forms | set((conj or {}).get("subordinate", [])):
            conn.execute("INSERT INTO de_verb_form VALUES (?,?)", (f, vid))
        for i, g in enumerate(english):
            conn.execute("INSERT INTO en_verb_gloss VALUES (?,?,?)", (g, vid, i))
    for en, gloss, greek, german in SENSES:
        sid = conn.execute("INSERT INTO sense(en_lemma, gloss) VALUES (?,?)", (en, gloss)).lastrowid
        conn.execute("INSERT INTO translation VALUES (?,?,?,0)", (sid, "el", greek[0]))
        for g in german:
            conn.execute("INSERT INTO translation VALUES (?,?,?,0)", (sid, "de", g))
        for form in greek:
            conn.execute(
                "INSERT INTO el_form VALUES (?,?,?,?)", (form, nz.greek_key(form), greek[0], 3.0)
            )
    conn.commit()
    return Dictionary(conn)
