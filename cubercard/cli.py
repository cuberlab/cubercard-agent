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
    table([["  " * n["depth"] + n["name"], n["id"], n["new_per_day"], n["card_count"], n["total_cards"]]
           for n in flatten(deck_tree(decks))], ["name", "deck", "new/day", "cards", "with subdecks"])
    return 0


# ---------------------------------------------------------------- decks

def deck_children(decks: list[dict]) -> dict[str | None, list[dict]]:
    """parent id → children in sort_order; a deck whose parent is not listed counts as top level."""
    ids = {d["id"] for d in decks}
    kids: dict[str | None, list[dict]] = {}
    for d in decks:
        parent = d.get("parent_id")
        kids.setdefault(parent if parent in ids else None, []).append(d)
    for siblings in kids.values():
        siblings.sort(key=lambda d: (d.get("sort_order") or 0, d["name"]))
    return kids


def deck_tree(decks: list[dict]) -> list[dict]:
    """Nested ``[{...deck, depth, total_cards, children}]``; total_cards includes every subdeck."""
    kids = deck_children(decks)

    def build(parent, depth):
        out = []
        for d in kids.get(parent, []):
            children = build(d["id"], depth + 1)
            total = (d.get("card_count") or 0) + sum(c["total_cards"] for c in children)
            out.append({**d, "depth": depth, "total_cards": total, "children": children})
        return out
    return build(None, 0)


def flatten(tree: list[dict]) -> list[dict]:
    out = []
    for node in tree:
        out.append(node)
        out += flatten(node["children"])
    return out


def deck_label(d: dict) -> str:
    return f"{d['id']} ({d['name']})"


def cmd_decks(args, client: Client) -> int:
    action = args.action or "list"
    if action == "list":
        decks = client.decks()
        if args.json:
            out(decks)
        else:
            table([[d["id"], d["name"], d.get("parent_id"), d["new_per_day"], d["card_count"]] for d in decks],
                  ["deck", "name", "parent", "new/day", "cards"])
        return 0
    if action == "tree":
        tree = deck_tree(client.decks())
        if args.json:
            out(tree)
            return 0
        rows = [["  " * n["depth"] + n["name"], n["id"], n.get("card_count"), n["total_cards"], n["new_per_day"]]
                for n in flatten(tree)]
        table(rows, ["name", "deck", "cards", "with subdecks", "new/day"])
        return 0
    if action == "create":
        name = mf.check_deck_name(args.name, "deck name")
        deck = client.create_deck(name, id=args.id, parent_id=args.parent, new_per_day=args.new_per_day)
        where = f", under {deck['parent_id']}" if deck.get("parent_id") else ""
        print(f"created deck {deck['id']} ({deck['name']}, new/day {deck['new_per_day']}{where})")
        return 0
    if action == "set":
        if args.new_per_day is None and not args.name:
            raise CliError("decks set: give --new-per-day and/or --name")
        if args.apply_to_subdecks and args.new_per_day is None:
            raise CliError("decks set: --apply-to-subdecks needs --new-per-day")
        name = mf.check_deck_name(args.name, "--name") if args.name else None
        deck = client.update_deck(args.id, name=name, new_per_day=args.new_per_day, apply_to_subdecks=args.apply_to_subdecks)
        print(f"deck {deck['id']}: name={deck['name']} new/day={deck['new_per_day']} cards={deck['card_count']}"
              + (" (new/day written to every subdeck too)" if args.apply_to_subdecks else ""))
        return 0
    if action == "rename":
        deck = client.update_deck(args.id, name=mf.check_deck_name(args.name, "deck name"))
        print(f"deck {deck['id']}: renamed to {deck['name']}")
        return 0
    if action == "move":
        parent = None if args.top_level else args.to
        if parent == args.id:
            raise CliError("decks move: a deck cannot be its own parent")
        deck = client.update_deck(args.id, parent_id=parent)
        print(f"deck {deck['id']} ({deck['name']}): now " + (f"under {deck['parent_id']}" if deck.get("parent_id") else "at the top level"))
        return 0
    if action == "delete":
        res = client.delete_deck(args.id, purge=args.purge)
        if args.json:
            out(res)
        elif args.purge:
            print(f"deck {args.id}: permanently deleted with its subdecks and cards" + _counts(res))
        else:
            print(f"deck {args.id}: moved to the trash with its subdecks and cards{_counts(res)}"
                  + (f"; restore before {res['purge_at']}" if res.get("purge_at") else "")
                  + f" with `cubercard decks restore {args.id}`")
        return 0
    if action == "restore":
        res = client.restore_deck(args.id)
        if args.json:
            out(res)
            return 0
        name = res.get("name") or (res.get("deck") or {}).get("name")
        print(f"deck {args.id}{f' ({name})' if name else ''}: restored with its subdecks and cards{_counts(res)}")
        if res.get("restored_to_root"):
            print(f"warning: its parent deck is gone, so {args.id} is now at the top level", file=sys.stderr)
        return 0
    raise CliError(f"unknown decks action: {action}")


