#!/usr/bin/env bash
# End-to-end: start the Cuber Card server with `wrangler dev --local`, log in with a test-mode ID token,
# create an API key, then drive the real `cubercard` CLI against it. Stops wrangler on exit.
#
# Usage:
#   CUBERCARD_SERVER_DIR=/path/to/cuber-card/server scripts/e2e.sh
# Optional: E2E_PORT (default 8790), E2E_PERSIST_DIR (default: fresh temp dir; reused dirs keep old data).
# Needs: node/npx (server deps installed with `npm install`), uv, ffmpeg, curl, jq.
# Never point this at production: production rejects test-mode tokens.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SERVER="${CUBERCARD_SERVER_DIR:?set CUBERCARD_SERVER_DIR to the cuber-card server directory}"
PORT="${E2E_PORT:-8790}"
BASE="http://127.0.0.1:$PORT"
WORK="$(mktemp -d)"
PERSIST="${E2E_PERSIST_DIR:-$WORK/state}"
FFMPEG="${FFMPEG:-ffmpeg}"
WRANGLER_PID=""

cleanup() {
  if [[ -n "$WRANGLER_PID" ]] && kill -0 "$WRANGLER_PID" 2>/dev/null; then
    kill "$WRANGLER_PID" 2>/dev/null || true
    wait "$WRANGLER_PID" 2>/dev/null || true
  fi
  # wrangler dev spawns workerd; make sure nothing keeps listening on our port.
  local left
  left="$(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$left" ]]; then kill $left 2>/dev/null || true; fi
  rm -rf -- "${WORK:?}"
}
trap cleanup EXIT

step() { printf '\n== %s\n' "$*"; }
fail() { printf 'E2E FAIL: %s\n' "$*" >&2; exit 1; }
cc() { (cd "$REPO" && uv run --quiet cubercard "$@"); }
# run CMD... → output in $WORK/out (stdout+stderr), echoed; nonzero exit fails unless EXPECT_FAIL=1
run() {
  set +e
  cc "$@" >"$WORK/out" 2>&1
  local status=$?
  set -e
  local lines
  lines="$(wc -l <"$WORK/out" | tr -d ' ')"
  head -n 15 "$WORK/out" | sed 's/^/  | /'
  if (( lines > 15 )); then echo "  | … $((lines - 15)) more lines"; fi
  if [[ "${EXPECT_FAIL:-0}" == 1 ]]; then
    [[ $status -ne 0 ]] || fail "expected failure: cubercard $*"
  else
    [[ $status -eq 0 ]] || fail "cubercard $* exited $status"
  fi
}
has() { grep -qE -- "$1" "$WORK/out" || fail "output lacks /$1/"; }
count() { grep -cE -- "$1" "$WORK/out" || true; }

export XDG_CONFIG_HOME="$WORK/config"
unset CUBERCARD_API_KEY CUBERCARD_API_BASE

step "migrate + start wrangler dev on :$PORT (persist $PERSIST)"
if lsof -ti "tcp:$PORT" -sTCP:LISTEN >/dev/null 2>&1; then fail "port $PORT is already in use"; fi
mkdir -p "$PERSIST"
(cd "$SERVER" && CI=1 npx wrangler d1 migrations apply cuber-card --local --persist-to "$PERSIST" >"$WORK/migrate.log" 2>&1) \
  || { cat "$WORK/migrate.log" >&2; fail "migrations failed"; }
(cd "$SERVER" && exec npx wrangler dev --local --port "$PORT" --ip 127.0.0.1 --persist-to "$PERSIST" >"$WORK/wrangler.log" 2>&1) &
WRANGLER_PID=$!
for _ in $(seq 1 60); do
  curl -fsS "$BASE/v1/health" >/dev/null 2>&1 && break
  kill -0 "$WRANGLER_PID" 2>/dev/null || { cat "$WORK/wrangler.log" >&2; fail "wrangler exited"; }
  sleep 1
done
curl -fsS "$BASE/v1/health" | jq -c . || { tail -40 "$WORK/wrangler.log" >&2; fail "server not healthy"; }

step "test-mode login → API key"
SESSION_SECRET="${SESSION_SECRET:-$(grep -E '^SESSION_SECRET=' "$SERVER/.dev.vars" | head -1 | cut -d= -f2-)}"
[[ -n "$SESSION_SECRET" ]] || fail "no SESSION_SECRET (server .dev.vars)"
SUB="e2e-$(date +%s)-$RANDOM"
ID_TOKEN="$(cd "$SERVER" && SESSION_SECRET="$SESSION_SECRET" node scripts/test-id-token.mjs google "$SUB" "$SUB@example.com")"
ACCESS="$(curl -fsS -X POST "$BASE/v1/auth/login" -H 'content-type: application/json' \
  -d "$(jq -nc --arg t "$ID_TOKEN" '{provider:"google", id_token:$t, device_id:"e2e"}')" | jq -r .access_token)"
