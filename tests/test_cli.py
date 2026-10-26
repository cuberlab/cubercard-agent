"""CLI behaviour against a mocked server (httpx.MockTransport)."""
import json

import httpx
import pytest

from cubercard import api, cli


@pytest.fixture
def server(monkeypatch, tmp_path):
    """Route requests to a handler; returns the list of recorded requests."""
    calls = []
    state = {"handler": None}

    class MockClient(api.Client):
        def __init__(self, base, key, timeout=60.0, transport=None):
            def handle(request):
                calls.append(request)
                return state["handler"](request)
            super().__init__(base, key, timeout, httpx.MockTransport(handle))

    monkeypatch.setattr(cli, "Client", MockClient)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("CUBERCARD_API_KEY", "ck_live_test")
    monkeypatch.setenv("CUBERCARD_API_BASE", "http://test.invalid")
    server.calls = calls

    def use(handler):
        state["handler"] = handler
        return calls
    return use


def test_auth_saves_credentials_0600(server, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("CUBERCARD_API_KEY")
    server(lambda r: httpx.Response(200, json={"id": "u1", "email": "someone@example.com"}))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("ck_live_abc\n"))
    assert cli.main(["auth", "--base", "http://test.invalid/v1"]) == 0
    path = tmp_path / "cfg" / "cubercard" / "credentials"
    assert json.loads(path.read_text()) == {"base": "http://test.invalid", "key": "ck_live_abc"}
    assert path.stat().st_mode & 0o777 == 0o600
    assert "someone@example.com" in capsys.readouterr().out


def test_error_prints_server_error_and_detail(server, capsys):
    server(lambda r: httpx.Response(404, json={"error": "not_found", "detail": "card not found"}))
    assert cli.main(["cards", "get", "c_x"]) == 1
    err = capsys.readouterr().err
    assert "HTTP 404 not_found: card not found" in err
    assert "Bearer" not in err and "ck_live_test" not in err


def test_schedule_sends_based_on_version_and_prints_409_state(server, capsys):
    current = {"queue": "review", "due": "2026-11-01T00:00:00Z", "state_version": 7}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"id": "c_1", "state": {**current, "state_version": 4}})
        body = json.loads(request.content)
        assert body == {"based_on_version": 4, "due": "2026-12-01T01:00:00Z"}
        return httpx.Response(409, json={"error": "version_conflict", "detail": "state_version is 7, not 4", "state": current})

    calls = server(handler)
    assert cli.main(["schedule", "c_1", "--due", "2026-12-01T09:00:00+08:00"]) == 1
    err = capsys.readouterr().err
    assert "current state on server" in err and '"state_version": 7' in err
    assert "HTTP 409 version_conflict: state_version is 7, not 4" in err
    assert [c.method for c in calls] == ["GET", "PATCH"]
    assert calls[0].headers["authorization"] == "Bearer ck_live_test"


def test_create_uploads_missing_media_then_batches_and_stops_on_failure(server, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("cubercard.manifest.MAX_BATCH_CARDS", 2)
    (tmp_path / "a.mp3").write_bytes(b"audio-a")
    cards = [{"external_key": f"k{i}", "term": "t", "definition": "d", "context": "c", "source": "s",
              "front": [{"type": "audio", "filename": "a.mp3"}], "back": [{"type": "text", "text": "x"}]} for i in range(3)]
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"batch_id": "b", "deck_id": "deck", "cards": cards}))

    def handler(request):
        if request.url.path == "/v1/media/check":
            shas = json.loads(request.content)["sha256"]
            return httpx.Response(200, json={"present": [], "missing": shas})
        if request.method == "PUT":
            assert request.headers["content-type"] == "audio/mpeg" and request.headers["content-length"] == "7"
            return httpx.Response(201, json={"sha256": request.url.path[-64:], "size": 7, "mime": "audio/mpeg", "status": "created"})
        batch = json.loads(request.content)
        assert all(set(b) <= {"type", "sha256"} for c in batch["cards"] for b in c["front"])
        if batch["batch_id"] == "b:part002":
            return httpx.Response(409, json={"error": "content_conflict", "detail": "whole batch rejected", "external_keys": ["k2"]})
        return httpx.Response(201, json={"created": 2, "existing": 0, "replay": False, "history_inserted": 0,
                                         "cards": [{"external_key": c["external_key"], "id": "c_" + c["external_key"], "status": "created"}
                                                   for c in batch["cards"]]})

    calls = server(handler)
    assert cli.main(["create", "--manifest", str(path)]) == 1
    captured = capsys.readouterr()
    assert "created  k0" in captured.out and "batch 1/2 b:part001: created 2" in captured.out
    assert "batch 2/2 (b:part002, 1 cards) failed" in captured.err and "409 content_conflict" in captured.err
    assert [c.method for c in calls] == ["POST", "PUT", "POST", "POST"]


