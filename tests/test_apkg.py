import json

import pytest

from cubercard import apkg, fsrs, manifest as mf
from cubercard.cli import main

from . import apkg_fixture as fx


def _events(cid):
    return [{"event_id": f"{rid:016d}", "rating": ease, "reviewed_at": apkg._iso_ms(rid)}
            for rid, c, ease, ivl, ms, t in fx.REVLOG if c == cid and ease in (1, 2, 3, 4) and t not in (4, 5)]


@pytest.mark.parametrize("fmt", ["legacy", "schema18"])
def test_apkg_to_manifest(tmp_path, fmt):
    path = fx.build(tmp_path, fmt)
    out = tmp_path / "out"
    report = apkg.convert(path, "french", out)
    data = json.loads((out / "manifest.json").read_text())
    cards = {c["external_key"]: c for c in data["cards"]}
    assert set(cards) == {f"anki:{fx.BASIC_NID}:0", f"anki:{fx.CLOZE_NID}:c1", f"anki:{fx.CLOZE_NID}:c2"}
    assert data["deck_id"] == "french" and data["batch_id"].startswith("anki:")
    assert report["collection"] == ("collection.anki2" if fmt == "legacy" else "collection.anki21b")

    basic = cards[f"anki:{fx.BASIC_NID}:0"]
    assert basic["term"] == "bonjour" and basic["definition"] == "hello\na greeting"
    assert basic["front"] == [{"type": "title", "text": "bonjour"}, {"type": "audio", "filename": "media/bonjour.mp3"}]
    # {{FrontSide}} repeats the front on the back; the app keeps the front on screen, so the repeats are dropped.
    assert basic["back"] == [{"type": "translation", "text": "hello\na greeting"}]
    assert [h["rating"] for h in basic["history"]] == [3, 3, 3]  # the manual reschedule row is not a review
    assert basic["history"][0] == {"rating": 3, "reviewed_at": "2026-01-01T00:00:00Z", "duration_ms": 4000}
    expected = fsrs.replay(_events(3001))
    expected.pop("state_version")
    assert basic["state"] == expected and basic["state"]["queue"] == "review"
    assert basic["source"] == ("Anki: French" if fmt == "legacy" else "Anki: Languages::French")

    c1, c2 = cards[f"anki:{fx.CLOZE_NID}:c1"], cards[f"anki:{fx.CLOZE_NID}:c2"]
    assert c1["kind"] == c2["kind"] == "cloze"
    assert c1["front"][0] == {"type": "cloze", "text": fx.CLOZE_TEXT, "index": 1}
    assert c2["front"][0]["index"] == 2 and c2["back"][0] == c2["front"][0]
    assert not [b for b in c1["back"] if b["type"] in ("audio", "video")], "the front's media is not repeated on the back"
    assert {"type": "image", "filename": "media/map.png"} in c1["back"]
    assert {"type": "text", "text": "Europe"} in c1["back"]
    assert c1["term"] == "France" and c2["term"] == "Germany"
    assert [h["rating"] for h in c1["history"]] == [1, 3] and c1["state"]["queue"] == "learning"
    assert "history" not in c2 and "state" not in c2  # Anki new card

    assert (out / "media" / "bonjour.mp3").read_bytes() == fx.AUDIO
    assert (out / "media" / "map.png").read_bytes() == fx.IMAGE
    # Reviewed cards first, new cards keep Anki's new order.
    assert data["cards"][-1]["external_key"] == f"anki:{fx.CLOZE_NID}:c2"
    # The converted manifest passes the same validation `create` runs.
    prepared = mf.prepare(data, out)
    assert len(prepared.cards) == 3 and len(prepared.media) == 2


def test_mapping_overrides(tmp_path):
    path = fx.build(tmp_path)
    mapping = {"note_types": {"Basic": {"lang": "fr", "kind": "sentence", "term": "Front", "definition": "Back",
                                        "blocks": {"Back": "text"}, "source": "My Anki"}}}
    report = apkg.convert(path, "french", tmp_path / "o", mapping, write=False)
    basic = report["manifest"]["cards"][0]
    assert basic["kind"] == "sentence" and basic["lang"] == "fr" and basic["source"] == "My Anki"
    assert {"type": "text", "text": "hello\na greeting", "lang": "fr"} in basic["back"]