KEY="$(curl -fsS -X POST "$BASE/v1/api-keys" -H "authorization: Bearer $ACCESS" -H 'content-type: application/json' \
  -d '{"name":"e2e agent"}' | jq -r .key)"
[[ "$KEY" == ck_live_* ]] || fail "no API key"
echo "  key ${KEY:0:16}… (test account $SUB@example.com)"

step "cubercard auth (key from stdin)"
printf '%s\n' "$KEY" | cc auth --base "$BASE" >"$WORK/out" 2>&1 || { cat "$WORK/out"; fail "auth failed"; }
sed 's/^/  | /' "$WORK/out"; has "authenticated as $SUB@example.com"
CRED="$XDG_CONFIG_HOME/cubercard/credentials"
[[ "$(ls -l "$CRED" | cut -c1-10)" == -rw------- ]] || fail "credentials not mode 600"
jq -e --arg b "$BASE" '.base == $b and (.key | startswith("ck_live_"))' "$CRED" >/dev/null || fail "bad credentials file"
echo "  credentials mode 600 ok"
printf 'ck_live_wrong\n' | (cd "$REPO" && XDG_CONFIG_HOME="$WORK/badcfg" uv run --quiet cubercard auth --base "$BASE") >"$WORK/out" 2>&1 \
  && fail "wrong key accepted"
sed 's/^/  | /' "$WORK/out"; has "HTTP 401"

step "decks create / set / list"
run decks create Geography --id geography
run decks create Reading --id reading --new-per-day 15
run decks create French --id french
run decks set reading --new-per-day 12
has "new/day=12"
EXPECT_FAIL=1 run decks create Geography --id geography
has "409 deck_exists"
run decks list
has "^geography +Geography +20 +0"

step "make a test clip and normalize it (-16 LUFS ±1, true peak ≤ -1.5 dBTP)"
mkdir -p "$WORK/media"
measure() { "$FFMPEG" -nostats -hide_banner -i "$1" -af ebur128=peak=true -f null - 2>&1 \
  | awk '/Summary/{s=1} s&&/ I:/{i=$2} s&&/Peak:/{p=$2} END{print i, p}'; }
"$FFMPEG" -y -hide_banner -loglevel error -f lavfi -i "sine=frequency=440:duration=1.5" -af "volume=-20dB" -ac 1 -ar 44100 "$WORK/raw.wav"
read -r I0 _ <<<"$(measure "$WORK/raw.wav")"
GAIN="$(awk -v i="$I0" 'BEGIN{printf "%.2f", -16 - i}')"
"$FFMPEG" -y -hide_banner -loglevel error -i "$WORK/raw.wav" \
  -af "volume=${GAIN}dB,alimiter=limit=0.8414:attack=5:release=50:level=false" \
  -ar 44100 -ac 1 -c:a libmp3lame -q:a 4 "$WORK/media/geography_paris_berlin.mp3"
read -r I1 TP1 <<<"$(measure "$WORK/media/geography_paris_berlin.mp3")"
echo "  before ${I0} LUFS, gain ${GAIN} dB → ${I1} LUFS, true peak ${TP1} dBTP"
awk -v i="$I1" -v p="$TP1" 'BEGIN{exit !((i >= -17 && i <= -15) && p <= -1.5)}' || fail "clip off target"

step "create cloze example: dry run, real run, rerun (idempotent)"
run create --manifest "$REPO/examples/cloze-card.json" --media-dir "$WORK/media" --dry-run
has "manifest ok: 3 cards, deck geography, 1 batch"; has "media: 0 already uploaded, 1 to upload"; has "dry run"
run create --manifest "$REPO/examples/cloze-card.json" --media-dir "$WORK/media"
has "^uploaded geography_paris_berlin.mp3"
[[ "$(count '^created ')" == 3 ]] || fail "expected 3 created"
has "geography:capitals:paris-berlin:c1"; has "geography:capitals:paris-berlin:c2"; has "geography:capitals:tirana:c1"
run create --manifest "$REPO/examples/cloze-card.json" --media-dir "$WORK/media"
has "media: 0 uploaded, 1 already present"; has "replay"
[[ "$(count '^existing ')" == 3 && "$(count '^created ')" == 0 ]] || fail "rerun should be all existing"

step "create text example (no media)"
run create --manifest "$REPO/examples/text-card.json"
[[ "$(count '^created ')" == 2 ]] || fail "expected 2 created"

step "content conflict is rejected as a whole batch"
jq '.batch_id = "reading:conflict-test" | .cards[0].definition = "changed"' "$REPO/examples/text-card.json" >"$WORK/conflict.json"
EXPECT_FAIL=1 run create --manifest "$WORK/conflict.json"
has "409 content_conflict"; has "reading:designing-games:ephemeral"

