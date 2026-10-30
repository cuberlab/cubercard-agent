"""Convert an Anki ``.apkg`` into a Cuber Card manifest.

Reads ``collection.anki21b`` (zstd-compressed, schema 18), ``collection.anki21``
or ``collection.anki2`` (legacy JSON models), whichever is newest. Every Anki
card becomes one manifest card; cloze notes become one ``kind: cloze`` card per
``{{cN::…}}`` number. Fields become text blocks, ``[sound:x]`` becomes
audio/video, ``<img src>`` becomes image, other HTML is stripped. The revlog
becomes ``history[]`` and FSRS replay of it becomes the initial ``state``.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import zstandard

from . import fsrs
from .api import CliError
from .manifest import DECKS_FILE, KINDS, MAX_DECK_DEPTH, TEXT_TYPES, cloze_answers, cloze_indexes, cloze_plain

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
COLLECTIONS = ("collection.anki21b", "collection.anki21", "collection.anki2")
VIDEO_EXT = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".mpg", ".mpeg"}
SPECIAL_REFS = {"FrontSide", "Tags", "Deck", "Subdeck", "Card", "CardFlag", "Type", "CardID"}
SOUND_RE = re.compile(r"\[sound:([^\]]+)\]")
IMG_RE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))[^>]*>", re.I)
MEDIA_RE = re.compile(SOUND_RE.pattern + "|" + IMG_RE.pattern, re.I)
REF_RE = re.compile(r"\{\{([^{}]+)\}\}")
BLOCK_ROLES = set(TEXT_TYPES) | {"skip"}
MAPPING_KEYS = {"kind", "lang", "term", "definition", "context", "source", "blocks", "front", "back", "templates", "skip"}


class ApkgError(CliError):
    pass


@dataclass
class Template:
    ord: int
    name: str
    qfmt: str
    afmt: str


@dataclass
class NoteType:
    id: int
    name: str
    is_cloze: bool
    fields: list[str]
    templates: list[Template]


@dataclass
class Collection:
    note_types: dict[int, NoteType]
    decks: dict[int, str]
    notes: dict[int, dict]
    cards: list[dict]
    revlog: dict[int, list[dict]]
    media: dict[str, str]  # filename -> zip member
    zip: zipfile.ZipFile
    source_sha256: str
    collection_name: str


# ---------------------------------------------------------------- protobuf (minimal reader)

def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def proto_fields(buf: bytes) -> dict[int, list[Any]]:
    out: dict[int, list[Any]] = {}
    i = 0
    while i < len(buf):
        tag, i = _varint(buf, i)
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 2:
            length, i = _varint(buf, i)
            value, i = buf[i:i + length], i + length
        elif wire == 1:
            value, i = buf[i:i + 8], i + 8
        elif wire == 5:
            value, i = buf[i:i + 4], i + 4
        else:
            raise ApkgError(f"unsupported protobuf wire type {wire}")
        out.setdefault(number, []).append(value)
    return out


def _maybe_zstd(data: bytes) -> bytes:
    if data[:4] == ZSTD_MAGIC:
        return zstandard.ZstdDecompressor().stream_reader(data).read()
    return data


# ---------------------------------------------------------------- reading

def _media_map(zf: zipfile.ZipFile) -> dict[str, str]:
    if "media" not in zf.namelist():
        return {}
    raw = _maybe_zstd(zf.read("media"))
    try:
        data = json.loads(raw.decode("utf-8"))
        return {name: member for member, name in data.items()}
    except (UnicodeDecodeError, ValueError):
        pass
    out = {}
    for index, entry in enumerate(proto_fields(raw).get(1, [])):
        f = proto_fields(entry)
        name = f.get(1, [b""])[0].decode("utf-8")
        member = str(f[255][0]) if 255 in f else str(index)
        if name:
            out[name] = member
    return out


def _legacy_models(conn) -> tuple[dict[int, NoteType], dict[int, str]]:
    row = conn.execute("SELECT models, decks FROM col").fetchone()
    models = json.loads(row[0] or "{}")
    decks = json.loads(row[1] or "{}")
    types = {}
    for mid, m in models.items():
        fields = [f["name"] for f in sorted(m.get("flds", []), key=lambda f: f.get("ord", 0))]
        tmpls = [Template(int(t.get("ord", i)), t.get("name", f"Card {i + 1}"), t.get("qfmt", ""), t.get("afmt", ""))
                 for i, t in enumerate(m.get("tmpls", []))]
        types[int(mid)] = NoteType(int(mid), m.get("name", str(mid)), m.get("type", 0) == 1, fields, tmpls)
    return types, {int(k): v.get("name", k) for k, v in decks.items()}


def _schema18_models(conn) -> tuple[dict[int, NoteType], dict[int, str]]:
    types = {}
    for ntid, name, config in conn.execute("SELECT id, name, config FROM notetypes"):
        kind = proto_fields(config or b"").get(1, [0])[0]
        fields = [r[0] for r in conn.execute("SELECT name FROM fields WHERE ntid = ? ORDER BY ord", (ntid,))]
        tmpls = []
        for ord_, tname, tconfig in conn.execute("SELECT ord, name, config FROM templates WHERE ntid = ? ORDER BY ord", (ntid,)):
            f = proto_fields(tconfig or b"")
            q = f.get(1, [b""])[0].decode("utf-8")
            a = f.get(2, [b""])[0].decode("utf-8")
            tmpls.append(Template(ord_, tname, q, a))
        types[ntid] = NoteType(ntid, name, kind == 1, fields, tmpls)
    decks = {did: name.replace("\x1f", "::") for did, name in conn.execute("SELECT id, name FROM decks")}
    return types, decks


def read_apkg(path: str | Path) -> Collection:
    path = Path(path)
    try:
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as error:
        raise ApkgError(f"{path} is not a readable .apkg (zip): {error}") from error
    names = set(zf.namelist())
    member = next((n for n in COLLECTIONS if n in names), None)
    if member is None:
        raise ApkgError(f"{path}: no collection.anki21b / anki21 / anki2 inside")
    data = _maybe_zstd(zf.read(member))
    conn = sqlite3.connect(":memory:")
    conn.deserialize(data)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    note_types, decks = _schema18_models(conn) if "notetypes" in tables else _legacy_models(conn)
    notes = {nid: {"id": nid, "mid": mid, "fields": flds.split("\x1f"), "tags": tags.split()}
             for nid, mid, flds, tags in conn.execute("SELECT id, mid, flds, tags FROM notes")}
    cards = [dict(zip(("id", "nid", "did", "ord", "type", "queue", "due", "odid"), r))
             for r in conn.execute("SELECT id, nid, did, ord, type, queue, due, odid FROM cards ORDER BY nid, ord")]
    for card in cards:
        # A card in a filtered deck lives in odid; that is its real deck.
        card["home"] = card["odid"] or card["did"]
    revlog: dict[int, list[dict]] = {}
    for rid, cid, ease, ivl, ms, rtype in conn.execute("SELECT id, cid, ease, ivl, time, type FROM revlog ORDER BY id"):
        revlog.setdefault(cid, []).append({"id": rid, "ease": ease, "ivl": ivl, "time": ms, "type": rtype})
    conn.close()
    from .manifest import sha256_file
    digest = sha256_file(path)
    return Collection(note_types, decks, notes, cards, revlog, _media_map(zf), zf, digest, member)


# ---------------------------------------------------------------- field conversion

def extract_media(value: str) -> list[tuple[str, str]]:
    """[(block_type, filename)] in order of appearance."""
    out = []
    for m in MEDIA_RE.finditer(value):
        if m.group(1) is not None:
            name = html.unescape(m.group(1).strip())
            out.append(("video" if Path(name).suffix.lower() in VIDEO_EXT else "audio", name))
        else:
            name = html.unescape((m.group(2) or m.group(3) or m.group(4) or "").strip())
            if name and not re.match(r"^(https?:|data:)", name, re.I):
                out.append(("image", name))
    return out


def drop_front_repeats(back: list[dict], front: list[dict], *, keep_if_empty: bool = True) -> list[dict]:
    """Anki's ``{{FrontSide}}`` repeats the front on the back. Cuber Card keeps the front on screen when
    the answer is shown, so a back block whose content is already on the front is left out (autoplay
    does not count). A back that would become empty is kept as is, so the card still imports."""
    def key(b: dict):
        return tuple(sorted((k, json.dumps(v, ensure_ascii=False, sort_keys=True)) for k, v in b.items() if k != "autoplay"))
    seen = {key(b) for b in front}
    kept = [b for b in back if key(b) not in seen]
    return kept or (back if keep_if_empty else [])


def strip_html(value: str) -> str:
    text = MEDIA_RE.sub(" ", value)
    text = re.sub(r"(?is)<(style|script)\b.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</(div|p|li|tr|h[1-6])>|<(div|p|li|tr|h[1-6])\b[^>]*>", "\n", text)
    text = re.sub(r"(?s)<[^>]*>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def template_refs(fmt: str, fields: list[str], *, answer: bool = False) -> list[str]:
    out = []
    for m in REF_RE.finditer(fmt):
        inner = m.group(1).strip()
        if not inner or inner[0] in "#^/!":
            continue
        parts = [p.strip() for p in inner.split(":")]
        name, filters = parts[-1], parts[:-1]
        if "type" in filters and not answer:
            continue  # typed-answer box: the field is not shown on this face
        if name == "FrontSide" and "FrontSide" not in out:
            out.append("FrontSide")
        elif name in fields and name not in out:
            out.append(name)
    return out


def template_faces(nt: NoteType, tmpl: Template) -> tuple[list[str], list[str]]:
    front = [r for r in template_refs(tmpl.qfmt, nt.fields) if r != "FrontSide"]
    back_refs = template_refs(tmpl.afmt, nt.fields, answer=True)
    back: list[str] = []
    for r in back_refs:
        for name in (front if r == "FrontSide" else [r]):
            if name not in back:
                back.append(name)
    if not front:
        front = nt.fields[:1]
    if not back:
        back = [f for f in nt.fields if f not in front] or list(front)
    return front, back


def cloze_field(nt: NoteType) -> str:
    for tmpl in nt.templates:
        m = re.search(r"\{\{[^{}]*cloze:([^{}]+)\}\}", tmpl.qfmt)
        if m and m.group(1).strip() in nt.fields:
            return m.group(1).strip()
    return nt.fields[0]


# ---------------------------------------------------------------- mapping

@dataclass
class TypePlan:
    nt: NoteType
    kind: str
    lang: str | None
    source: str | None
    term: str
    definition: str
    context: str | None
    roles: dict[str, str]
    faces: dict[int, tuple[list[str], list[str]]]  # template ord -> (front fields, back fields)
    skip: bool = False
    cloze_text: str | None = None
    notes: int = 0
    cards: int = 0


def load_mapping(path: str | Path | None) -> dict:
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as error:
        raise ApkgError(f"cannot read mapping {path}: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("note_types", {}), dict):
        raise ApkgError("mapping must be {\"note_types\": {\"<note type name>\": {...}}}")
    return data


def plan_types(col: Collection, mapping: dict) -> dict[int, TypePlan]:
    configs = mapping.get("note_types", {})
    by_name = {nt.name: nt for nt in col.note_types.values()}
    unknown = sorted(set(configs) - set(by_name))
    if unknown:
        raise ApkgError(f"mapping names unknown note type(s): {', '.join(unknown)}; present: {', '.join(sorted(by_name))}")
    plans = {}
    for nt in col.note_types.values():
        cfg = configs.get(nt.name, {})
        bad_keys = sorted(set(cfg) - MAPPING_KEYS)
        if bad_keys:
            raise ApkgError(f"mapping {nt.name}: unknown key(s) {', '.join(bad_keys)}")

        def need_field(name, label):
            if name is not None and name not in nt.fields:
                raise ApkgError(f"mapping {nt.name}.{label}: no field named {name!r} (fields: {', '.join(nt.fields)})")
            return name

        if not nt.fields:
            continue
        term = need_field(cfg.get("term"), "term") or (cloze_field(nt) if nt.is_cloze else nt.fields[0])
        definition = need_field(cfg.get("definition"), "definition") or (nt.fields[1] if len(nt.fields) > 1 else nt.fields[0])
        context = need_field(cfg.get("context"), "context") or (nt.fields[2] if len(nt.fields) > 2 and not nt.is_cloze else None)
        roles = {}
        for i, name in enumerate(nt.fields):
            roles[name] = "title" if i == 0 else "translation" if i == 1 else "text"
        if nt.is_cloze:
            roles = {name: "text" for name in nt.fields}
            roles[cloze_field(nt)] = "cloze"
        for name, role in (cfg.get("blocks") or {}).items():
            need_field(name, "blocks")
            if role not in BLOCK_ROLES:
                raise ApkgError(f"mapping {nt.name}.blocks.{name}: must be one of {', '.join(sorted(BLOCK_ROLES))}")
            if nt.is_cloze and roles.get(name) == "cloze" and role != "skip":
                continue
            roles[name] = role
        kind = cfg.get("kind", "cloze" if nt.is_cloze else "basic")
        if kind not in KINDS or (kind == "cloze") != nt.is_cloze:
            raise ApkgError(f"mapping {nt.name}.kind: {kind!r} is not valid for this note type")
        faces = {}
        for tmpl in nt.templates:
            front, back = template_faces(nt, tmpl)
            override = (cfg.get("templates") or {}).get(tmpl.name) or (cfg.get("templates") or {}).get(str(tmpl.ord)) or {}
            front = override.get("front", cfg.get("front", front))
            back = override.get("back", cfg.get("back", back))
            for name in list(front) + list(back):
                need_field(name, f"{tmpl.name}.front/back")
            faces[tmpl.ord] = (list(front), list(back))
        plans[nt.id] = TypePlan(nt, kind, cfg.get("lang"), cfg.get("source"), term, definition, context, roles, faces,
                                skip=bool(cfg.get("skip")), cloze_text=cloze_field(nt) if nt.is_cloze else None)
    return plans


# ---------------------------------------------------------------- conversion

def _iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def history_and_state(card: dict, revlog: list[dict]) -> tuple[list[dict], dict | None]:
    """Graded revlog rows → history; FSRS replay after the last Forget → state (None = new card)."""
    history, events = [], []
    for row in revlog:
        if row["type"] == 4 and row["ease"] == 0 and row["ivl"] == 0:
            events = []  # Forget: the card went back to new
            continue
        if row["ease"] not in (1, 2, 3, 4) or row["type"] in (4, 5):
            continue
        at = _iso_ms(row["id"])
        history.append({"rating": row["ease"], "reviewed_at": at, "duration_ms": max(0, int(row["time"] or 0))})
        events.append({"event_id": f"{row['id']:016d}", "rating": row["ease"], "reviewed_at": at})
    if card["type"] == 0 or not events:
        return history, None
    state = fsrs.replay(events)
    state.pop("state_version", None)
    return history, state


class Converter:
    def __init__(self, col: Collection, plans: dict[int, TypePlan]):
        self.col = col
        self.plans = plans
        self.used_media: dict[str, str] = {}
        self.missing_media: set[str] = set()
        self.warnings: list[str] = []
        self.home: dict[str, int | None] = {}  # external_key -> Anki deck id the card lives in

    def _media_block(self, kind: str, name: str, autoplay: bool | None = None) -> dict | None:
        if "/" in name or "\\" in name or name.startswith(".") or name not in self.col.media:
            self.missing_media.add(name)
            return None
        self.used_media[name] = self.col.media[name]
        block = {"type": kind, "filename": "media/" + name}
        if autoplay is not None and kind in ("audio", "video"):
            block["autoplay"] = autoplay
        return block

    def field_blocks(self, plan: TypePlan, note: dict, names: list[str], *, repeat: set[str] = frozenset()) -> list[dict]:
        out = []
        for name in names:
            role = plan.roles.get(name, "text")
            if role in ("skip", "cloze"):
                continue
            value = note["fields"][plan.nt.fields.index(name)] if plan.nt.fields.index(name) < len(note["fields"]) else ""
            text = strip_html(value)
            if text:
                out.append({"type": role, "text": text, **({"lang": plan.lang} if plan.lang else {})})
            for kind, filename in extract_media(value):
                block = self._media_block(kind, filename, False if name in repeat else None)
                if block:
                    out.append(block)
        return out

    def field_text(self, plan: TypePlan, note: dict, name: str | None) -> str:
        if not name:
            return ""
        i = plan.nt.fields.index(name)
        value = note["fields"][i] if i < len(note["fields"]) else ""
        text = strip_html(value)
        if not text:
            media = extract_media(value)
            text = media[0][1] if media else ""
        return text

    def source(self, plan: TypePlan, card: dict | None) -> str:
        if plan.source:
            return plan.source
        deck = self.col.decks.get(card["home"]) if card else None
        return f"Anki: {deck}" if deck else "Anki import"

    def convert(self) -> list[tuple[dict, dict | None]]:
        """[(manifest card, anki card)]"""
        out = []
        cards_by_note: dict[int, list[dict]] = {}
        for card in self.col.cards:
            cards_by_note.setdefault(card["nid"], []).append(card)
        for nid, note in sorted(self.col.notes.items()):
            plan = self.plans.get(note["mid"])
            if plan is None or plan.skip:
                continue
            plan.notes += 1
            anki_cards = cards_by_note.get(nid, [])
            made = self._cloze(plan, note, anki_cards) if plan.nt.is_cloze else self._basic(plan, note, anki_cards)
            for card, anki in made:
                # A cloze number without its own Anki card stays with the note's other cards.
                owner = anki or (anki_cards[0] if anki_cards else None)
                self.home[card["external_key"]] = owner["home"] if owner else None
            plan.cards += len(made)
            out += made
        return out

    def _finish(self, base: dict, anki_card: dict | None) -> dict:
        if anki_card is not None:
            history, state = history_and_state(anki_card, self.col.revlog.get(anki_card["id"], []))
            if history:
                base["history"] = history
            if state:
                base["state"] = state
            elif anki_card["type"] != 0:
                self.warnings.append(f"{base['external_key']}: Anki says reviewed but no graded revlog; imported as new")
            if anki_card["queue"] == -1:
                base["suspended"] = True
        return base

    def _basic(self, plan: TypePlan, note: dict, anki_cards: list[dict]) -> list:
        out = []
        for card in anki_cards:
            front_names, back_names = plan.faces.get(card["ord"], (plan.nt.fields[:1], plan.nt.fields[1:]))
            front = self.field_blocks(plan, note, front_names)
            back = drop_front_repeats(self.field_blocks(plan, note, back_names, repeat=set(front_names)), front)
            key = f"anki:{note['id']}:{card['ord']}"
            if not front or not back:
                self.warnings.append(f"{key}: {'front' if not front else 'back'} is empty after conversion; skipped")
                continue
            term = self.field_text(plan, note, plan.term) or "(empty)"
            base = {"external_key": key, "term": term,
                    "definition": self.field_text(plan, note, plan.definition) or term,
                    "context": self.field_text(plan, note, plan.context) or term,
                    "source": self.source(plan, card), "front": front, "back": back}
            if plan.kind != "basic":
                base["kind"] = plan.kind
            if plan.lang:
                base["lang"] = plan.lang
            out.append((self._finish(base, card), card))
        return out

    def _cloze(self, plan: TypePlan, note: dict, anki_cards: list[dict]) -> list:
        name = plan.cloze_text
        value = note["fields"][plan.nt.fields.index(name)]
        text = strip_html(value)
        # The API's cloze syntax is single-line inside each deletion. Preserve
        # the answer/hint and line breaks outside deletions instead of losing cards.
        text = re.sub(r"\{\{c\d+::[^{}]*\}\}", lambda match: " ".join(match[0].splitlines()), text)
        indexes = cloze_indexes(text)
        if not indexes:
            self.warnings.append(f"anki:{note['id']}: cloze note has no {{{{cN::}}}} in {name!r}; skipped")
        by_ord = {c["ord"]: c for c in anki_cards}
        for c in anki_cards:
            if c["ord"] + 1 not in indexes:
                self.warnings.append(f"anki:{note['id']}:c{c['ord'] + 1}: Anki card has no matching cloze in the text; skipped")
        front_names, back_names = plan.faces[plan.nt.templates[0].ord]
        media = [b for b in (self._media_block(k, f) for k, f in extract_media(value)) if b]
        front_extra = [*media, *self.field_blocks(plan, note, front_names)]
        back_extra = drop_front_repeats(
            self.field_blocks(plan, note, back_names, repeat=set(front_names)), front_extra, keep_if_empty=False)
        out = []
        for n in indexes:
            card = by_ord.get(n - 1)
            cloze = {"type": "cloze", "text": text, "index": n, **({"lang": plan.lang} if plan.lang else {})}
            answers, hints = cloze_answers(text, n)
            base = {"external_key": f"anki:{note['id']}:c{n}", "kind": "cloze", "term": " / ".join(answers),
                    "definition": (strip_html(note["fields"][plan.nt.fields.index(plan.definition)])
                                   if plan.definition != name and plan.definition in plan.nt.fields else "")
                    or " / ".join(hints) or " / ".join(answers),
                    "context": cloze_plain(text), "source": self.source(plan, card or (anki_cards[0] if anki_cards else None)),
                    # The back repeats the cloze (the app shows it once, answered); the front's media is not repeated.
                    "front": [cloze, *front_extra], "back": [cloze, *back_extra]}
            if plan.lang:
                base["lang"] = plan.lang
            out.append((self._finish(base, card), card))
        return out


def order_key(item: tuple[dict, dict | None]) -> tuple:
    card, anki = item
    is_new = "state" not in card
    due = anki["due"] if (anki and is_new and anki["type"] == 0) else 0
    return (is_new, due, anki["nid"] if anki else 0, anki["ord"] if anki else 0, card["external_key"])


def deck_path(name: str) -> list[str]:
    return [part.strip() or "(unnamed)" for part in name.split("::")]


def anki_deck_id(path: list[str], by_path: dict[str, int]) -> str:
    """``anki-<Anki deck id>`` when Anki has a deck for this path, else ``anki-path-<SHA-256 of A::B, 12 hex>``."""
    full = "::".join(path)
    if full in by_path:
        return f"anki-{by_path[full]}"
    return "anki-path-" + hashlib.sha256(full.encode("utf-8")).hexdigest()[:12]


def deck_tree(col: Collection, used: set[int]) -> tuple[list[dict], dict[int, str]]:
    """Anki ``A::B::C`` → three Cuber Card decks. Returns (decks.json entries top-down, Anki deck id → leaf deck id).
    Only decks that hold cards and their ancestors are included."""
    by_path = {"::".join(deck_path(name)): did for did, name in col.decks.items()}
    nodes: dict[tuple[str, ...], dict] = {}
    leaf: dict[int, str] = {}
    for did in sorted(used):
        path = deck_path(col.decks.get(did) or f"Anki deck {did}")
        for depth in range(1, len(path) + 1):
            prefix = tuple(path[:depth])
            if prefix in nodes:
                continue
            if depth == len(path) and did not in col.decks:
                deck_id = f"anki-{did}"
            else:
                deck_id = anki_deck_id(list(prefix), by_path)
            parent = nodes[prefix[:-1]]["id"] if depth > 1 else None
            nodes[prefix] = {"id": deck_id, "name": prefix[-1], "parent": parent}
        leaf[did] = nodes[tuple(path)]["id"]
    ordered = sorted(nodes.items(), key=lambda item: tuple(p.casefold() for p in item[0]))
    return [node for _, node in ordered], leaf


def convert(path: str | Path, deck_id: str | None, out_dir: str | Path, mapping: dict | None = None,
            batch_id: str | None = None, write: bool = True) -> dict:
    """``deck_id`` given: every card goes to that deck, one ``manifest.json``, no ``decks.json``.
    ``deck_id`` None: the Anki deck tree becomes ``decks.json``; one manifest per Anki deck that has cards
    (``manifest.json`` when there is one, else ``manifest-<deck id>.json``), its ``deck_id`` being that deck."""
    if write and deck_id and (Path(out_dir) / DECKS_FILE).exists():
        raise ApkgError(f"{out_dir}: contains {DECKS_FILE}; use a fresh --out directory with --deck")
    col = read_apkg(path)
    plans = plan_types(col, mapping or {})
    conv = Converter(col, plans)
    items = sorted(conv.convert(), key=order_key)
    base_batch = batch_id or f"anki:{col.source_sha256[:16]}"
    if deck_id and batch_id is None:
        base_batch += ':' + deck_id
    decks = None
    if deck_id:
        groups = {deck_id: [card for card, _ in items]}
    else:
        if not items:
            raise ApkgError(f"{path}: no cards to import")
        homeless = [card["external_key"] for card, _ in items if conv.home.get(card["external_key"]) is None]
        if homeless:
            raise ApkgError(f"no Anki deck for {', '.join(homeless[:5])}; give --deck to put every card in one deck")
        decks, leaf = deck_tree(col, {conv.home[card["external_key"]] for card, _ in items})
        groups = {}
        for card, _ in items:
            groups.setdefault(leaf[conv.home[card["external_key"]]], []).append(card)
        for d in decks:
            depth, parent = 1, d["parent"]
            while parent:
                depth += 1
                parent = next(x["parent"] for x in decks if x["id"] == parent)
            if depth > MAX_DECK_DEPTH:
                conv.warnings.append(f"deck {d['id']} ({d['name']}) is level {depth}; the server allows {MAX_DECK_DEPTH}")
    single = len(groups) == 1
    manifests = []
    for leaf_id, cards in groups.items():
        manifests.append({
            "deck_id": leaf_id,
            "file": "manifest.json" if single else f"manifest-{leaf_id}.json",
            "manifest": {"batch_id": base_batch if single else f"{base_batch}:{leaf_id}", "deck_id": leaf_id, "cards": cards},
        })
    report = {"manifests": manifests, "manifest": manifests[0]["manifest"] if single else None, "decks": decks,
              "plans": plans, "warnings": conv.warnings, "missing_media": sorted(conv.missing_media),
              "media": sorted(conv.used_media), "collection": col.collection_name}
    if write:
        out = Path(out_dir)
        expected = {m["file"] for m in manifests}
        existing = {p.name for p in out.glob("manifest-*.json")}
        if (out / "manifest.json").exists():
            existing.add("manifest.json")
        stale = existing - expected
        if stale:
            col.zip.close()
            raise ApkgError(f"{out_dir}: contains stale manifests {', '.join(sorted(stale))}; use a fresh --out directory")
        (out / "media").mkdir(parents=True, exist_ok=True)
        for name, member in conv.used_media.items():
            data = _maybe_zstd(col.zip.read(member))
            (out / "media" / name).write_bytes(data)
        for m in manifests:
            m["path"] = str(out / m["file"])
            Path(m["path"]).write_text(json.dumps(m["manifest"], ensure_ascii=False, indent=2) + "\n")
        if decks is not None:
            report["decks_path"] = str(out / DECKS_FILE)
            Path(report["decks_path"]).write_text(json.dumps(decks, ensure_ascii=False, indent=2) + "\n")
        report["path"] = manifests[0]["path"] if single else None
    col.zip.close()
    return report


def tree_lines(decks: list[dict], counts: dict[str, int]) -> list[str]:
    depth: dict[str, int] = {}
    lines = []
    for d in decks:
        depth[d["id"]] = depth[d["parent"]] + 1 if d["parent"] else 0
        n = counts.get(d["id"], 0)
        lines.append(f"  {'  ' * depth[d['id']]}{d['name']}  [{d['id']}]" + (f"  cards={n}" if n else ""))
    return lines


def preview_lines(report: dict) -> list[str]:
    lines = [f"collection: {report['collection']}"]
    for plan in report["plans"].values():
        if plan.notes == 0 and not plan.skip:
            continue
        lines.append("")
        lines.append(f"note type: {plan.nt.name}  kind={plan.kind}  notes={plan.notes}  cards={plan.cards}"
                     + ("  (skipped by mapping)" if plan.skip else ""))
        lines.append(f"  term={plan.term}  definition={plan.definition}  context={plan.context or '(term)'}"
                     + (f"  lang={plan.lang}" if plan.lang else ""))
        for name in plan.nt.fields:
            lines.append(f"  field {name!r:24} -> {plan.roles.get(name, 'text')}")
        if plan.nt.is_cloze:
            lines.append(f"  cloze text field: {plan.cloze_text}; one card per {{{{cN::}}}}")
        else:
            for tmpl in plan.nt.templates:
                front, back = plan.faces.get(tmpl.ord, ([], []))
                lines.append(f"  template {tmpl.ord} {tmpl.name!r}: front={front} back={back}")
    lines.append("")
    if report["decks"] is None:
        lines.append(f"deck: {report['manifests'][0]['deck_id']} (all cards; the Anki deck tree is not used)")
    else:
        counts = {m["deck_id"]: len(m["manifest"]["cards"]) for m in report["manifests"]}
        lines.append(f"deck tree ({DECKS_FILE}, {len(report['decks'])} decks, {len(report['manifests'])} with cards):")
        lines += tree_lines(report["decks"], counts)
    cards = [c for m in report["manifests"] for c in m["manifest"]["cards"]]
    reviewed = sum(1 for c in cards if "state" in c)
    lines.append("")
    lines.append(f"total cards: {len(cards)} (with state: {reviewed}, new: {len(cards) - reviewed}, "
                 f"history rows: {sum(len(c.get('history', [])) for c in cards)}, media files: {len(report['media'])})")
    if report["missing_media"]:
        lines.append(f"missing media (dropped): {', '.join(report['missing_media'][:20])}"
                     + (" …" if len(report["missing_media"]) > 20 else ""))
    for w in report["warnings"][:50]:
        lines.append("warning: " + w)
    if len(report["warnings"]) > 50:
        lines.append(f"… {len(report['warnings']) - 50} more warnings")
    return lines
