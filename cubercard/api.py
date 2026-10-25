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
            if response.status_code == 429 and attempt < MAX_RETRIES_429:
                wait = _retry_after(response)
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
        return self.request(method, path, ok=ok, **kwargs).json()

    # ---- endpoints ----
    def me(self) -> dict:
        return self.json("GET", "/v1/me")

    def decks(self) -> list[dict]:
        return self.json("GET", "/v1/decks")["decks"]

    def create_deck(self, body: dict) -> dict:
        return self.json("POST", "/v1/decks", body)

    def patch_deck(self, deck_id: str, body: dict) -> dict:
        return self.json("PATCH", f"/v1/decks/{_seg(deck_id)}", body)

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


def _seg(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")


def _retry_after(response: httpx.Response) -> int:
    try:
        return max(1, min(120, int(response.headers.get("Retry-After", "5"))))
    except ValueError:
        return 5
