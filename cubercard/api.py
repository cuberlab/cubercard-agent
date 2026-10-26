"""HTTP client and credential storage for the Cuber Card API."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from . import __version__

DEFAULT_BASE = "https://api.card.cuberlab.tw"
ENV_KEY = "CUBERCARD_API_KEY"
ENV_BASE = "CUBERCARD_API_BASE"
MAX_RETRIES_429 = 5
# Per-minute limits are retried; a longer Retry-After (the daily media limit) is reported instead.
MAX_RETRY_WAIT_S = 90
UNSET: Any = object()  # "argument not given", distinct from None (= JSON null)


class CliError(Exception):
    """Local problem (usage, file, validation). Exit code 2."""


class ApiError(Exception):
    """The server answered with an error body. Exit code 1."""

    def __init__(self, status: int, body: Any, method: str = "", path: str = ""):
        self.status = status
        self.body = body if isinstance(body, dict) else {"error": "http_" + str(status), "detail": str(body)[:500]}
        self.method = method
        self.path = path
        super().__init__(self.describe())

    @property
    def code(self) -> str:
        return str(self.body.get("error", ""))

    @property
    def detail(self) -> str:
        return str(self.body.get("detail", ""))

    def extra(self) -> dict:
        return {k: v for k, v in self.body.items() if k not in ("error", "detail")}

    def describe(self) -> str:
        line = f"HTTP {self.status} {self.code}: {self.detail}"
        extra = self.extra()
        if extra:
            line += "\n" + json.dumps(extra, ensure_ascii=False, indent=2)
        return line


def config_dir() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(root) / "cubercard"


def credentials_path() -> Path:
    return config_dir() / "credentials"


def normalize_base(base: str) -> str:
    base = base.strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    if not base.startswith(("http://", "https://")):
        raise CliError(f"base URL must start with http:// or https://: {base}")
    return base


def read_credentials() -> dict:
    path = credentials_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise CliError(f"cannot read {path}: {error}") from error
    return data if isinstance(data, dict) else {}


def write_credentials(base: str, key: str) -> Path:
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump({"base": base, "key": key}, out)
        out.write("\n")
    os.chmod(path, 0o600)
    return path


def resolve(base: str | None = None, key: str | None = None) -> tuple[str, str | None]:
    """Base and key: explicit argument > environment > credentials file > default."""
    stored = read_credentials()
    base = base or os.environ.get(ENV_BASE) or stored.get("base") or DEFAULT_BASE
    key = key or os.environ.get(ENV_KEY) or stored.get("key")
    return normalize_base(base), key


class Client:
    def __init__(self, base: str, key: str | None, timeout: float = 60.0, transport: httpx.BaseTransport | None = None):
        self.base = normalize_base(base)
        self.key = key
        headers = {"User-Agent": f"cubercard/{__version__}"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        self._http = httpx.Client(base_url=self.base, headers=headers, timeout=timeout, transport=transport)

    @classmethod
    def from_env(cls, require_key: bool = True) -> "Client":
        base, key = resolve()
        if require_key and not key:
            raise CliError(f"no API key: run `cubercard auth` or set {ENV_KEY}")
        return cls(base, key)

    def close(self) -> None:
        self._http.close()

    def request(self, method: str, path: str, *, ok: tuple[int, ...] = (200, 201), **kwargs) -> httpx.Response:
        for attempt in range(MAX_RETRIES_429 + 1):
            try:
                response = self._http.request(method, path, **kwargs)
            except httpx.HTTPError as error:
                raise CliError(f"{method} {self.base}{path}: {error}") from error
            wait = _retry_after(response) if response.status_code == 429 else None
            if wait is not None and wait <= MAX_RETRY_WAIT_S and attempt < MAX_RETRIES_429:
                print(f"rate limited (429), retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            break
        if response.status_code not in ok:
            try:
                body = response.json()
            except ValueError:
                body = response.text
            raise ApiError(response.status_code, body, method, path)
        return response

    def json(self, method: str, path: str, body: Any = None, *, ok: tuple[int, ...] = (200, 201), **kwargs) -> Any:
        if body is not None:
            kwargs["content"] = json.dumps(body, ensure_ascii=False).encode()
            kwargs.setdefault("headers", {})["Content-Type"] = "application/json"
        response = self.request(method, path, ok=ok, **kwargs)
        return response.json() if response.content else {}

    # ---- endpoints ----
    def me(self) -> dict:
        return self.json("GET", "/v1/me")

    def decks(self) -> list[dict]:
        return self.json("GET", "/v1/decks")["decks"]

    def create_deck(self, name: str, *, id: str | None = None, parent_id: str | None = None,
                    new_per_day: int | None = None, sort_order: int | None = None) -> dict:
        body: dict[str, Any] = {"name": name}
        for key, value in (("id", id), ("parent_id", parent_id), ("new_per_day", new_per_day), ("sort_order", sort_order)):
            if value is not None:
                body[key] = value
        return self.json("POST", "/v1/decks", body)

    def update_deck(self, deck_id: str, *, name: str | None = None, parent_id: Any = UNSET, new_per_day: int | None = None,
                    sort_order: int | None = None, apply_to_subdecks: bool = False) -> dict:
        """PATCH /decks/{id}. ``parent_id=None`` moves the deck to the top level; leave it out to keep the parent."""
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if parent_id is not UNSET:
            body["parent_id"] = parent_id
        if new_per_day is not None:
            body["new_per_day"] = new_per_day
            if apply_to_subdecks:
                body["apply_to_subdecks"] = True
        elif apply_to_subdecks:
            raise CliError("apply_to_subdecks needs new_per_day")
        if sort_order is not None:
            body["sort_order"] = sort_order
        if not body:
            raise CliError("update_deck: nothing to change")
        return self.json("PATCH", f"/v1/decks/{_seg(deck_id)}", body)

    def reorder_decks(self, parent_id: str | None, ids: list[str]) -> dict:
        """Set sort_order 0..n for all siblings under parent_id (None = top level); ids must be that whole level."""
        return self.json("POST", "/v1/decks/reorder", {"parent_id": parent_id, "ids": ids})

    def delete_deck(self, deck_id: str, purge: bool = False) -> dict:
        """The deck, its subdecks and their cards go to the trash; purge=True deletes them for good."""
        return self.json("DELETE", f"/v1/decks/{_seg(deck_id)}", params={"purge": "1"} if purge else None)

    def restore_deck(self, deck_id: str) -> dict:
        return self.json("POST", f"/v1/decks/{_seg(deck_id)}/restore")

    def media_check(self, shas: list[str]) -> dict:
        present, missing = [], []
        for i in range(0, len(shas), 200):
            res = self.json("POST", "/v1/media/check", {"sha256": shas[i:i + 200]})
            present += res["present"]
            missing += res["missing"]
        return {"present": present, "missing": missing}

    def put_media(self, sha: str, path: Path, mime: str) -> dict:
        data = path.read_bytes()
        headers = {"Content-Type": mime, "Content-Length": str(len(data))}
        return self.json("PUT", f"/v1/media/{sha}", None, content=data, headers=headers)

    def batch(self, manifest: dict) -> dict:
        return self.json("POST", "/v1/cards/batch", manifest)

    def cards(self, **params) -> dict:
        return self.json("GET", "/v1/cards", params={k: v for k, v in params.items() if v is not None})

    def card(self, card_id: str) -> dict:
        return self.json("GET", f"/v1/cards/{_seg(card_id)}")

    def patch_card(self, card_id: str, body: dict) -> dict:
        return self.json("PATCH", f"/v1/cards/{_seg(card_id)}", body)

    def patch_state(self, card_id: str, body: dict) -> dict:
        return self.json("PATCH", f"/v1/cards/{_seg(card_id)}/state", body)

    def delete_card(self, card_id: str, purge: bool = False) -> dict:
        """Move a card to the trash (restorable for the retention period); purge=True deletes it and its history."""
        return self.json("DELETE", f"/v1/cards/{_seg(card_id)}", params={"purge": "1"} if purge else None)

    def restore_card(self, card_id: str) -> dict:
        return self.json("POST", f"/v1/cards/{_seg(card_id)}/restore")

    def move_card(self, card_id: str, deck_id: str) -> dict:
        return self.json("PATCH", f"/v1/cards/{_seg(card_id)}", {"deck_id": deck_id})

    def list_trash(self) -> dict:
        return self.json("GET", "/v1/trash")

    def empty_trash(self) -> dict:
        return self.json("DELETE", "/v1/trash")

def _seg(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")


def _retry_after(response: httpx.Response) -> int:
    try:
        return max(1, int(response.headers.get("Retry-After", "5")))
    except ValueError:
        return 5
