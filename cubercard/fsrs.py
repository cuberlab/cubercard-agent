"""Deterministic FSRS-5 scheduling, with no storage or clock dependencies.

Ported from Roa Cards; the parameters match the server's bootstrap
``scheduler`` and the shared ``tests/fixtures/fsrs_vectors.json``. Stability is
measured in days. Elapsed whole 24-hour periods select long-term versus
short-term updates; due dates preserve the review's UTC time. Daily queue
selection (local 04:00) belongs to the client, rather than this interval
calculation. ``state_version`` belongs to the server and is not incremented
per replayed event.
"""
from datetime import datetime, timedelta, timezone
import math

PARAMS = (0.40255, 1.18385, 3.173, 15.69105, 7.1949, 0.5345, 1.4604,
          0.0046, 1.54575, 0.1192, 1.01925, 1.9395, 0.11, 0.29605,
          2.2698, 0.2315, 2.9898, 0.51655, 0.6621)
DESIRED_RETENTION = 0.9
LEARNING_STEPS_MIN = (1, 10)
RELEARNING_STEPS_MIN = (10,)
MAXIMUM_INTERVAL_DAYS = 36500
DECAY = -0.5
FACTOR = 19 / 81
MIN_STABILITY = 0.01


def scheduler_config():
    return {"algorithm": "fsrs-5", "params": list(PARAMS),
            "desired_retention": DESIRED_RETENTION,
            "learning_steps_min": list(LEARNING_STEPS_MIN),
            "relearning_steps_min": list(RELEARNING_STEPS_MIN),
            "maximum_interval_days": MAXIMUM_INTERVAL_DAYS,
            "day_starts_hour": 4}


def new_state():
    return {"queue": "new", "due": None, "new_position": None,
            "stability": None, "difficulty": None, "step": 0, "reps": 0,
            "lapses": 0, "last_review": None, "state_version": 0}


def _time(value):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise ValueError("time must be finite")
        result = datetime.fromtimestamp(value, timezone.utc)
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("time must be an ISO timestamp, datetime, or epoch")
    if result.tzinfo is None:
        raise ValueError("time must include a UTC offset")
    return result.astimezone(timezone.utc).replace(microsecond=0)


