"""Manifest validation, cloze expansion, media resolution and batch splitting.

The rules mirror the server's ``POST /v1/cards/batch`` validation so mistakes
are caught before any upload. Input media blocks name a local ``filename``
(relative to the media directory); the CLI hashes it and sends only
``sha256`` to the server.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .api import CliError

MAX_BATCH_CARDS = 500
# Server limit is 2 MiB per JSON body; keep headroom for encoding differences.
MAX_BATCH_BYTES = 1_900_000
MAX_BLOCKS = 50
KINDS = ("basic", "sentence", "text_recall", "alphabet", "cloze")
DIRECTIONS = ("listen", "read", "speak", "write")
QUEUES = ("new", "learning", "review", "relearning")
TEXT_TYPES = ("title", "text", "quote", "translation", "source")
MEDIA_TYPES = ("audio", "video", "image")
DECK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,3})?Z$")
CLOZE_RE = re.compile(r"\{\{c(\d+)::(.+?)(?:::(.*?))?\}\}")
CARD_KEYS = {"external_key", "term", "definition", "context", "source", "pos", "kind", "template", "lang",
             "direction", "front", "back", "history", "state", "suspended"}
MANIFEST_KEYS = {"batch_id", "deck_id", "cards"}
FUTURE_SKEW = timedelta(minutes=10)

MIME_BY_EXT = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".oga": "audio/ogg", ".opus": "audio/ogg", ".flac": "audio/flac", ".mp4": "video/mp4", ".m4v": "video/mp4",
    ".mov": "video/quicktime", ".webm": "video/webm", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".avif": "image/avif", ".heic": "image/heic",
}


class ManifestError(CliError):
    pass


@dataclass
class MediaFile:
    sha256: str
    path: Path
    mime: str
    size: int


@dataclass
class Prepared:
    batch_id: str
    deck_id: str
    cards: list[dict]
    media: dict[str, MediaFile] = field(default_factory=dict)  # sha256 -> local file
    remote_media: set[str] = field(default_factory=set)  # sha256 referenced without a local file
    warnings: list[str] = field(default_factory=list)


def mime_for(path: str | Path) -> str:
    mime = MIME_BY_EXT.get(Path(path).suffix.lower())
    if not mime:
        raise ManifestError(f"unknown media type for {path}; use one of {', '.join(sorted(MIME_BY_EXT))}")
    return mime


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load(path: str | Path) -> dict:
    path = Path(path)
    try:
        data = json.loads(path.read_text())
    except OSError as error:
        raise ManifestError(f"cannot read manifest {path}: {error}") from error
    except ValueError as error:
        raise ManifestError(f"manifest {path} is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON object")
    return data


# ---------------------------------------------------------------- helpers

def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{label} must be a nonempty string")
    return value


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not ISO_RE.match(value):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def cloze_indexes(text: str) -> list[int]:
    return sorted({int(m.group(1)) for m in CLOZE_RE.finditer(text)})


def cloze_answers(text: str, index: int) -> tuple[list[str], list[str]]:
    answers, hints = [], []
    for m in CLOZE_RE.finditer(text):
        if int(m.group(1)) == index:
            answers.append(m.group(2))
            if m.group(3):
                hints.append(m.group(3))
    return answers, hints


def cloze_plain(text: str) -> str:
    return CLOZE_RE.sub(lambda m: m.group(2), text)


def safe_relative(name: str, label: str) -> PurePosixPath:
    if not isinstance(name, str) or not name.strip():
        raise ManifestError(f"{label}: filename must be a nonempty string")
    rel = PurePosixPath(name.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts or name.startswith("~"):
        raise ManifestError(f"{label}: filename must be relative without '..': {name}")
    return rel


# ---------------------------------------------------------------- cloze expansion

def expand_cloze_notes(cards: list[Any]) -> list[Any]:
    """A ``kind: cloze`` entry whose cloze blocks have no ``index`` is a note: one card per cN."""
    out = []
    for i, raw in enumerate(cards):
        if not (isinstance(raw, dict) and raw.get("kind") == "cloze"):
            out.append(raw)
            continue
        blocks = [b for side in ("front", "back") for b in (raw.get(side) or []) if isinstance(b, dict) and b.get("type") == "cloze"]
        if not blocks or all("index" in b for b in blocks):
            out.append(raw)
            continue
        if any("index" in b for b in blocks):
            raise ManifestError(f"cards[{i}]: give index on every cloze block or on none (to expand the note)")
        if raw.get("history") or raw.get("state"):
            raise ManifestError(f"cards[{i}]: history/state belong to a single card; expand the cloze note yourself")
        key = _nonempty(raw.get("external_key"), f"cards[{i}].external_key")
        texts = {b.get("text") for b in blocks}
        front_text = next((b.get("text") for b in raw.get("front") or [] if isinstance(b, dict) and b.get("type") == "cloze"), None)
        if not isinstance(front_text, str):
            raise ManifestError(f"{key}: cloze note needs a cloze block on the front")
        if len(texts) != 1:
            raise ManifestError(f"{key}: back cloze block must repeat the front cloze text")
        indexes = cloze_indexes(front_text)
        if not indexes:
            raise ManifestError(f"{key}: cloze text has no {{{{cN::…}}}} deletion")
        for n in indexes:
            card = copy.deepcopy(raw)
            card["external_key"] = f"{key}:c{n}"
            for side in ("front", "back"):
                for b in card.get(side) or []:
                    if isinstance(b, dict) and b.get("type") == "cloze":
                        b["index"] = n
            answers, hints = cloze_answers(front_text, n)
            card.setdefault("term", " / ".join(answers))
            card.setdefault("definition", " / ".join(hints) or " / ".join(answers))
            card.setdefault("context", cloze_plain(front_text))
            out.append(card)
    return out


# ---------------------------------------------------------------- validation

class _Ctx:
    def __init__(self, media_dir: Path | None, require_files: bool):
        self.media_dir = media_dir
        self.require_files = require_files
        self.media: dict[str, MediaFile] = {}
        self.remote: set[str] = set()
        self._hash_cache: dict[Path, str] = {}

    def resolve(self, filename: str, declared: str | None, label: str) -> str | None:
        rel = safe_relative(filename, label)
        if self.media_dir is None:
            if declared:
                return declared
            if self.require_files:
                raise ManifestError(f"{label}: no media directory for {filename}")
            return None
        root = self.media_dir.resolve()
        path = (root / rel).resolve()
        if root != path and root not in path.parents:
            raise ManifestError(f"{label}: {filename} resolves outside the media directory")
        if not path.is_file():
            if self.require_files:
                raise ManifestError(f"{label}: media file not found: {filename}")
            return declared
        size = path.stat().st_size
        if size == 0:
            raise ManifestError(f"{label}: media file is empty: {filename}")
        if size > 20 * 1024 * 1024:
            raise ManifestError(f"{label}: media file is larger than 20 MB: {filename}")
        digest = self._hash_cache.get(path) or sha256_file(path)
        self._hash_cache[path] = digest
        if declared and declared != digest:
            raise ManifestError(f"{label}: sha256 of {filename} is {digest}, manifest says {declared}")
        self.media[digest] = MediaFile(digest, path, mime_for(path), size)
        return digest


def _blocks(raw: Any, label: str, ctx: _Ctx) -> tuple[list[dict], list[tuple[str, int]]]:
    if not isinstance(raw, list) or not raw:
        raise ManifestError(f"{label} must be a nonempty block array")
    if len(raw) > MAX_BLOCKS:
        raise ManifestError(f"{label} has more than {MAX_BLOCKS} blocks")
    out, clozes = [], []
    for j, b in enumerate(raw):
        where = f"{label}[{j}]"
        if not isinstance(b, dict):
            raise ManifestError(f"{where}: block must be an object")
        kind = b.get("type")

        def allow(keys):
            unknown = sorted(set(b) - set(keys))
            if unknown:
                raise ManifestError(f"{where}: unsupported {kind} block field: {', '.join(unknown)}")

        def lang_rtl():
            if "lang" in b:
                _nonempty(b["lang"], f"{where}.lang")
            if "rtl" in b and not isinstance(b["rtl"], bool):
                raise ManifestError(f"{where}: rtl must be boolean")

        if kind in TEXT_TYPES:
            allow(("type", "text", "lang", "rtl", "highlight"))
            _nonempty(b.get("text"), f"{where}.text")
            lang_rtl()
            if "highlight" in b:
                _nonempty(b["highlight"], f"{where}.highlight")
            out.append(dict(b))
        elif kind == "breakdown":
            allow(("type", "rows", "lang", "rtl"))
            rows = b.get("rows")
            if not isinstance(rows, list) or not rows:
                raise ManifestError(f"{where}: breakdown rows must be nonempty")
            for r in rows:
                if not isinstance(r, dict) or sorted(r) != ["source", "target"]:
                    raise ManifestError(f"{where}: breakdown row must be {{source, target}}")
                _nonempty(r["source"], f"{where} row source")
                _nonempty(r["target"], f"{where} row target")
            lang_rtl()
            out.append(dict(b))
        elif kind in MEDIA_TYPES:
            allow(("type", "filename", "sha256", "autoplay", "loudness_lufs"))
            declared = b.get("sha256")
            if declared is not None and (not isinstance(declared, str) or not SHA_RE.match(declared)):
                raise ManifestError(f"{where}: sha256 must be lowercase SHA-256 hex")
            if "autoplay" in b and not isinstance(b["autoplay"], bool):
                raise ManifestError(f"{where}: autoplay must be boolean")
            if "loudness_lufs" in b:
                value = b["loudness_lufs"]
                if (not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value)
                        or value > 0 or value < -70):
                    raise ManifestError(f"{where}: loudness_lufs must be a number between -70 and 0")
            if "filename" in b:
                digest = ctx.resolve(b["filename"], declared, where)
            elif declared:
                digest = declared
                ctx.remote.add(declared)
            else:
                raise ManifestError(f"{where}: media block needs filename (or sha256 of an uploaded file)")
            block = {"type": kind, "sha256": digest}
            for k in ("autoplay", "loudness_lufs"):
                if k in b:
                    block[k] = b[k]
            out.append(block)
        elif kind == "cloze":
            allow(("type", "text", "index", "lang", "rtl"))
            text = _nonempty(b.get("text"), f"{where}.text")
            index = b.get("index")
            if not _is_int(index) or not 1 <= index <= 99:
                raise ManifestError(f"{where}: cloze index must be an integer 1–99")
            if index not in cloze_indexes(text):
                raise ManifestError(f"{where}: cloze text has no {{{{c{index}::…}}}} deletion")
            lang_rtl()
            clozes.append((text, index))
            out.append(dict(b))
        else:
            raise ManifestError(f"{where}: unsupported block type: {kind}")
    return out, clozes


def _state(raw: Any, label: str) -> dict:
    if not isinstance(raw, dict):
        raise ManifestError(f"{label} must be an object")
    queue = raw.get("queue")
    if queue not in QUEUES:
        raise ManifestError(f"{label}: queue must be new | learning | review | relearning")
    out = {"queue": queue}
    for k in ("step", "reps", "lapses"):
        v = raw.get(k, 0)
        if v is None:
            v = 0
        if not _is_int(v) or v < 0:
            raise ManifestError(f"{label}: {k} must be a non-negative integer")
        out[k] = v
    for k, low, high in (("stability", 0, 1e6), ("difficulty", 1, 10)):
        v = raw.get(k)
        if v is not None and (not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or not low <= v <= high):
            raise ManifestError(f"{label}: {k} must be a number in [{low}, {high}]")
        out[k] = v
    last = raw.get("last_review")
    if last is not None and parse_utc(last) is None:
        raise ManifestError(f"{label}: last_review must be UTC ISO 8601 (…Z)")
    out["last_review"] = last
    if queue == "new":
        if raw.get("due") is not None:
            raise ManifestError(f"{label}: new cards must have due = null")
        pos = raw.get("new_position")
        if pos is not None and (not _is_int(pos) or pos < 0):
            raise ManifestError(f"{label}: new_position must be a non-negative integer")
        out["due"], out["new_position"] = None, pos
    else:
        if parse_utc(raw.get("due")) is None:
            raise ManifestError(f"{label}: due must be UTC ISO 8601 (…Z) for non-new cards")
        if raw.get("new_position") is not None:
            raise ManifestError(f"{label}: new_position must be null unless queue is new")
        if out["stability"] is None or out["difficulty"] is None:
            raise ManifestError(f"{label}: stability and difficulty are required unless queue is new")
        out["due"], out["new_position"] = raw["due"], None
    return out


def validate_card(raw: Any, label: str, ctx: _Ctx, now: datetime) -> dict:
    if not isinstance(raw, dict):
        raise ManifestError(f"{label} must be an object")
    unknown = sorted(set(raw) - CARD_KEYS)
    if unknown:
        raise ManifestError(f"{label}: unknown field(s): {', '.join(unknown)}")
    key = _nonempty(raw.get("external_key"), f"{label}.external_key")
    label = f"{label} ({key})"
    if len(key) > 512:
        raise ManifestError(f"{label}: external_key is longer than 512 chars")
    card: dict[str, Any] = {"external_key": key}
    for k in ("term", "definition", "context", "source"):
        card[k] = _nonempty(raw.get(k), f"{label}.{k}")
    for k in ("pos", "template", "lang"):
        if raw.get(k) is not None:
            card[k] = _nonempty(raw[k], f"{label}.{k}")
    kind = raw.get("kind", "basic")
    if kind not in KINDS:
        raise ManifestError(f"{label}: kind must be one of {', '.join(KINDS)}")
    if "kind" in raw:
        card["kind"] = kind
    if raw.get("direction") is not None:
        if raw["direction"] not in DIRECTIONS:
            raise ManifestError(f"{label}: direction must be one of {', '.join(DIRECTIONS)}")
        card["direction"] = raw["direction"]
    card["front"], front_clozes = _blocks(raw.get("front"), f"{label}.front", ctx)
    card["back"], back_clozes = _blocks(raw.get("back"), f"{label}.back", ctx)
    if kind == "cloze":
        if len(front_clozes) != 1:
            raise ManifestError(f"{label}: cloze card front needs exactly one cloze block")
        text, index = front_clozes[0]
        if any(c != (text, index) for c in back_clozes):
            raise ManifestError(f"{label}: back cloze block must repeat the front cloze block")
        suffix = f":c{index}"
        if not key.endswith(suffix) or key == suffix:
            raise ManifestError(f"{label}: cloze external_key must end with \"{suffix}\"")
    elif front_clozes or back_clozes:
        raise ManifestError(f"{label}: cloze blocks require kind \"cloze\"")
    if raw.get("history") is not None:
        if not isinstance(raw["history"], list):
            raise ManifestError(f"{label}: history must be an array")
        history = []
        for h in raw["history"]:
            if not isinstance(h, dict) or set(h) - {"rating", "reviewed_at", "duration_ms"}:
                raise ManifestError(f"{label}: history items are {{rating, reviewed_at, duration_ms}}")
            if not _is_int(h.get("rating")) or not 1 <= h["rating"] <= 4:
                raise ManifestError(f"{label}: history rating must be 1–4")
            at = parse_utc(h.get("reviewed_at"))
            if at is None or at > now + FUTURE_SKEW:
                raise ManifestError(f"{label}: history reviewed_at must be UTC ISO 8601 (…Z) and not in the future")
            if not _is_int(h.get("duration_ms")) or h["duration_ms"] < 0:
                raise ManifestError(f"{label}: history duration_ms must be a non-negative integer")
            history.append({"rating": h["rating"], "reviewed_at": h["reviewed_at"], "duration_ms": h["duration_ms"]})
        card["history"] = history
    if raw.get("state") is not None:
        card["state"] = _state(raw["state"], f"{label}.state")
    if raw.get("suspended") is not None:
        if not isinstance(raw["suspended"], bool):
            raise ManifestError(f"{label}: suspended must be boolean")
        card["suspended"] = raw["suspended"]
    return card


def prepare(data: dict, media_dir: Path | None, *, require_files: bool = True, now: datetime | None = None) -> Prepared:
    """Validate a whole manifest; resolve local media to sha256."""
    unknown = sorted(set(data) - MANIFEST_KEYS)
    if unknown:
        raise ManifestError(f"manifest: unknown field(s): {', '.join(unknown)}")
    batch_id = _nonempty(data.get("batch_id"), "batch_id")
    deck_id = _nonempty(data.get("deck_id"), "deck_id")
    if not DECK_ID_RE.match(deck_id):
        raise ManifestError("deck_id must match [a-z0-9][a-z0-9_-]{0,63}")
    raw_cards = data.get("cards")
    if not isinstance(raw_cards, list) or not raw_cards:
        raise ManifestError("cards must be a nonempty array")
    now = now or datetime.now(timezone.utc)
    ctx = _Ctx(media_dir, require_files)
    cards, seen = [], set()
    for i, raw in enumerate(expand_cloze_notes(raw_cards)):
        card = validate_card(raw, f"cards[{i}]", ctx, now)
        if card["external_key"] in seen:
            raise ManifestError(f"duplicate external_key: {card['external_key']}")
        seen.add(card["external_key"])
        cards.append(card)
    return Prepared(batch_id, deck_id, cards, ctx.media, ctx.remote - set(ctx.media))


def split_batches(prepared: Prepared, max_cards: int | None = None, max_bytes: int | None = None) -> list[dict]:
    """Deterministic chunks (≤ max_cards, ≤ max_bytes). One chunk keeps batch_id; more get ``:partNNN``."""
    max_cards = max_cards or MAX_BATCH_CARDS
    max_bytes = max_bytes or MAX_BATCH_BYTES
    chunks: list[list[dict]] = [[]]
    size = 0
    overhead = len(json.dumps({"batch_id": prepared.batch_id + ":part000", "deck_id": prepared.deck_id, "cards": []},
                              ensure_ascii=False).encode())
    for card in prepared.cards:
        card_size = len(json.dumps(card, ensure_ascii=False).encode()) + 1
        if card_size + overhead > max_bytes:
            raise ManifestError(f"{card['external_key']}: card is larger than one request can carry ({card_size} bytes)")
        if chunks[-1] and (len(chunks[-1]) >= max_cards or size + card_size + overhead > max_bytes):
            chunks.append([])
            size = 0
        chunks[-1].append(card)
        size += card_size
    if len(chunks) == 1:
        return [{"batch_id": prepared.batch_id, "deck_id": prepared.deck_id, "cards": chunks[0]}]
    return [{"batch_id": f"{prepared.batch_id}:part{i + 1:03d}", "deck_id": prepared.deck_id, "cards": c}
            for i, c in enumerate(chunks)]


def media_shas(batch: dict) -> set[str]:
    return {b["sha256"] for c in batch["cards"] for side in ("front", "back") for b in c[side] if b.get("type") in MEDIA_TYPES}


def api_blocks(blocks: list[dict]) -> list[dict]:
    """Server card blocks store media as ``media``; requests use ``sha256``."""
    out = []
    for b in blocks:
        if "media" in b:
            b = {**{k: v for k, v in b.items() if k != "media"}, "sha256": b["media"]}
        out.append(b)
    return out


def resolve_card(raw: dict, media_dir: Path | None, current: dict) -> tuple[dict, dict[str, MediaFile], set[str]]:
    """Validate a partial manifest card (``cards update``) merged over the current server card."""
    if not isinstance(raw, dict):
        raise ManifestError("card file must be a JSON object")
    ctx = _Ctx(media_dir, True)
    merged = {k: current.get(k) for k in ("external_key", "kind", "lang", "direction", "template")}
    merged.update(term=current.get("sort_key") or "-", definition="-", context="-", source="-",
                  front=api_blocks(current["front"]), back=api_blocks(current["back"]))
    merged = {k: v for k, v in merged.items() if v is not None}
    merged.update(raw)
    card = validate_card(merged, "card", ctx, datetime.now(timezone.utc))
    return card, ctx.media, ctx.remote - set(ctx.media)
