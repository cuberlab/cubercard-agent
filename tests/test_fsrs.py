import copy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import unittest

from cubercard.fsrs import (PARAMS, new_state, next_state, preview, replay,
                           retrievability, scheduler_config)


class FSRSTests(unittest.TestCase):
    def test_shared_vectors(self):
        payload = json.loads((Path(__file__).parent / 'fixtures/fsrs_vectors.json').read_text())
        self.assertEqual(payload['params'], list(PARAMS))
        self.assertGreaterEqual(len(payload['vectors']), 40)
        for vector in payload['vectors']:
            with self.subTest(name=vector['name']):
                result = replay(vector['events'], vector['initial_state'])
                self.assertEqual(set(result), set(vector['expected']))
                for field, expected in vector['expected'].items():
                    if isinstance(expected, float):
                        self.assertAlmostEqual(result[field], expected, places=6, msg=field)
                    else:
                        self.assertEqual(result[field], expected, field)

    def test_initial_parameters_and_learning_schedule(self):
        for rating, stability, delay, queue, step in [
                (1, 0.40255, 60, 'learning', 0),
                (2, 1.18385, 330, 'learning', 0),
                (3, 3.173, 600, 'learning', 1),
                (4, 15.69105, 16 * 86400, 'review', 0)]:
            result = next_state(None, rating, '2026-01-01T00:00:00Z')
            self.assertEqual(result['stability'], stability)
            self.assertEqual(result['queue'], queue)
            self.assertEqual(result['step'], step)
            self.assertEqual(preview(None, '2026-01-01T00:00:00Z')[str(rating)], delay)
        self.assertEqual(next_state(None, 1, 1767225600)['difficulty'], 7.1949)

    def test_forgetting_curve_at_stability_is_retention(self):
        state = dict(stability=10, last_review='2026-01-01T00:00:00Z')
        self.assertAlmostEqual(retrievability(state, '2026-01-11T00:00:00Z'), 0.9, places=14)
        self.assertEqual(retrievability(state, '2026-01-01T00:00:00Z'), 1)
        self.assertIsNone(retrievability(new_state(), '2026-01-01T00:00:00Z'))

    def test_short_term_formula_known_result(self):
        # FSRS-5 Eq. S'=S*exp(w17*(G-3+w18)); Good at S=10.
        state = dict(queue='review', stability=10, difficulty=5,
                     last_review='2026-01-01T00:00:00Z')
        result = next_state(state, 3, '2026-01-01T12:00:00Z')
        self.assertAlmostEqual(result['stability'], 14.077712147375411, places=12)
        self.assertEqual(result['due'], '2026-01-15T12:00:00Z')
        # Initial Easy difficulty is the mean-reversion target, not Good.
        initial_easy_d = 7.1949 - math.exp(0.5345 * 3) + 1
        self.assertAlmostEqual(result['difficulty'], 0.0046 * initial_easy_d + 0.9954 * 5)

    def test_long_term_recall_formula(self):
        state = dict(queue='review', stability=10, difficulty=5,
                     last_review='2026-01-01T00:00:00Z')
        # At t=S, R=.9, so this value has no rounded elapsed-time ambiguity.
        expected = 10 * (1 + math.exp(1.54575) * 6 * 10 ** -0.1192 *
                         (math.exp(0.1 * 1.01925) - 1))
        self.assertAlmostEqual(next_state(state, 3, '2026-01-11T00:00:00Z')['stability'], expected)

    def test_fsrs5_difficulty_damping_then_mean_reversion(self):
        state = dict(queue='review', stability=10, difficulty=5,
                     last_review='2026-01-01T00:00:00Z')
        initial_easy_d = 7.1949 - math.exp(0.5345 * 3) + 1
        for difficulty in (1, 5, 9, 10):
            for rating in (1, 2, 3, 4):
                delta = -1.4604 * (rating - 3)
                adjusted = difficulty + delta * (10 - difficulty) / 9
                expected = min(10, max(1, 0.0046 * initial_easy_d + 0.9954 * adjusted))
                result = next_state(dict(state, difficulty=difficulty), rating, '2026-01-11T00:00:00Z')
                self.assertAlmostEqual(result['difficulty'], expected, places=12)

    def test_astra_hand_calculated_lapse_then_good(self):
        state = dict(queue='review', stability=10, difficulty=5,
                     last_review='2026-01-01T00:00:00Z')
        again = next_state(state, 1, '2026-01-11T00:00:00Z')
        self.assertAlmostEqual(again['difficulty'], 6.607035, places=6)
        good = next_state(again, 3, '2026-02-11T00:00:00Z')
        self.assertEqual(good['due'], '2026-03-13T00:00:00Z')

    def test_hard_penalty_easy_bonus_and_interval_bounds(self):
        state = dict(queue='review', stability=10, difficulty=5,
                     last_review='2026-01-01T00:00:00Z')
        growth = 10 * math.exp(1.54575) * 6 * 10 ** -0.1192 * (math.exp(.1 * 1.01925) - 1)
        for rating, multiplier in ((2, .2315), (3, 1), (4, 2.9898)):
            result = next_state(state, rating, '2026-01-11T00:00:00Z')
            self.assertAlmostEqual(result['stability'], 10 + growth * multiplier, places=12)
        from cubercard.fsrs import _interval
        for stability, days in ((.01, 1), (10, 10), (10.5, 11), (36501, 36500)):
            self.assertEqual(_interval(stability), days)

    def test_fsrs5_lapse_uses_original_forgetting_formula(self):
        state = dict(queue='review', stability=0.1, difficulty=5,
                     last_review='2025-01-01T00:00:00Z')
        r = (1 + (19 / 81) * 365 / 0.1) ** -0.5
        expected = 1.9395 * 5 ** -0.11 * ((1.1 ** 0.29605) - 1) * math.exp(2.2698 * (1 - r))
        result = next_state(state, 1, '2026-01-01T00:00:00Z')
        self.assertAlmostEqual(result['stability'], expected, places=12)
        self.assertGreater(result['stability'], 0.1 / math.exp(0.51655 * 0.6621))

    def test_relearning_and_lapses(self):
        state = dict(queue='review', stability=10, difficulty=5, reps=5,
                     last_review='2026-01-01T00:00:00Z')
        lapsed = next_state(state, 1, '2026-01-11T00:00:00Z')
        self.assertEqual((lapsed['queue'], lapsed['lapses']), ('relearning', 1))
        self.assertEqual(lapsed['due'], '2026-01-11T00:10:00Z')
        hard = next_state(lapsed, 2, '2026-01-11T00:10:00Z')
        self.assertEqual(hard['due'], '2026-01-11T00:25:00Z')
        self.assertEqual(hard['lapses'], 1)
        graduated = next_state(hard, 3, '2026-01-11T00:25:00Z')
        self.assertEqual(graduated['queue'], 'review')

    def test_replay_order_and_duplicates(self):
        events = [{'event_id': 'c', 'rating': 1, 'reviewed_at': '2026-01-02T00:00:00Z'},
                  {'event_id': 'b', 'rating': 3, 'reviewed_at': '2026-01-01T00:00:00Z'},
                  {'event_id': 'a', 'rating': 4, 'reviewed_at': '2026-01-01T00:00:00Z'}]
        expected = replay([events[2], events[1], events[0]])
        self.assertEqual(replay(events + events[::-1]), expected)
        self.assertEqual(expected['reps'], 3)
        duplicate = dict(events[0], rating=4)
        with self.assertRaisesRegex(ValueError, 'conflicting duplicate'):
            replay(events + [duplicate])

    def test_replay_with_import_baseline_and_versions(self):
        state = next_state(None, 4, '2026-01-01T00:00:00Z')
        state['state_version'] = 42
        baseline = copy.deepcopy(state)
        events = [dict(id='device-b', rating=3, reviewed_at='2026-02-01T00:00:00Z')]
        result = replay(events, state)
        self.assertEqual(result['reps'], 2)
        self.assertEqual(result['state_version'], 42)
        self.assertEqual(state, baseline)

    def test_pure_preview_and_time_normalization(self):
        state = new_state()
        original = copy.deepcopy(state)
        result = preview(state, '2026-01-01T08:00:00+08:00')
        self.assertEqual(result, {'1': 60, '2': 330, '3': 600, '4': 1382400})
        self.assertEqual(state, original)
        self.assertEqual(next_state(state, 3, datetime(2026, 1, 1, tzinfo=timezone.utc))['due'],
                         '2026-01-01T00:10:00Z')

    def test_input_validation(self):
        for rating in [0, 5, True, 3.0, '3', None]:
            with self.subTest(rating=rating), self.assertRaises(ValueError):
                next_state(None, rating, '2026-01-01T00:00:00Z')
        with self.assertRaises(ValueError):
            next_state(None, 3, '2026-01-01T00:00:00')
        with self.assertRaises(ValueError):
            next_state(dict(queue='bogus'), 3, '2026-01-01T00:00:00Z')
        with self.assertRaisesRegex(ValueError, 'precedes'):
            next_state(dict(last_review='2026-01-02T00:00:00Z'), 3, '2026-01-01T00:00:00Z')

    def test_configuration_and_maximum_interval(self):
        self.assertEqual(len(scheduler_config()['params']), 19)
        state = dict(queue='review', stability=36500, difficulty=1,
                     last_review='1926-01-01T00:00:00Z')
        self.assertEqual(preview(state, '2026-01-01T00:00:00Z')['4'], 36500 * 86400)


if __name__ == '__main__':
    unittest.main()