step "1,001 cards → 3 batches of ≤ 500; rerun replays every part"
jq -n '{batch_id: "reading:bulk:v1", deck_id: "reading", cards: [range(1001) | tostring as $i |
  {external_key: ("bulk:" + $i), term: ("word" + $i), definition: ("定義" + $i), context: ("context " + $i), source: "e2e bulk",
   front: [{type: "title", text: ("word" + $i)}], back: [{type: "translation", text: ("定義" + $i)}]}]}' >"$WORK/bulk.json"
cc create --manifest "$WORK/bulk.json" >"$WORK/out" 2>&1 || { tail -20 "$WORK/out"; fail "bulk create failed"; }
grep -E '^(manifest|batch|done)' "$WORK/out" | sed 's/^/  | /'
has "batch 1/3 reading:bulk:v1:part001: created 500"; has "batch 3/3 reading:bulk:v1:part003: created 1"; has "done: created 1001"
cc create --manifest "$WORK/bulk.json" >"$WORK/out" 2>&1 || fail "bulk rerun failed"
grep -E '^(batch|done)' "$WORK/out" | sed 's/^/  | /'
has "done: created 0, existing 1001"; [[ "$(count 'replay')" == 3 ]] || fail "each part should replay"

step "apkg-to-manifest → create (history + FSRS state)"
(cd "$REPO" && uv run --quiet python -c "import sys; from pathlib import Path; from tests.apkg_fixture import build; print(build(Path(sys.argv[1])))" "$WORK") >"$WORK/apkg-path"
run apkg-to-manifest "$(cat "$WORK/apkg-path")" --deck french --out "$WORK/anki"
has "note type: Basic  kind=basic  notes=1  cards=1"; has "note type: Cloze  kind=cloze  notes=1  cards=2"
has "total cards: 3 \(with state: 2, new: 1, history rows: 5, media files: 2\)"
run create --manifest "$WORK/anki/manifest.json"
[[ "$(count '^created ')" == 3 ]] || fail "expected 3 anki cards"
has "history rows 5"

step "cards list / get"
run cards list --deck geography
[[ "$(count '^c_')" == 3 ]] || fail "expected 3 geography cards"
run cards list --deck french --json
REVIEW_ID="$(jq -r '.[] | select(.external_key == "anki:2001:0") | .id' "$WORK/out")"
NEW_ID="$(jq -r '.[] | select(.external_key == "anki:2002:c2") | .id' "$WORK/out")"
jq -e '.[] | select(.external_key == "anki:2001:0") | .state.queue == "review" and .state.state_version == 1' "$WORK/out" >/dev/null \
  || fail "imported review state missing"
run cards get "$REVIEW_ID"
jq -e '(.history | length) == 3 and all(.history[]; .source == "import")' "$WORK/out" >/dev/null || fail "history not imported"
echo "  $REVIEW_ID: review state + 3 import history rows"
run cards list --deck reading --limit 400 --all
[[ "$(count '^c_')" == 1003 ]] || fail "expected 1003 reading cards over pages"

step "schedule (reads state_version, then writes)"
run schedule "$REVIEW_ID" --due 2026-12-01T09:00:00+08:00
has "queue=review due=2026-12-01T01:00:00Z"; has "state_version 1 → 2"
run schedule "$NEW_ID" --new-position 0
has "new_position=0"
EXPECT_FAIL=1 run schedule "$NEW_ID" --due 2026-12-01T00:00:00Z
has "400 invalid_state"
run schedule "$REVIEW_ID" --reset
has "queue=new"; has "state_version 2 → 3"
# Stale based_on_version (what a concurrent phone sync causes) → 409 with the current state.
code="$(curl -sS -o "$WORK/body" -w '%{http_code}' -X PATCH "$BASE/v1/cards/$REVIEW_ID/state" -H "authorization: Bearer $KEY" \
  -H 'content-type: application/json' -d '{"based_on_version": 1, "new_position": 5}')"
[[ "$code" == 409 ]] && jq -e '.error == "version_conflict" and .state.state_version == 3' "$WORK/body" >/dev/null || fail "expected 409"
echo "  stale based_on_version → 409 version_conflict with state_version 3"

step "cards suspend / unsuspend / update"
run cards suspend "$NEW_ID" --reason too_easy
has "suspended=True \(too_easy\)"
run cards unsuspend "$NEW_ID"
has "suspended=False"
TEXT_ID="$(cc cards list --deck reading --external-key reading:designing-games:ephemeral --json | jq -r '.[0].id')"
cat >"$WORK/update.json" <<'JSON'
{"definition": "短暫的", "back": [{"type": "translation", "text": "短暫的"}, {"type": "audio", "filename": "geography_paris_berlin.mp3"}]}
JSON
run cards update "$TEXT_ID" --manifest-card "$WORK/update.json" --media-dir "$WORK/media"
has "content_version 1 → 2"

step "status"
run status
has "account: $SUB@example.com"; has "storage: "; has "^reading +Reading +12 +1003"

printf '\nE2E OK\n'
