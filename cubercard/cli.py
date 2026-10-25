"""cubercard: command-line client for the Cuber Card API."""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, apkg, manifest as mf
from .api import ApiError, CliError, Client, resolve, write_credentials

SUSPEND_REASONS = ("too_easy", "too_hard", "not_interested", "agent", "import")


def out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def table(rows: list[list], headers: list[str]) -> None:
    rows = [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip())
    for r in rows:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip())


def mib(n: int | None) -> str:
    return "?" if n is None else f"{n / 1024 / 1024:.1f} MiB"


# ---------------------------------------------------------------- auth / status

def cmd_auth(args) -> int:
    base, _ = resolve(base=args.base)
    if sys.stdin.isatty():
        key = getpass.getpass("Cuber Card API key (ck_live_…): ")
    else:
        key = sys.stdin.readline()
    key = key.strip()
    if not key:
        raise CliError("no key given")
    client = Client(base, key)
    try:
        me = client.me()
    finally:
        client.close()
    path = write_credentials(base, key)
    print(f"authenticated as {me.get('email') or me.get('id')} at {base}")
    print(f"saved to {path} (mode 600)")
    return 0


def cmd_status(args, client: Client) -> int:
    me = client.me()
    decks = client.decks()
    if args.json:
        out({"me": me, "decks": decks})
        return 0
    print(f"base:    {client.base}")
    print(f"account: {me.get('email') or '-'} ({me.get('id')})")
    logins = ", ".join(i.get("provider", "?") for i in me.get("identities", []))
    print(f"logins:  {logins or '-'}")
    print(f"storage: {mib(me.get('storage_used_bytes'))} / {mib(me.get('storage_quota_bytes'))}")
    print()
    table([[d["id"], d["name"], d["new_per_day"], d["card_count"]] for d in decks], ["deck", "name", "new/day", "cards"])
    return 0


# ---------------------------------------------------------------- decks

def cmd_decks(args, client: Client) -> int:
    action = args.action or "list"
    if action == "list":
        decks = client.decks()
        if args.json:
            out(decks)
        else:
            table([[d["id"], d["name"], d["new_per_day"], d["card_count"]] for d in decks], ["deck", "name", "new/day", "cards"])
        return 0
    if action == "create":
        body = {"name": args.name}
        if args.id:
            body["id"] = args.id
        if args.new_per_day is not None:
            body["new_per_day"] = args.new_per_day
        deck = client.create_deck(body)
        print(f"created deck {deck['id']} ({deck['name']}, new/day {deck['new_per_day']})")
        return 0
    if action == "set":
        body = {}
        if args.new_per_day is not None:
            body["new_per_day"] = args.new_per_day
        if args.name:
            body["name"] = args.name
        if not body:
            raise CliError("decks set: give --new-per-day and/or --name")
        deck = client.patch_deck(args.id_arg, body)
        print(f"deck {deck['id']}: name={deck['name']} new/day={deck['new_per_day']} cards={deck['card_count']}")
        return 0
    raise CliError(f"unknown decks action: {action}")


# ---------------------------------------------------------------- create

def default_media_dir(manifest_path: Path, media_dir: str | None) -> Path:
    return Path(media_dir) if media_dir else manifest_path.resolve().parent


def upload_media(client: Client, files: dict[str, mf.MediaFile], remote: set[str]) -> tuple[int, int]:
    shas = sorted(set(files) | remote)
    if not shas:
        return 0, 0
    check = client.media_check(shas)
    missing = set(check["missing"])
    absent_remote = sorted(missing & remote)
    if absent_remote:
        raise CliError("media referenced only by sha256 is not uploaded yet: " + ", ".join(absent_remote))
    uploaded = 0
    for sha in sorted(missing):
        f = files[sha]
        res = client.put_media(sha, f.path, f.mime)
        uploaded += 1
        print(f"uploaded {f.path.name} {sha[:12]} {res.get('size')} bytes ({res.get('status')})")
    return uploaded, len(shas) - len(missing)


