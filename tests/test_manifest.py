import copy
import hashlib
import json
from pathlib import Path

import pytest

from cubercard import manifest as mf

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def card(key="k1", **extra):
    c = {"external_key": key, "term": "lucid", "definition": "清晰的", "context": "A lucid explanation.", "source": "test",
         "front": [{"type": "title", "text": "lucid"}], "back": [{"type": "translation", "text": "清晰的"}]}
    c.update(extra)
    return c


def manifest(*cards, batch_id="b1", deck_id="deck"):
    return {"batch_id": batch_id, "deck_id": deck_id, "cards": list(cards)}


@pytest.mark.parametrize("field", ["external_key", "term", "definition", "context", "source", "front", "back"])
def test_required_card_fields(field):
    c = card()
    del c[field]
    with pytest.raises(mf.ManifestError, match=field):
        mf.prepare(manifest(c), None)


@pytest.mark.parametrize("field,value", [("batch_id", ""), ("deck_id", "Bad Deck"), ("cards", [])])
def test_required_manifest_fields(field, value):
    m = manifest(card())
    m[field] = value
    with pytest.raises(mf.ManifestError):
        mf.prepare(m, None)


def test_rejects_unknown_fields_and_duplicates():
    with pytest.raises(mf.ManifestError, match="unknown field"):
        mf.prepare(manifest(card(termm="x")), None)
    with pytest.raises(mf.ManifestError, match="duplicate external_key"):
        mf.prepare(manifest(card(), card()), None)
    with pytest.raises(mf.ManifestError, match="unsupported block type"):
        mf.prepare(manifest(card(front=[{"type": "html", "text": "<b>x</b>"}])), None)


def test_history_and_state_rules():
    good = card(history=[{"rating": 3, "reviewed_at": "2026-01-01T00:00:00Z", "duration_ms": 1000}],
                state={"queue": "review", "due": "2026-01-05T00:00:00Z", "stability": 4.0, "difficulty": 5.0,
                       "step": 0, "reps": 1, "lapses": 0, "last_review": "2026-01-01T00:00:00Z", "state_version": 0})
    prepared = mf.prepare(manifest(good), None)
    assert "state_version" not in prepared.cards[0]["state"]
    for bad in ({"rating": 5, "reviewed_at": "2026-01-01T00:00:00Z", "duration_ms": 1},
                {"rating": 3, "reviewed_at": "2026-01-01T00:00:00+08:00", "duration_ms": 1},
                {"rating": 3, "reviewed_at": "2099-01-01T00:00:00Z", "duration_ms": 1}):
        with pytest.raises(mf.ManifestError, match="history"):
            mf.prepare(manifest(card(history=[bad])), None)
    with pytest.raises(mf.ManifestError, match="due"):
        mf.prepare(manifest(card(state={"queue": "review", "stability": 1, "difficulty": 5})), None)
    with pytest.raises(mf.ManifestError, match="new cards"):
        mf.prepare(manifest(card(state={"queue": "new", "due": "2026-01-05T00:00:00Z"})), None)


CLOZE = "Paris is the capital of {{c1::France::國家}}, Berlin of {{c2::Germany}}."


def cloze_note(key="geo:capitals"):
    return {"external_key": key, "kind": "cloze", "source": "test",
            "front": [{"type": "cloze", "text": CLOZE}],
            "back": [{"type": "cloze", "text": CLOZE}, {"type": "text", "text": "extra"}]}


def test_cloze_note_expands_to_one_card_per_number():
    prepared = mf.prepare(manifest(cloze_note()), None)
    assert [c["external_key"] for c in prepared.cards] == ["geo:capitals:c1", "geo:capitals:c2"]
    c1, c2 = prepared.cards
    assert c1["front"] == [{"type": "cloze", "text": CLOZE, "index": 1}]
    assert c1["back"][0] == c1["front"][0] and c2["back"][0]["index"] == 2
    assert (c1["term"], c1["definition"]) == ("France", "國家")
    assert (c2["term"], c2["definition"]) == ("Germany", "Germany")
    assert c1["context"] == "Paris is the capital of France, Berlin of Germany."


