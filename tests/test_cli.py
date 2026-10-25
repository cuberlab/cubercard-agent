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
