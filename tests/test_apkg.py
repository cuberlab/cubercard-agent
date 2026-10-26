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