def test_429_retries_short_waits_only(server, monkeypatch, capsys):
    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    replies = [httpx.Response(429, headers={"Retry-After": "3"}, json={"error": "rate_limited", "detail": "300/min"}),
               httpx.Response(200, json={"decks": []})]
    calls = server(lambda r: replies.pop(0))
    assert cli.main(["decks", "list"]) == 0 and len(calls) == 2
    server(lambda r: httpx.Response(429, headers={"Retry-After": "40000"}, json={"error": "rate_limited", "detail": "2000 media/day"}))
    assert cli.main(["decks", "list"]) == 1
    assert "429 rate_limited: 2000 media/day" in capsys.readouterr().err


def test_cards_delete_moves_to_trash_by_default(server, capsys):
    def handler(request):
        assert request.method == "DELETE" and request.url.path == "/v1/cards/c_1"
        assert "purge" not in request.url.params and "force" not in request.url.params
        return httpx.Response(200, json={"id": "c_1", "deleted": True, "purge_at": "2026-11-02T08:00:00Z"})

    calls = server(handler)
    assert cli.main(["cards", "delete", "c_1"]) == 0
    assert capsys.readouterr().out.strip() == (
        "c_1: moved to the trash; restore before 2026-11-02T08:00:00Z with `cubercard cards restore c_1`")
    assert len(calls) == 1


def test_cards_delete_purge_sends_purge_1(server, capsys):
    def handler(request):
        assert request.url.params.get("purge") == "1"
        return httpx.Response(200, json={"id": "c_1", "deleted": True, "media_released": ["a" * 64, "b" * 64],
                                         "storage_used_bytes": 0, "storage_quota_bytes": 5368709120})

    calls = server(handler)
    assert cli.main(["cards", "delete", "c_1", "--purge"]) == 0
    assert capsys.readouterr().out.strip() == "c_1: permanently deleted with its progress and history (2 media file(s) released)"
    assert [(c.method, c.url.params.get("purge")) for c in calls] == [("DELETE", "1")]


def test_cards_delete_force_is_gone(server):
    calls = server(lambda r: httpx.Response(500))
    with pytest.raises(SystemExit) as exc:
        cli.main(["cards", "delete", "c_1", "--force"])
    assert exc.value.code == 2 and calls == []


def test_cards_restore(server, capsys):
    calls = server(lambda r: httpx.Response(200, json={"id": "c_1", "deck_id": "french"}))
    assert cli.main(["cards", "restore", "c_1"]) == 0
    assert capsys.readouterr().out.strip() == "c_1: restored from the trash (deck french)"
    assert [(c.method, c.url.path) for c in calls] == [("POST", "/v1/cards/c_1/restore")]


def test_cards_restore_deck_deleted_points_at_decks_restore(server, capsys):
    server(lambda r: httpx.Response(409, json={"error": "deck_deleted", "detail": "deck is in the trash", "deck_id": "anki-12"}))
    assert cli.main(["cards", "restore", "c_1"]) == 1
    assert "`cubercard decks restore anki-12`" in capsys.readouterr().err


def test_cards_move_patches_deck_id(server, capsys):
    def handler(request):
        assert (request.method, request.url.path) == ("PATCH", "/v1/cards/c_1")
        assert json.loads(request.content) == {"deck_id": "grace"}
        return httpx.Response(200, json={"id": "c_1", "deck_id": "grace"})

    server(handler)
    assert cli.main(["cards", "move", "c_1", "--to", "grace"]) == 0
    assert capsys.readouterr().out.strip() == "c_1: moved to deck grace"


# ---------------------------------------------------------------- decks

DECKS = [
    {"id": "english", "name": "English", "parent_id": None, "new_per_day": 20, "card_count": 27, "sort_order": 1},
    {"id": "grace", "name": "Grace", "parent_id": "english", "new_per_day": 10, "card_count": 118, "sort_order": 0},
    {"id": "kids", "name": "Kids", "parent_id": "grace", "new_per_day": 5, "card_count": 3, "sort_order": 0},
    {"id": "turkish", "name": "Turkish", "parent_id": None, "new_per_day": 15, "card_count": 9, "sort_order": 0},
]