def _counts(res: dict) -> str:
    parts = [f"{res[k]} {label}" for k, label in (("deck_count", "deck(s)"), ("card_count", "card(s)")) if res.get(k) is not None]
    return f" ({', '.join(parts)})" if parts else ""


def ensure_decks(client: Client, entries: list[dict], *, dry_run: bool = False) -> None:
    """Create the decks of decks.json that do not exist yet, top-down. Existing ones are left alone;
    a different name or parent on the server is reported, never changed."""
    server = {d["id"]: d for d in client.decks()}
    created = existing = 0
    for e in entries:
        have = server.get(e["id"])
        if have is not None:
            existing += 1
            if have.get("name") != e["name"]:
                print(f"warning: deck {e['id']} is named {have.get('name')!r} on the server, decks.json says {e['name']!r}; "
                      f"not changed", file=sys.stderr)
            if have.get("parent_id") != e["parent"]:
                print(f"warning: deck {e['id']} is under {have.get('parent_id') or 'the top level'} on the server, "
                      f"decks.json says {e['parent'] or 'the top level'}; not changed", file=sys.stderr)
            continue
        if e["parent"] and e["parent"] not in server:
            raise CliError(f"decks.json: parent {e['parent']} of {e['id']} is neither in decks.json nor on the server")
        if dry_run:
            print(f"deck {deck_label(e)}: would be created" + (f" under {e['parent']}" if e["parent"] else " at the top level"))
            server[e["id"]] = {"id": e["id"], "name": e["name"], "parent_id": e["parent"]}
            created += 1
            continue
        try:
            deck = client.create_deck(e["name"], id=e["id"], parent_id=e["parent"])
        except ApiError as error:
            if error.code == "deck_exists":
                print(f"deck {e['id']} exists but is not listed: it is probably in the trash "
                      f"(`cubercard trash list`, then `cubercard decks restore {e['id']}`)", file=sys.stderr)
            elif error.code == "deck_name_taken":
                print(f"another deck under {e['parent'] or 'the top level'} is already named {e['name']!r}; "
                      f"rename one of them, or edit decks.json", file=sys.stderr)
            raise
        server[deck["id"]] = deck
        created += 1
        print(f"deck {deck_label(e)}: created" + (f" under {e['parent']}" if e["parent"] else ""))
    print(f"decks: {created} {'to create' if dry_run else 'created'}, {existing} already exist")


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
    decks_path = mf.decks_path_for(path, args.decks)
    deck_entries = mf.load_decks(decks_path) if decks_path else []
    if decks_path:
        print(f"decks: {decks_path} ({len(deck_entries)} decks)")
    if args.dry_run:
        for i, b in enumerate(batches, 1):
            print(f"  batch {i}/{len(batches)} {b['batch_id']}: {len(b['cards'])} cards")
        _, key = resolve()
        if key:
            client = Client.from_env()
            try:
                decks = {d["id"] for d in client.decks()}
                if deck_entries:
                    ensure_decks(client, deck_entries, dry_run=True)
                planned = decks | {e["id"] for e in deck_entries}
                state = ("exists" if prepared.deck_id in decks else "created from decks.json" if prepared.deck_id in planned
                         else "NOT FOUND (create it first, or add it to decks.json)")
                print(f"deck {prepared.deck_id}: {state}")
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
        if deck_entries:
            ensure_decks(client, deck_entries)
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
        body = {k: (None if raw[k] is None and k in ("pos", "template", "lang", "direction") else card[k])
                for k in ("term", "definition", "context", "source", "pos", "template", "lang", "direction", "front", "back")
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
    if action == "delete":
        res = client.delete_card(args.id, purge=args.purge)
        if args.purge:
            print(f"{res.get('id', args.id)}: permanently deleted with its progress and history "
                  f"({len(res.get('media_released') or [])} media file(s) released)")
        else:
            until = f" before {res['purge_at']}" if res.get("purge_at") else ""
            print(f"{res.get('id', args.id)}: moved to the trash; restore{until} with `cubercard cards restore {args.id}`")
        return 0
    if action == "restore":
        try:
            res = client.restore_card(args.id)
        except ApiError as error:
            if error.status == 409 and error.code == "deck_deleted":
                deck = error.body.get("deck_id") or "?"
                print(f"the card's deck {deck} is in the trash: run `cubercard decks restore {deck}` "
                      f"(restores the whole deck with its cards)", file=sys.stderr)
                return 1
            raise
        print(f"{res.get('id', args.id)}: restored from the trash" + (f" (deck {res['deck_id']})" if res.get("deck_id") else ""))
        return 0
    if action == "move":
        res = client.move_card(args.id, args.to)
        print(f"{res.get('id', args.id)}: moved to deck {res.get('deck_id', args.to)}")
        return 0
    raise CliError(f"unknown cards action: {action}")


# ---------------------------------------------------------------- trash

def cmd_trash(args, client: Client) -> int:
    if args.action == "list":
        res = client.list_trash()
        if args.json:
            out(res)
            return 0
        items = res.get("items") or []
        print(f"trash: {len(items)} item(s), kept {res.get('retention_days', '?')} days")
        rows = []
        for i in items:
            if i.get("type") == "deck":
                what = f"{i.get('name')} ({i.get('deck_count', '?')} deck(s), {i.get('card_count', '?')} card(s))"
                where = i.get("parent_id")
            else:
                what = i.get("term") or i.get("sort_key")
                where = i.get("deck_name") or i.get("deck_id")
            rows.append([i.get("type"), i.get("id"), what, where, i.get("deleted_at"), i.get("purge_at")])
        if rows:
            table(rows, ["type", "id", "name/term", "parent/deck", "deleted_at", "purge_at"])
        return 0
    if args.action == "empty":
        res = client.empty_trash()
        if args.json:
            out(res)
            return 0
        print("trash emptied: everything in it is permanently deleted" + _counts(res))
        return 0
    raise CliError(f"unknown trash action: {args.action}")


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
    if args.deck and not mf.DECK_ID_RE.match(args.deck):
        raise CliError("--deck must match [a-z0-9][a-z0-9_-]{0,63}")
    mapping = apkg.load_mapping(args.mapping)
    report = apkg.convert(args.file, args.deck, args.out, mapping, batch_id=args.batch_id, write=not args.preview)
    for line in apkg.preview_lines(report):
        print(line)
    if args.preview:
        print("\npreview only: nothing written")
        return 0
    # Same checks `create` runs, so what converts also validates.
    for m in report["manifests"]:
        mf.prepare(m["manifest"], Path(args.out))
    if report.get("decks_path"):
        mf.load_decks(report["decks_path"])
    print()
    for m in report["manifests"]:
        print(f"wrote {m['path']} ({len(m['manifest']['cards'])} cards, deck {m['deck_id']})")
    if report.get("decks_path"):
        print(f"wrote {report['decks_path']} ({len(report['decks'])} decks; `create` makes the missing ones first)")
    print(f"wrote {len(report['media'])} media file(s) to {Path(args.out) / 'media'}")
    for m in report["manifests"]:
        print(f"next: cubercard create --manifest {m['path']} --dry-run")
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

    d = sub.add_parser("decks", help="deck tree: list / tree / create / set / rename / move / delete / restore")
    d.add_argument("--json", action="store_true")
    ds = d.add_subparsers(dest="action", metavar="ACTION")

    def deck_cmd(name, help_text, json_flag=True):
        sp = ds.add_parser(name, help=help_text)
        if json_flag:
            # SUPPRESS keeps `decks --json list` working: the subcommand does not reset the flag.
            sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        return sp

    deck_cmd("list", "all decks, flat, with parent")
    deck_cmd("tree", "decks as an indented tree with own and subtree card counts and new cards per day")
    dc = deck_cmd("create", "create a deck (names are one level: no '::'; use --parent for a subdeck)", json_flag=False)
    dc.add_argument("name")
    dc.add_argument("--id", help="deck id slug ([a-z0-9][a-z0-9_-]{0,63}); default: made by the server")
    dc.add_argument("--parent", help="parent deck id (default: top level)")
    dc.add_argument("--new-per-day", type=int)
    dt = deck_cmd("set", "change a deck's new cards per day and/or name", json_flag=False)
    dt.add_argument("id")
    dt.add_argument("--new-per-day", type=int, help="limits this deck's own cards only, not its subdecks")
    dt.add_argument("--apply-to-subdecks", action="store_true", help="also write --new-per-day to every subdeck")
    dt.add_argument("--name", help="new name")
    dr = deck_cmd("rename", "rename a deck", json_flag=False)
    dr.add_argument("id")
    dr.add_argument("name")
    dm = deck_cmd("move", "move a deck (with its subdecks) under another deck, or to the top level", json_flag=False)
    dm.add_argument("id")
    target = dm.add_mutually_exclusive_group(required=True)
    target.add_argument("--to", help="parent deck id (including a deck named root)")
    target.add_argument("--top-level", action="store_true", help="move to the top level")
    dd = deck_cmd("delete", "move a deck, its subdecks and all their cards to the trash")
    dd.add_argument("id")
    dd.add_argument("--purge", action="store_true", help="delete for good instead (cards, progress, history); cannot be undone")
    dx = deck_cmd("restore", "restore a deck from the trash with its subdecks and cards")
    dx.add_argument("id")

    c = sub.add_parser("create", help="create missing decks from decks.json, validate a manifest, upload media, "
                                      "create cards in batches of ≤ 500")
    c.add_argument("--manifest", required=True)
    c.add_argument("--media-dir", help="directory that media filenames are relative to (default: manifest's directory)")
    c.add_argument("--decks", help="decks file to create first (default: decks.json next to the manifest, if any)")
    c.add_argument("--dry-run", action="store_true")

    k = sub.add_parser("cards", help="list / get / update / suspend / unsuspend / delete / restore / move cards")
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
    kd = ks.add_parser("delete", help="move a wrong / duplicate / to-be-redone card to the trash (restorable for 7 days)")
    kd.add_argument("id")
    kd.add_argument("--purge", action="store_true", help="delete for good instead, with progress and history; cannot be undone")
    kx = ks.add_parser("restore", help="restore a card from the trash")
    kx.add_argument("id")
    km = ks.add_parser("move", help="move a card to another deck (the card id stays the same)")
    km.add_argument("id")
    km.add_argument("--to", required=True, help="target deck id")

    t = sub.add_parser("trash", help="list / empty the trash (deleted decks and cards, kept 7 days)")
    ts = t.add_subparsers(dest="action", required=True)
    tl = ts.add_parser("list", help="deleted decks (root of each deleted subtree) and single deleted cards, newest first")
    tl.add_argument("--json", action="store_true")
    te = ts.add_parser("empty", help="permanently delete everything in the trash; cannot be undone")
    te.add_argument("--json", action="store_true")

    sc = sub.add_parser("schedule", help="change one card's schedule (reads state_version first)")
    sc.add_argument("id")
    sc.add_argument("--due", help="ISO 8601 with time zone; review/learning cards only")
    sc.add_argument("--new-position", type=int, help="new cards only")
    sc.add_argument("--reset", action="store_true", help="forget progress, back to a new card")

    ap = sub.add_parser("apkg-to-manifest", help="convert an Anki .apkg into a manifest + media folder")
    ap.add_argument("file")
    ap.add_argument("--deck", help="put every card in this existing deck id; default: the Anki deck tree becomes "
                                   "decks.json and each Anki deck gets its own manifest")
    ap.add_argument("--out", required=True, help="output directory (manifest*.json, decks.json, media/)")
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
        client = Client.from_env()
        try:
            handler = {"status": cmd_status, "decks": cmd_decks, "cards": cmd_cards, "trash": cmd_trash,
                       "schedule": cmd_schedule}[args.command]
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