def cmd_create(args) -> int:
    path = Path(args.manifest)
    data = mf.load(path)
    prepared = mf.prepare(data, default_media_dir(path, args.media_dir))
    batches = mf.split_batches(prepared)
    print(f"manifest ok: {len(prepared.cards)} cards, deck {prepared.deck_id}, {len(batches)} batch(es), "
          f"{len(prepared.media)} local media file(s)")
    if args.dry_run:
        for i, b in enumerate(batches, 1):
            print(f"  batch {i}/{len(batches)} {b['batch_id']}: {len(b['cards'])} cards")
        _, key = resolve()
        if key:
            client = Client.from_env()
            try:
                decks = {d["id"] for d in client.decks()}
                print(f"deck {prepared.deck_id}: {'exists' if prepared.deck_id in decks else 'NOT FOUND (create it first)'}")
                shas = sorted(set(prepared.media) | prepared.remote_media)
                if shas:
                    check = client.media_check(shas)
                    print(f"media: {len(check['present'])} already uploaded, {len(check['missing'])} to upload")
            finally:
                client.close()
        print("dry run: nothing uploaded or created")
        return 0
    client = Client.from_env()
    try:
        uploaded, present = upload_media(client, prepared.media, prepared.remote_media)
        print(f"media: {uploaded} uploaded, {present} already present")
        totals = {"created": 0, "existing": 0}
        for i, batch in enumerate(batches, 1):
            try:
                res = client.batch(batch)
            except ApiError as error:
                print(f"batch {i}/{len(batches)} ({batch['batch_id']}, {len(batch['cards'])} cards) failed; "
                      f"earlier batches are saved, rerun is safe", file=sys.stderr)
                raise error
            for card in res["cards"]:
                print(f"{card['status']:8} {card['external_key']}  {card['id']}")
            totals["created"] += res["created"]
            totals["existing"] += res["existing"]
            print(f"batch {i}/{len(batches)} {batch['batch_id']}: created {res['created']}, existing {res['existing']}"
                  f"{', replay' if res.get('replay') else ''}, history rows {res.get('history_inserted', 0)}")
        print(f"done: created {totals['created']}, existing {totals['existing']}")
    finally:
        client.close()
    return 0


# ---------------------------------------------------------------- cards

def card_line(c: dict) -> list:
    s = c.get("state") or {}
    when = s.get("due") if s.get("queue") != "new" else f"#{s.get('new_position')}"
    return [c["id"], c["external_key"], c["kind"], s.get("queue"), when, "yes" if c.get("suspended") else ""]


def cmd_cards(args, client: Client) -> int:
    action = args.action
    if action == "list":
        cards, after = [], None
        while True:
            res = client.cards(deck_id=args.deck, external_key=args.external_key, after=after, limit=args.limit)
            cards += res["cards"]
            after = res.get("next_after")
            if not args.all or not after:
                break
        if args.json:
            out(cards)
        else:
            table([card_line(c) for c in cards], ["id", "external_key", "kind", "queue", "due/new#", "suspended"])
            if after and not args.all:
                print(f"(more: --all, or continue after {after})")
        return 0
    if action == "get":
        out(client.card(args.id))
        return 0
    if action == "update":
        raw = mf.load(args.manifest_card)
        media_dir = default_media_dir(Path(args.manifest_card), args.media_dir)
        current = client.card(args.id)
        card, files, remote = mf.resolve_card(raw, media_dir, current)
        for k in ("external_key", "kind"):
            if k in raw and raw[k] != current.get(k):
                raise CliError(f"{k} cannot be changed ({current.get(k)!r} → {raw[k]!r}); create a new card instead")
        upload_media(client, files, remote)
        body = {k: card[k] for k in ("term", "definition", "context", "source", "pos", "template", "lang", "direction", "front", "back")
                if k in raw}
        body["based_on_content_version"] = current["content_version"]
        res = client.patch_card(args.id, body)
        print(f"updated {res['id']}: content_version {current['content_version']} → {res['content_version']}")
        return 0
    if action in ("suspend", "unsuspend"):
        body = {"suspended": action == "suspend"}
        if action == "suspend":
            body["suspend_reason"] = args.reason
        res = client.patch_state(args.id, body)
        print(f"{res['id']}: suspended={res['suspended']}" + (f" ({res['suspend_reason']})" if res.get("suspend_reason") else ""))
        return 0
    raise CliError(f"unknown cards action: {action}")


# ---------------------------------------------------------------- schedule

def to_utc(value: str) -> str:
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CliError(f"--due must be ISO 8601: {value}") from error
    if when.tzinfo is None:
        raise CliError("--due needs a time zone (e.g. 2026-11-01T08:00:00+08:00 or …Z)")
    return when.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def cmd_schedule(args, client: Client) -> int:
    chosen = [args.due is not None, args.new_position is not None, args.reset]
    if sum(chosen) != 1:
        raise CliError("schedule: give exactly one of --due, --new-position, --reset")
    card = client.card(args.id)
    before = card["state"]
    body = {"based_on_version": before["state_version"]}
    if args.due is not None:
        body["due"] = to_utc(args.due)
    elif args.new_position is not None:
        body["new_position"] = args.new_position
    else:
        body["reset"] = True
    try:
        res = client.patch_state(args.id, body)
    except ApiError as error:
        if error.status == 409:
            print("current state on server:", file=sys.stderr)
            print(json.dumps(error.body.get("state"), ensure_ascii=False, indent=2), file=sys.stderr)
        raise
    s = res["state"]
    print(f"{res['id']}: queue={s['queue']} due={s['due']} new_position={s['new_position']} "
          f"state_version {before['state_version']} → {s['state_version']}")
    return 0


# ---------------------------------------------------------------- apkg

