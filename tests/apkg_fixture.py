"""Build minimal .apkg files with sqlite3 for tests (legacy anki2 and schema-18 anki21b)."""
import json
import sqlite3
import zipfile
from pathlib import Path

import zstandard

BASIC_MID, CLOZE_MID = 1001, 1002
DECK_ID = 1
BASIC_NID, CLOZE_NID = 2001, 2002
BASIC_FRONT = "<b>bonjour</b>[sound:bonjour.mp3]"
BASIC_BACK = "hello&nbsp;<br>a greeting"
CLOZE_TEXT = "Paris is the capital of {{c1::France::country}}, Berlin of {{c2::Germany}}."
CLOZE_EXTRA = 'Europe <img src="map.png">'
AUDIO = b"ID3fake-mp3-bytes-for-tests"
IMAGE = b"\x89PNG\r\n\x1a\nfake-png"
BASE_MS = 1767225600000  # 2026-01-01T00:00:00Z
DAY = 86400000

# (revlog id, card id, ease, ivl, time ms, type)
REVLOG = [
    (BASE_MS, 3001, 3, -600, 4000, 0),
    (BASE_MS + 600000, 3001, 3, 1, 3500, 0),
    (BASE_MS + 2 * DAY, 3001, 3, 8, 2500, 1),
    (BASE_MS + 3 * DAY, 3001, 0, 5, 0, 4),  # manual reschedule: not a review
    (BASE_MS + DAY, 3002, 1, -60, 9000, 0),
    (BASE_MS + DAY + 60000, 3002, 3, -600, 5000, 0),
]
# (id, nid, did, ord, type, queue, due)
CARDS = [
    (3001, BASIC_NID, DECK_ID, 0, 2, 2, 20),
    (3002, CLOZE_NID, DECK_ID, 0, 1, 1, 0),
    (3003, CLOZE_NID, DECK_ID, 1, 0, 0, 7),
]


