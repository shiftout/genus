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
GERMAN = [("Baum", "m", "Bäume"), ("Haus", "n", "Häuser"), ("Tür", "f", "Türen"),
          ("See", "m,f", "Seen"), ("Straße", "f", "Straßen")]
# (english, gloss, [greek forms: first is the lemma], german)
SENSES = [
    ("tree", "woody plant", ["δέντρο", "δέντρα", "δένδρο"], ["Baum"]),
    ("house", "building", ["σπίτι", "σπίτια"], ["Haus"]),
    ("door", "entrance", ["πόρτα", "πόρτες"], ["Tür"]),
]


@pytest.fixture()
def dictionary():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    for lemma, genders, plural in GERMAN:
        decl = DECL.get(lemma)
        nid = conn.execute(
            "INSERT INTO de_noun(lemma, key, genders, plural, gloss, declension, weight) "
            "VALUES (?,?,?,?,?,?,?)",
            (lemma, nz.german_key(lemma), genders, plural, None,
             json.dumps(decl, ensure_ascii=False) if decl else None, 3.0),
        ).lastrowid
        for forms in (decl or {}).values():
            for variants in forms.values():
                for v in variants:
                    conn.execute("INSERT INTO de_form VALUES (?,?)", (v, nid))
        for gloss in ENGLISH.get(lemma, []):
            conn.execute("INSERT INTO en_gloss VALUES (?,?,0)", (gloss, nid))
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