def test_decks_list_shows_parent(server, capsys):
    server(lambda r: httpx.Response(200, json={"decks": DECKS}))
    assert cli.main(["decks"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["deck", "name", "parent", "new/day", "cards"]
    assert lines[2].split() == ["grace", "Grace", "english", "10", "118"]
    assert cli.main(["decks", "--json", "list"]) == 0
    assert json.loads(capsys.readouterr().out)[1]["parent_id"] == "english"
    assert cli.main(["decks", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == DECKS


def test_decks_tree_indents_and_sums_subdecks(server, capsys):
    server(lambda r: httpx.Response(200, json={"decks": DECKS}))
    assert cli.main(["decks", "tree"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["name", "deck", "cards", "with", "subdecks", "new/day"]
    assert [line.split("  ")[0] for line in lines[1:3]] == ["Turkish", "English"]  # siblings by sort_order
    assert lines[3].startswith("  Grace ") and lines[4].startswith("    Kids ")
    assert lines[2].split() == ["English", "english", "27", "148", "20"]
    assert lines[3].split() == ["Grace", "grace", "118", "121", "10"]
    assert cli.main(["decks", "tree", "--json"]) == 0
    tree = json.loads(capsys.readouterr().out)
    assert [n["id"] for n in tree] == ["turkish", "english"]
    assert tree[1]["children"][0]["children"][0]["id"] == "kids" and tree[1]["total_cards"] == 148


def test_decks_create_with_parent(server, capsys):
    def handler(request):
        assert json.loads(request.content) == {"name": "Grace", "id": "grace", "parent_id": "english", "new_per_day": 10}
        return httpx.Response(201, json={"id": "grace", "name": "Grace", "parent_id": "english", "new_per_day": 10,
                                         "card_count": 0, "sort_order": 0})

    server(handler)
    assert cli.main(["decks", "create", "Grace", "--id", "grace", "--parent", "english", "--new-per-day", "10"]) == 0
    assert capsys.readouterr().out.strip() == "created deck grace (Grace, new/day 10, under english)"


def test_deck_names_with_double_colon_are_rejected_locally(server, capsys):
    calls = server(lambda r: httpx.Response(500))
    assert cli.main(["decks", "create", "English::Grace"]) == 2
    assert cli.main(["decks", "rename", "grace", "English::Grace"]) == 2
    assert "cannot contain '::'" in capsys.readouterr().err and calls == []


@pytest.mark.parametrize("argv, body, printed", [
    (["decks", "move", "grace", "--to", "root"], {"parent_id": None}, "deck grace (Grace): now at the top level"),
    (["decks", "move", "grace", "--to", "turkish"], {"parent_id": "turkish"}, "deck grace (Grace): now under turkish"),
    (["decks", "rename", "grace", "Grace 2"], {"name": "Grace 2"}, "deck grace: renamed to Grace 2"),
    (["decks", "set", "english", "--new-per-day", "5", "--apply-to-subdecks"], {"new_per_day": 5, "apply_to_subdecks": True},
     "(new/day written to every subdeck too)"),
    (["decks", "set", "english", "--new-per-day", "5"], {"new_per_day": 5}, "new/day=5"),
])
def test_decks_patch(server, capsys, argv, body, printed):
    def handler(request):
        assert (request.method, request.url.path) == ("PATCH", f"/v1/decks/{argv[2]}")
        assert json.loads(request.content) == body
        deck = {"id": argv[2], "name": body.get("name", "Grace"), "parent_id": body.get("parent_id"),
                "new_per_day": body.get("new_per_day", 10), "card_count": 1}
        return httpx.Response(200, json=deck)

    server(handler)
    assert cli.main(argv) == 0
    assert printed in capsys.readouterr().out


def test_decks_apply_to_subdecks_needs_new_per_day(server, capsys):
    calls = server(lambda r: httpx.Response(500))
    assert cli.main(["decks", "set", "english", "--name", "E", "--apply-to-subdecks"]) == 2
    assert calls == []


def test_decks_delete_trash_and_purge(server, capsys):
    def handler(request):
        assert (request.method, request.url.path) == ("DELETE", "/v1/decks/english")
        if request.url.params.get("purge") == "1":
            return httpx.Response(200, json={"id": "english", "deleted": True})
        assert not request.url.params
        return httpx.Response(200, json={"id": "english", "deleted": True, "deck_count": 3, "card_count": 148,
                                         "purge_at": "2026-11-02T08:00:00Z"})

    calls = server(handler)
    assert cli.main(["decks", "delete", "english"]) == 0
    assert capsys.readouterr().out.strip() == (
        "deck english: moved to the trash with its subdecks and cards (3 deck(s), 148 card(s)); "
        "restore before 2026-11-02T08:00:00Z with `cubercard decks restore english`")
    assert cli.main(["decks", "delete", "english", "--purge"]) == 0
    assert capsys.readouterr().out.strip() == "deck english: permanently deleted with its subdecks and cards"
    assert [c.url.params.get("purge") for c in calls] == [None, "1"]


def test_decks_restore_warns_when_back_at_top_level(server, capsys):
    calls = server(lambda r: httpx.Response(200, json={"id": "grace", "name": "Grace (2)", "restored_to_root": True}))
    assert cli.main(["decks", "restore", "grace"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "deck grace (Grace (2)): restored with its subdecks and cards"
    assert "now at the top level" in captured.err
    assert [(c.method, c.url.path) for c in calls] == [("POST", "/v1/decks/grace/restore")]


# ---------------------------------------------------------------- trash

def test_trash_list_and_empty(server, capsys):
    trash = {"retention_days": 7, "items": [
        {"type": "deck", "id": "english", "name": "English", "parent_id": None, "deck_count": 3, "card_count": 148,
         "deleted_at": "2026-10-26T08:00:00Z", "purge_at": "2026-11-02T08:00:00Z"},
        {"type": "card", "id": "c_1", "deck_id": "turkish", "deck_name": "Turkish", "sort_key": "pamuk", "term": "pamuk",
         "deleted_at": "2026-10-25T08:00:00Z", "purge_at": "2026-11-01T08:00:00Z"}]}

    def handler(request):
        assert request.url.path == "/v1/trash"
        if request.method == "DELETE":
            return httpx.Response(200, json={"deck_count": 3, "card_count": 149})
        return httpx.Response(200, json=trash)

    calls = server(handler)
    assert cli.main(["trash", "list"]) == 0
    text = capsys.readouterr().out
    assert text.splitlines()[0] == "trash: 2 item(s), kept 7 days"
    assert "English (3 deck(s), 148 card(s))" in text and "pamuk" in text and "Turkish" in text
    assert cli.main(["trash", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == trash
    assert cli.main(["trash", "empty"]) == 0
    assert capsys.readouterr().out.strip() == "trash emptied: everything in it is permanently deleted (3 deck(s), 149 card(s))"
    assert [c.method for c in calls] == ["GET", "GET", "DELETE"]


# ---------------------------------------------------------------- create + decks.json

def _deck_server(existing):
    """Stateful fake for GET/POST /decks and POST /cards/batch."""
    decks = {d["id"]: dict(d) for d in existing}

    def handler(request):
        if request.url.path == "/v1/decks" and request.method == "GET":
            return httpx.Response(200, json={"decks": list(decks.values())})
        if request.url.path == "/v1/decks" and request.method == "POST":
            body = json.loads(request.content)
            if body.get("parent_id") and body["parent_id"] not in decks:
                return httpx.Response(400, json={"error": "invalid_parent", "detail": "no such parent"})
            deck = {"id": body["id"], "name": body["name"], "parent_id": body.get("parent_id"), "new_per_day": 20,
                    "card_count": 0, "sort_order": 0}
            decks[deck["id"]] = deck
            return httpx.Response(201, json=deck)
        if request.url.path == "/v1/cards/batch":
            batch = json.loads(request.content)
            if batch["deck_id"] not in decks:
                return httpx.Response(404, json={"error": "unknown_deck", "detail": batch["deck_id"]})
            return httpx.Response(201, json={"created": len(batch["cards"]), "existing": 0, "replay": False,
                                             "cards": [{"external_key": c["external_key"], "id": "c_" + c["external_key"],
                                                        "status": "created"} for c in batch["cards"]]})
        raise AssertionError(f"unexpected {request.method} {request.url}")
    return handler, decks


def _import_dir(tmp_path, decks_json, deck_id="anki-10"):
    folder = tmp_path / "import"
    folder.mkdir()
    (folder / "decks.json").write_text(json.dumps(decks_json))
    card = {"external_key": "k1", "term": "t", "definition": "d", "context": "c", "source": "s",
            "front": [{"type": "title", "text": "t"}], "back": [{"type": "text", "text": "d"}]}
    (folder / "manifest.json").write_text(json.dumps({"batch_id": "b", "deck_id": deck_id, "cards": [card]}))
    return folder


TREE_JSON = [{"id": "anki-path-0123456789ab", "name": "English", "parent": None},
             {"id": "anki-12", "name": "Grace", "parent": "anki-path-0123456789ab"},
             {"id": "anki-10", "name": "Kids", "parent": "anki-12"}]


def test_create_makes_missing_decks_top_down_then_cards_and_is_idempotent(server, tmp_path, capsys):
    folder = _import_dir(tmp_path, TREE_JSON)
    handler, decks = _deck_server([{"id": "anki-path-0123456789ab", "name": "English", "parent_id": None,
                                    "new_per_day": 20, "card_count": 0, "sort_order": 0}])
    calls = server(handler)
    assert cli.main(["create", "--manifest", str(folder / "manifest.json")]) == 0
    out = capsys.readouterr().out
    posts = [json.loads(c.content) for c in calls if c.method == "POST" and c.url.path == "/v1/decks"]
    assert posts == [{"name": "Grace", "id": "anki-12", "parent_id": "anki-path-0123456789ab"},
                     {"name": "Kids", "id": "anki-10", "parent_id": "anki-12"}]
    assert "decks: 2 created, 1 already exist" in out and "done: created 1" in out
    paths = [c.url.path for c in calls]
    assert paths.index("/v1/cards/batch") > max(i for i, p in enumerate(paths) if p == "/v1/decks")

    calls.clear()
    assert cli.main(["create", "--manifest", str(folder / "manifest.json")]) == 0
    captured = capsys.readouterr()
    assert "decks: 0 created, 3 already exist" in captured.out and "warning" not in captured.err
    assert not [c for c in calls if c.method == "POST" and c.url.path == "/v1/decks"]


def test_create_warns_on_name_or_parent_mismatch_without_changing(server, tmp_path, capsys):
    folder = _import_dir(tmp_path, TREE_JSON)
    handler, _ = _deck_server([
        {"id": "anki-path-0123456789ab", "name": "🇬🇧 English", "parent_id": None, "new_per_day": 20, "card_count": 0},
        {"id": "anki-12", "name": "Grace", "parent_id": None, "new_per_day": 20, "card_count": 0},
        {"id": "anki-10", "name": "Kids", "parent_id": "anki-12", "new_per_day": 20, "card_count": 0}])
    calls = server(handler)
    assert cli.main(["create", "--manifest", str(folder / "manifest.json")]) == 0
    err = capsys.readouterr().err
    assert "deck anki-path-0123456789ab is named '🇬🇧 English' on the server, decks.json says 'English'; not changed" in err
    assert "deck anki-12 is under the top level on the server, decks.json says anki-path-0123456789ab; not changed" in err
    assert {c.method for c in calls if c.url.path.startswith("/v1/decks")} == {"GET"}


def test_create_dry_run_lists_decks_to_create_without_writing(server, tmp_path, capsys):
    folder = _import_dir(tmp_path, TREE_JSON)
    handler, _ = _deck_server([])
    calls = server(handler)
    assert cli.main(["create", "--manifest", str(folder / "manifest.json"), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "deck anki-10 (Kids): would be created under anki-12" in out
    assert "decks: 3 to create, 0 already exist" in out and "deck anki-10: created from decks.json" in out
    assert {c.method for c in calls} == {"GET"}


def test_create_with_explicit_decks_file(server, tmp_path, capsys):
    folder = _import_dir(tmp_path, [])
    other = tmp_path / "tree.json"
    other.write_text(json.dumps(TREE_JSON))
    handler, decks = _deck_server([])
    server(handler)
    assert cli.main(["create", "--manifest", str(folder / "manifest.json"), "--decks", str(other)]) == 0
    assert set(decks) == {"anki-path-0123456789ab", "anki-12", "anki-10"}


def test_create_rejects_bad_decks_json_before_any_request(server, tmp_path, capsys):
    folder = _import_dir(tmp_path, [{"id": "anki-10", "name": "English::Kids", "parent": None}])
    calls = server(lambda r: httpx.Response(500))
    assert cli.main(["create", "--manifest", str(folder / "manifest.json")]) == 2
    assert "cannot contain '::'" in capsys.readouterr().err and calls == []


def test_create_explains_a_deck_in_the_trash(server, tmp_path, capsys):
    folder = _import_dir(tmp_path, TREE_JSON[:1], deck_id="anki-path-0123456789ab")

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"decks": []})
        return httpx.Response(409, json={"error": "deck_exists", "detail": "deck id exists"})

    server(handler)
    assert cli.main(["create", "--manifest", str(folder / "manifest.json")]) == 1
    assert "probably in the trash" in capsys.readouterr().err