def _common(conn, card_decks=None):
    conn.executescript("""
        CREATE TABLE notes (id integer primary key, guid text, mid integer, mod integer, usn integer, tags text,
                            flds text, sfld integer, csum integer, flags integer, data text);
        CREATE TABLE cards (id integer primary key, nid integer, did integer, ord integer, mod integer, usn integer,
                            type integer, queue integer, due integer, ivl integer, factor integer, reps integer,
                            lapses integer, left integer, odue integer, odid integer, flags integer, data text);
        CREATE TABLE revlog (id integer primary key, cid integer, usn integer, ease integer, ivl integer,
                             lastIvl integer, factor integer, time integer, type integer);
    """)
    conn.execute("INSERT INTO notes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (BASIC_NID, "g1", BASIC_MID, 0, 0, " vocab ", "\x1f".join([BASIC_FRONT, BASIC_BACK]), 0, 0, 0, ""))
    conn.execute("INSERT INTO notes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (CLOZE_NID, "g2", CLOZE_MID, 0, 0, "", "\x1f".join([CLOZE_TEXT, CLOZE_EXTRA]), 0, 0, 0, ""))
    for cid, nid, did, ord_, type_, queue, due in CARDS:
        where = (card_decks or {}).get(cid, did)
        did, odid = where if isinstance(where, tuple) else (where, 0)
        conn.execute("INSERT INTO cards VALUES (?,?,?,?,0,0,?,?,?,0,0,0,0,0,0,?,0,'')", (cid, nid, did, ord_, type_, queue, due, odid))
    for rid, cid, ease, ivl, ms, type_ in REVLOG:
        conn.execute("INSERT INTO revlog VALUES (?,?,0,?,?,0,0,?,?)", (rid, cid, ease, ivl, ms, type_))


BASIC_TMPL = ("Card 1", "{{Front}}", "{{FrontSide}}<hr id=answer>{{Back}}")
CLOZE_TMPL = ("Cloze", "{{cloze:Text}}", "{{cloze:Text}}<br>{{Back Extra}}")


def legacy_db(path: Path, decks=None, card_decks=None) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE col (id integer primary key, crt integer, mod integer, scm integer, ver integer, dty integer,"
                 " usn integer, ls integer, conf text, models text, decks text, dconf text, tags text)")
    models = {
        str(BASIC_MID): {"name": "Basic", "type": 0, "flds": [{"name": "Front", "ord": 0}, {"name": "Back", "ord": 1}],
                         "tmpls": [{"name": BASIC_TMPL[0], "ord": 0, "qfmt": BASIC_TMPL[1], "afmt": BASIC_TMPL[2]}]},
        str(CLOZE_MID): {"name": "Cloze", "type": 1, "flds": [{"name": "Text", "ord": 0}, {"name": "Back Extra", "ord": 1}],
                         "tmpls": [{"name": CLOZE_TMPL[0], "ord": 0, "qfmt": CLOZE_TMPL[1], "afmt": CLOZE_TMPL[2]}]},
    }
    decks = {str(did): {"name": name} for did, name in (decks or {DECK_ID: "French"}).items()}
    conn.execute("INSERT INTO col VALUES (1,0,0,0,11,0,0,0,'{}',?,?,'{}','{}')", (json.dumps(models), json.dumps(decks)))
    _common(conn, card_decks)
    conn.commit()
    conn.close()


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def pb_varint(field: int, value: int) -> bytes:
    return _varint(field << 3) + _varint(value)


def pb_bytes(field: int, value: bytes | str) -> bytes:
    data = value.encode() if isinstance(value, str) else value
    return _varint(field << 3 | 2) + _varint(len(data)) + data


def schema18_db(path: Path, decks=None, card_decks=None) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE col (id integer primary key, crt integer, mod integer, scm integer, ver integer, dty integer,
                          usn integer, ls integer, conf text, models text, decks text, dconf text, tags text);
        CREATE TABLE notetypes (id integer primary key, name text, mtime_secs integer, usn integer, config blob);
        CREATE TABLE fields (ntid integer, ord integer, name text, config blob);
        CREATE TABLE templates (ntid integer, ord integer, name text, mtime_secs integer, usn integer, config blob);
        CREATE TABLE decks (id integer primary key, name text, mtime_secs integer, usn integer, common blob, kind blob);
    """)
    conn.execute("INSERT INTO col VALUES (1,0,0,0,18,0,0,0,'','','','','')")
    for mid, name, kind, fields, tmpl in ((BASIC_MID, "Basic", 0, ["Front", "Back"], BASIC_TMPL),
                                          (CLOZE_MID, "Cloze", 1, ["Text", "Back Extra"], CLOZE_TMPL)):
        config = pb_varint(1, kind) + pb_bytes(3, ".card {}")
        conn.execute("INSERT INTO notetypes VALUES (?,?,0,0,?)", (mid, name, config))
        for i, f in enumerate(fields):
            conn.execute("INSERT INTO fields VALUES (?,?,?,?)", (mid, i, f, b""))
        conn.execute("INSERT INTO templates VALUES (?,?,?,0,0,?)",
                     (mid, 0, tmpl[0], pb_bytes(1, tmpl[1]) + pb_bytes(2, tmpl[2])))
    for did, name in (decks or {DECK_ID: "Languages::French"}).items():
        conn.execute("INSERT INTO decks VALUES (?,?,0,0,x'',x'')", (did, name.replace("::", "\x1f")))
    _common(conn, card_decks)
    conn.commit()
    conn.close()


def build(tmp: Path, fmt: str = "legacy", decks: dict[int, str] | None = None,
          card_decks: dict[int, int | tuple[int, int]] | None = None) -> Path:
    """decks: Anki deck id -> full name (``A::B``); card_decks: card id -> deck id, or (filtered deck id, home deck id)."""
    db = tmp / f"collection-{fmt}.db"
    out = tmp / f"deck-{fmt}.apkg"
    zstd = zstandard.ZstdCompressor()
    with zipfile.ZipFile(out, "w") as zf:
        if fmt == "legacy":
            legacy_db(db, decks, card_decks)
            zf.write(db, "collection.anki2")
            zf.writestr("media", json.dumps({"0": "bonjour.mp3", "1": "map.png"}))
            zf.writestr("0", AUDIO)
            zf.writestr("1", IMAGE)
        else:
            schema18_db(db, decks, card_decks)
            legacy_db(tmp / "dummy.db")  # new exports also carry a placeholder legacy collection
            zf.write(tmp / "dummy.db", "collection.anki2")
            zf.writestr("collection.anki21b", zstd.compress(db.read_bytes()))
            entries = b"".join(pb_bytes(1, pb_bytes(1, name) + pb_varint(2, len(data)))
                               for name, data in (("bonjour.mp3", AUDIO), ("map.png", IMAGE)))
            zf.writestr("media", zstd.compress(entries))
            zf.writestr("0", zstd.compress(AUDIO))
            zf.writestr("1", zstd.compress(IMAGE))
            zf.writestr("meta", b"\x08\x03")
    return out