def test_explicit_cloze_card_rules():
    explicit = cloze_note("geo:x:c1")
    for side in ("front", "back"):
        explicit[side][0]["index"] = 1
    explicit.update(term="France", definition="法國", context="ctx")
    assert len(mf.prepare(manifest(explicit), None).cards) == 1
    wrong_suffix = copy.deepcopy(explicit)
    wrong_suffix["external_key"] = "geo:x:c2"
    with pytest.raises(mf.ManifestError, match='must end with ":c1"'):
        mf.prepare(manifest(wrong_suffix), None)
    mismatch = copy.deepcopy(explicit)
    mismatch["back"][0]["index"] = 2
    with pytest.raises(mf.ManifestError, match="repeat the front"):
        mf.prepare(manifest(mismatch), None)
    with pytest.raises(mf.ManifestError, match="no \\{\\{c3"):
        bad = copy.deepcopy(explicit)
        bad["front"][0]["index"] = 3
        mf.prepare(manifest(bad), None)
    with pytest.raises(mf.ManifestError, match='require kind "cloze"'):
        mf.prepare(manifest(card(front=[{"type": "cloze", "text": CLOZE, "index": 1}])), None)


def test_split_batches_500():
    cards = [card(f"k{i:04d}") for i in range(1201)]
    batches = mf.split_batches(mf.prepare(manifest(*cards), None))
    assert [len(b["cards"]) for b in batches] == [500, 500, 201]
    assert [b["batch_id"] for b in batches] == ["b1:part001", "b1:part002", "b1:part003"]
    assert sum((b["cards"] for b in batches), []) == mf.prepare(manifest(*cards), None).cards
    # Deterministic: the same manifest splits the same way (rerun = replay of each part).
    assert batches == mf.split_batches(mf.prepare(manifest(*cards), None))
    single = mf.split_batches(mf.prepare(manifest(*cards[:500]), None))
    assert len(single) == 1 and single[0]["batch_id"] == "b1"


def test_split_batches_by_size():
    big = "x" * 300_000
    cards = [card(f"k{i}", context=big) for i in range(10)]
    batches = mf.split_batches(mf.prepare(manifest(*cards), None))
    assert len(batches) > 1
    for b in batches:
        assert len(json.dumps(b, ensure_ascii=False).encode()) <= mf.MAX_BATCH_BYTES


def test_media_filename_is_hashed_and_sent_as_sha256(tmp_path):
    clip = tmp_path / "clip.mp3"
    clip.write_bytes(b"fake mp3")
    digest = hashlib.sha256(b"fake mp3").hexdigest()
    m = manifest(card(front=[{"type": "audio", "filename": "clip.mp3", "loudness_lufs": -16.1}]))
    prepared = mf.prepare(m, tmp_path)
    assert prepared.cards[0]["front"] == [{"type": "audio", "sha256": digest, "loudness_lufs": -16.1}]
    assert prepared.media[digest].mime == "audio/mpeg" and prepared.media[digest].size == 8
    m["cards"][0]["front"][0]["sha256"] = "0" * 64
    with pytest.raises(mf.ManifestError, match="manifest says"):
        mf.prepare(m, tmp_path)
    for name in ("../clip.mp3", "/etc/passwd"):
        with pytest.raises(mf.ManifestError, match="relative"):
            mf.prepare(manifest(card(front=[{"type": "audio", "filename": name}])), tmp_path)
    with pytest.raises(mf.ManifestError, match="not found"):
        mf.prepare(manifest(card(front=[{"type": "audio", "filename": "missing.mp3"}])), tmp_path)


def test_sha_only_media_is_remote():
    sha = "a" * 64
    prepared = mf.prepare(manifest(card(front=[{"type": "image", "sha256": sha}])), None)
    assert prepared.remote_media == {sha} and not prepared.media


@pytest.mark.parametrize("name", sorted(p.name for p in EXAMPLES.glob("*.json") if p.name != "mapping.json"))
def test_examples_validate(name):
    data = mf.load(EXAMPLES / name)
    prepared = mf.prepare(data, EXAMPLES, require_files=False)
    assert prepared.cards