def _iso(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _clamp(value, low, high):
    return min(max(value, low), high)


def _initial_difficulty(rating):
    return _clamp(PARAMS[4] - math.exp(PARAMS[5] * (rating - 1)) + 1, 1, 10)


def _difficulty(difficulty, rating):
    delta = -PARAMS[6] * (rating - 3)
    adjusted = difficulty + delta * (10 - difficulty) / 9
    return _clamp(PARAMS[7] * _initial_difficulty(4) +
                  (1 - PARAMS[7]) * adjusted, 1, 10)


def _retrievability(stability, elapsed_days):
    return (1 + FACTOR * elapsed_days / stability) ** DECAY


def retrievability(state, now):
    """Return current R, or None for a never-reviewed card."""
    if not state.get("last_review") or not state.get("stability"):
        return None
    elapsed = max(0, (_time(now) - _time(state["last_review"])).total_seconds() / 86400)
    return _retrievability(max(MIN_STABILITY, state["stability"]), elapsed)


def _stability(difficulty, stability, retrievability, rating, elapsed_days):
    w = PARAMS
    if elapsed_days == 0:
        result = stability * math.exp(w[17] * (rating - 3 + w[18]))
    elif rating == 1:
        forgotten = (w[11] * difficulty ** -w[12] *
                     ((stability + 1) ** w[13] - 1) *
                     math.exp((1 - retrievability) * w[14]))
        # The original FSRS-5 forgetting formula has no additional cap
        # derived from the short-term Good multiplier.
        result = forgotten
    else:
        hard = w[15] if rating == 2 else 1
        easy = w[16] if rating == 4 else 1
        result = stability * (1 + math.exp(w[8]) * (11 - difficulty) *
                              stability ** -w[9] *
                              (math.exp((1 - retrievability) * w[10]) - 1) * hard * easy)
    return max(MIN_STABILITY, result)


def _interval(stability):
    # At R=.9 the interval is exactly S; avoid cancellation shifting x.5
    # just below the half-day boundary before rounding.
    days = (stability if DESIRED_RETENTION == 0.9 else
            stability / FACTOR * (DESIRED_RETENTION ** (1 / DECAY) - 1))
    # Round half upward, matching Swift rounded(.toNearestOrAwayFromZero).
    return _clamp(math.floor(days + 0.5), 1, MAXIMUM_INTERVAL_DAYS)


def next_state(state, rating, now):
    """Apply one rating (1..4), returning a new CardState without mutation."""
    if type(rating) is not int or rating not in (1, 2, 3, 4):
        raise ValueError("rating must be an integer from 1 to 4")
    reviewed_at = _time(now)
    result = new_state()
    result.update(state or {})
    queue = result["queue"]
    if queue not in ("new", "learning", "review", "relearning"):
        raise ValueError("unknown queue")
    previous = _time(result["last_review"]) if result["last_review"] else None
    if previous and reviewed_at < previous:
        raise ValueError("review precedes last_review; replay events in time order")
    if previous is None or not result.get("stability") or not result.get("difficulty"):
        stability = max(MIN_STABILITY, PARAMS[rating - 1])
        difficulty = _initial_difficulty(rating)
    else:
        elapsed_days = (reviewed_at - previous).days
        old_s = max(MIN_STABILITY, float(result["stability"]))
        old_d = _clamp(float(result["difficulty"]), 1, 10)
        r = _retrievability(old_s, elapsed_days)
        stability = _stability(old_d, old_s, r, rating, elapsed_days)
        difficulty = _difficulty(old_d, rating)
    result.update(stability=stability, difficulty=difficulty,
                  reps=int(result["reps"]) + 1,
                  last_review=_iso(reviewed_at), new_position=None)
    step = max(0, int(result["step"]))
    delay = None
    if queue == "review":
        if rating == 1:
            queue, step, delay = "relearning", 0, RELEARNING_STEPS_MIN[0] * 60
            result["lapses"] = int(result["lapses"]) + 1
    else:
        if queue == "new":
            queue, step = "learning", 0
        steps = RELEARNING_STEPS_MIN if queue == "relearning" else LEARNING_STEPS_MIN
        step = min(step, len(steps) - 1)
        if rating == 1:
            step, delay = 0, steps[0] * 60
        elif rating == 2:
            delay = ((steps[0] + steps[1]) * 30 if step == 0 and len(steps) > 1
                     else steps[step] * (90 if len(steps) == 1 else 60))
        elif rating == 3 and step + 1 < len(steps):
            step += 1
            delay = steps[step] * 60
    if delay is None:
        queue, step = "review", 0
        delay = _interval(stability) * 86400
    result.update(queue=queue, step=step,
                  due=_iso(reviewed_at + timedelta(seconds=delay)))
    return result


def preview(state, now):
    """Return rating-string -> seconds until due, leaving input unchanged."""
    reviewed_at = _time(now)
    return {str(rating): int((_time(next_state(state, rating, reviewed_at)["due"]) -
                              reviewed_at).total_seconds()) for rating in (1, 2, 3, 4)}


def replay(events, initial_state=None):
    """Replay the union of events, ordered by (reviewed_at, event_id).

    Duplicate nonempty IDs count once. Conflicting duplicates are rejected;
    silently picking one would make two-device replay depend on arrival order.
    Anonymous events are supported for local calculations, without deduplication.
    """
    unique, ordered = {}, []
    for event in events:
        event_id = event.get("event_id", event.get("id"))
        at = _time(event["reviewed_at"])
        rating = event["rating"]
        if event_id:
            key = str(event_id)
            signature = (at, rating)
            if key in unique:
                if unique[key] != signature:
                    raise ValueError("conflicting duplicate event_id")
                continue
            unique[key] = signature
        else:
            key = ""
        ordered.append((at, key, rating))
    result = new_state()
    result.update(initial_state or {})
    for at, _, rating in sorted(ordered, key=lambda event: (event[0], event[1])):
        result = next_state(result, rating, at)
    return result