def cmd_apkg(args) -> int:
    if not mf.DECK_ID_RE.match(args.deck):
        raise CliError("--deck must match [a-z0-9][a-z0-9_-]{0,63}")
    mapping = apkg.load_mapping(args.mapping)
    report = apkg.convert(args.file, args.deck, args.out, mapping, batch_id=args.batch_id, write=not args.preview)
    for line in apkg.preview_lines(report):
        print(line)
    if args.preview:
        print("\npreview only: nothing written")
        return 0
    # Same checks `create` runs, so a manifest that converts also validates.
    mf.prepare(report["manifest"], Path(args.out))
    print(f"\nwrote {report['path']} and {len(report['media'])} media file(s) to {Path(args.out) / 'media'}")
    print(f"next: cubercard create --manifest {report['path']} --dry-run")
    return 0


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cubercard", description="Create and maintain Cuber Card flashcards over the API.")
    p.add_argument("--version", action="version", version=f"cubercard {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("auth", help="save an API key (read hidden from the terminal, or from stdin)")
    a.add_argument("--base", help="API base URL (default https://api.card.cuberlab.tw)")

    s = sub.add_parser("status", help="account, decks and storage")
    s.add_argument("--json", action="store_true")

    d = sub.add_parser("decks", help="list / create / set decks")
    d.add_argument("action", nargs="?", choices=["list", "create", "set"], default="list")
    d.add_argument("target", nargs="?", help="create: NAME; set: deck ID")
    d.add_argument("--id", help="create: deck id slug ([a-z0-9][a-z0-9_-]{0,63})")
    d.add_argument("--name", help="set: new name")
    d.add_argument("--new-per-day", type=int)
    d.add_argument("--json", action="store_true")

    c = sub.add_parser("create", help="validate a manifest, upload media, create cards in batches of ≤ 500")
    c.add_argument("--manifest", required=True)
    c.add_argument("--media-dir", help="directory that media filenames are relative to (default: manifest's directory)")
    c.add_argument("--dry-run", action="store_true")

    k = sub.add_parser("cards", help="list / get / update / suspend / unsuspend cards")
    ks = k.add_subparsers(dest="action", required=True)
    kl = ks.add_parser("list")
    kl.add_argument("--deck")
    kl.add_argument("--external-key")
    kl.add_argument("--limit", type=int, default=100)
    kl.add_argument("--all", action="store_true", help="follow pagination")
    kl.add_argument("--json", action="store_true")
    kg = ks.add_parser("get")
    kg.add_argument("id")
    ku = ks.add_parser("update")
    ku.add_argument("id")
    ku.add_argument("--manifest-card", required=True, help="JSON file with the manifest card fields to change")
    ku.add_argument("--media-dir")
    kp = ks.add_parser("suspend")
    kp.add_argument("id")
    kp.add_argument("--reason", choices=SUSPEND_REASONS, default="agent")
    kr = ks.add_parser("unsuspend")
    kr.add_argument("id")

    sc = sub.add_parser("schedule", help="change one card's schedule (reads state_version first)")
    sc.add_argument("id")
    sc.add_argument("--due", help="ISO 8601 with time zone; review/learning cards only")
    sc.add_argument("--new-position", type=int, help="new cards only")
    sc.add_argument("--reset", action="store_true", help="forget progress, back to a new card")

    ap = sub.add_parser("apkg-to-manifest", help="convert an Anki .apkg into a manifest + media folder")
    ap.add_argument("file")
    ap.add_argument("--deck", required=True, help="target Cuber Card deck id")
    ap.add_argument("--out", required=True, help="output directory (manifest.json, media/)")
    ap.add_argument("--mapping", help="JSON field mapping per note type (see examples/mapping.json)")
    ap.add_argument("--batch-id", help="default anki:<first 16 hex of the .apkg SHA-256>")
    ap.add_argument("--preview", action="store_true", help="print the mapping preview only")
    return p


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)  # keep stdout/stderr in order when piped
    args = build_parser().parse_args(argv)
    try:
        if args.command == "auth":
            return cmd_auth(args)
        if args.command == "create":
            return cmd_create(args)
        if args.command == "apkg-to-manifest":
            return cmd_apkg(args)
        if args.command == "decks":
            if args.action == "create":
                if not args.target:
                    raise CliError("decks create NAME")
                args.name = args.name or args.target
            if args.action == "set":
                if not args.target:
                    raise CliError("decks set ID --new-per-day N")
                args.id_arg = args.target
        client = Client.from_env()
        try:
            handler = {"status": cmd_status, "decks": cmd_decks, "cards": cmd_cards, "schedule": cmd_schedule}[args.command]
            return handler(args, client)
        finally:
            client.close()
    except ApiError as error:
        print(f"error: {error.method} {error.path} → {error.describe()}", file=sys.stderr)
        return 1
    except CliError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