def test_mapping_rejects_unknown_field(tmp_path):
    path = fx.build(tmp_path)
    with pytest.raises(apkg.ApkgError, match="no field named"):
        apkg.convert(path, "french", tmp_path / "o", {"note_types": {"Basic": {"term": "Nope"}}}, write=False)


def test_cli_preview(tmp_path, capsys):
    path = fx.build(tmp_path)
    assert main(["apkg-to-manifest", str(path), "--deck", "french", "--out", str(tmp_path / "o"), "--preview"]) == 0
    text = capsys.readouterr().out
    assert "note type: Basic  kind=basic  notes=1  cards=1" in text
    assert "note type: Cloze  kind=cloze  notes=1  cards=2" in text
    assert "total cards: 3" in text
    assert not (tmp_path / "o" / "manifest.json").exists()


def test_html_helpers():
    assert apkg.strip_html("a<br>b&amp;c<div>d</div><style>x{}</style>") == "a\nb&c\nd"
    assert apkg.extract_media('[sound:a.mp3] <img src="b.png"> <img src=c.jpg> [sound:v.mp4]') == [
        ("audio", "a.mp3"), ("image", "b.png"), ("image", "c.jpg"), ("video", "v.mp4")]
    assert apkg.template_refs("{{#Front}}{{Front}}{{/Front}} {{type:Back}} {{tts en_US:Front}} {{hint:Extra}}",
                              ["Front", "Back", "Extra"]) == ["Front", "Extra"]


def test_drop_front_repeats_ignores_autoplay_and_keeps_a_back_that_would_be_empty():
    title = {"type": "title", "text": "pamuk"}
    audio = {"type": "audio", "filename": "media/p.mp3"}
    back = [title, dict(audio, autoplay=False), {"type": "translation", "text": "棉"}]
    assert apkg.drop_front_repeats(back, [title, audio]) == [{"type": "translation", "text": "棉"}]
    assert apkg.drop_front_repeats([title], [title, audio]) == [title]
    assert apkg.drop_front_repeats([{"type": "title", "text": "pamuk", "lang": "tr"}], [title]) == [{"type": "title", "text": "pamuk", "lang": "tr"}]


def _sha12(path):
    import hashlib
    return hashlib.sha256(path.encode()).hexdigest()[:12]


# Grace has an Anki deck, English has one and its own cards, Languages has no Anki deck row.
TREE = {10: "Languages::English::Grace", 12: "Languages::English"}
TREE_CARDS = {3001: 10, 3002: 12, 3003: 12}


@pytest.mark.parametrize("fmt", ["legacy", "schema18"])
def test_three_level_tree_becomes_decks_json_and_one_manifest_per_deck(tmp_path, fmt):
    path = fx.build(tmp_path, fmt, decks=TREE, card_decks=TREE_CARDS)
    out = tmp_path / "out"
    report = apkg.convert(path, None, out)
    top = "anki-path-" + _sha12("Languages")
    decks = json.loads((out / "decks.json").read_text())
    assert decks == [{"id": top, "name": "Languages", "parent": None},
                     {"id": "anki-12", "name": "English", "parent": top},
                     {"id": "anki-10", "name": "Grace", "parent": "anki-12"}]
    assert mf.load_decks(out / "decks.json") == decks
    assert not (out / "manifest.json").exists()
    english = json.loads((out / "manifest-anki-12.json").read_text())
    grace = json.loads((out / "manifest-anki-10.json").read_text())
    assert english["deck_id"] == "anki-12" and grace["deck_id"] == "anki-10"
    assert [c["external_key"] for c in grace["cards"]] == [f"anki:{fx.BASIC_NID}:0"]
    assert {c["external_key"] for c in english["cards"]} == {f"anki:{fx.CLOZE_NID}:c1", f"anki:{fx.CLOZE_NID}:c2"}
    base = english["batch_id"].rsplit(":", 1)[0]
    assert english["batch_id"] == f"{base}:anki-12" and grace["batch_id"] == f"{base}:anki-10"
    assert grace["cards"][0]["source"] == "Anki: Languages::English::Grace"
    assert report["manifest"] is None and len(report["manifests"]) == 2
    for name in ("manifest-anki-12.json", "manifest-anki-10.json"):
        mf.prepare(json.loads((out / name).read_text()), out)  # media/ is shared next to both manifests


def test_single_deck_keeps_manifest_json_and_batch_id(tmp_path):
    path = fx.build(tmp_path)  # legacy, one deck "French" id 1
    out = tmp_path / "out"
    report = apkg.convert(path, None, out)
    data = json.loads((out / "manifest.json").read_text())
    assert data["deck_id"] == "anki-1" and data["batch_id"] == f"anki:{mf.sha256_file(path)[:16]}"
    assert json.loads((out / "decks.json").read_text()) == [{"id": "anki-1", "name": "French", "parent": None}]
    assert report["path"] == str(out / "manifest.json")


def test_schema18_intermediate_without_anki_id_uses_path_hash(tmp_path):
    path = fx.build(tmp_path, "schema18")  # one deck "Languages::French", no row for "Languages"
    apkg.convert(path, None, tmp_path / "out")
    top = "anki-path-" + _sha12("Languages")
    assert json.loads((tmp_path / "out" / "decks.json").read_text()) == [
        {"id": top, "name": "Languages", "parent": None}, {"id": "anki-1", "name": "French", "parent": top}]


def test_explicit_deck_puts_everything_in_one_manifest_without_decks_json(tmp_path):
    path = fx.build(tmp_path, decks=TREE, card_decks=TREE_CARDS)
    report = apkg.convert(path, "english", tmp_path / "out")
    data = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert data["deck_id"] == "english" and len(data["cards"]) == 3
    assert report["decks"] is None and not (tmp_path / "out" / "decks.json").exists()


def test_filtered_deck_cards_go_to_their_home_deck(tmp_path):
    path = fx.build(tmp_path, decks={1: "French", 99: "Filtered Deck 1"}, card_decks={3001: (99, 1)})
    report = apkg.convert(path, None, tmp_path / "out", write=False)
    assert report["decks"] == [{"id": "anki-1", "name": "French", "parent": None}]
    assert report["manifest"]["deck_id"] == "anki-1" and len(report["manifest"]["cards"]) == 3
    assert report["manifest"]["cards"][0]["source"] == "Anki: French"


def test_deeper_than_five_levels_warns(tmp_path):
    path = fx.build(tmp_path, decks={10: "A::B::C::D::E::F"}, card_decks={3001: 10, 3002: 10, 3003: 10})
    report = apkg.convert(path, None, tmp_path / "out", write=False)
    assert [d["name"] for d in report["decks"]] == list("ABCDEF")
    assert any("deck anki-10 (F) is level 6" in w for w in report["warnings"])


def test_cli_preview_prints_deck_tree_and_writes_nothing(tmp_path, capsys):
    path = fx.build(tmp_path, decks=TREE, card_decks=TREE_CARDS)
    assert main(["apkg-to-manifest", str(path), "--out", str(tmp_path / "o"), "--preview"]) == 0
    text = capsys.readouterr().out
    top = "anki-path-" + _sha12("Languages")
    assert "deck tree (decks.json, 3 decks, 2 with cards):" in text
    assert f"\n  Languages  [{top}]\n    English  [anki-12]  cards=2\n      Grace  [anki-10]  cards=1\n" in text
    assert "total cards: 3" in text
    assert not (tmp_path / "o").exists()


def test_cli_writes_manifests_and_decks_json(tmp_path, capsys):
    path = fx.build(tmp_path, decks=TREE, card_decks=TREE_CARDS)
    out = tmp_path / "o"
    assert main(["apkg-to-manifest", str(path), "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert f"wrote {out / 'decks.json'} (3 decks" in text
    assert f"next: cubercard create --manifest {out / 'manifest-anki-12.json'} --dry-run" in text
    assert f"next: cubercard create --manifest {out / 'manifest-anki-10.json'} --dry-run" in text


@pytest.mark.parametrize("data, message", [
    ([{"id": "a", "name": "A::B", "parent": None}], "cannot contain '::'"),
    ([{"id": "b", "name": "B", "parent": "a"}, {"id": "a", "name": "A", "parent": None}], "must come before"),
    ([{"id": "a", "name": "A", "parent": None}, {"id": "a", "name": "A2", "parent": None}], "duplicate deck id"),
    ([{"id": "A!", "name": "A", "parent": None}], "must match"),
    ({"id": "a"}, "JSON array"),
    ([{"id": "a", "name": "A", "parent": None, "extra": 1}], "unknown field"),
])
def test_load_decks_rejects(tmp_path, data, message):
    path = tmp_path / "decks.json"
    path.write_text(json.dumps(data))
    with pytest.raises(mf.ManifestError, match=message):
        mf.load_decks(path)
