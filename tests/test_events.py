from __future__ import annotations

import unittest

from services.events import evaluate_transitions


class EvaluateTransitionsTests(unittest.TestCase):
    def test_first_sample_has_no_baseline(self) -> None:
        current = {"temperature": 25.0, "humidity": 50.0, "status": "online"}
        self.assertEqual(evaluate_transitions(None, current, 30.0), [])

    def test_steady_state_produces_nothing(self) -> None:
        previous = {"temperature": 25.0, "humidity": 50.0, "status": "online"}
        current = {"temperature": 25.1, "humidity": 50.2, "status": "online"}
        self.assertEqual(evaluate_transitions(previous, current, 30.0), [])

    def test_status_transition(self) -> None:
        previous = {"temperature": 25.0, "humidity": 50.0, "status": "online"}
        current = {"temperature": None, "humidity": None, "status": "offline"}
        events = evaluate_transitions(previous, current, 30.0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "status_change")
        self.assertEqual(events[0]["old_state"], "ONLINE")
        self.assertEqual(events[0]["new_state"], "OFFLINE")

    def test_threshold_disabled_by_none(self) -> None:
        previous = {"temperature": 28.0, "humidity": 50.0, "status": "online"}
        current = {"temperature": 35.0, "humidity": 50.0, "status": "online"}
        self.assertEqual(evaluate_transitions(previous, current, None), [])

    def test_missing_temperature_skips_band_evaluation(self) -> None:
        previous = {"temperature": 31.0, "humidity": 50.0, "status": "online"}
        current = {"temperature": None, "humidity": None, "status": "offline"}
        events = evaluate_transitions(previous, current, 30.0)
        self.assertEqual(
            [e["event_type"] for e in events], ["status_change"]
        )

    def test_high_and_recovery_events(self) -> None:
        normal = {"temperature": 28.0, "humidity": 50.0, "status": "online"}
        high = {"temperature": 31.0, "humidity": 50.0, "status": "online"}

        rising = evaluate_transitions(normal, high, 30.0)
        self.assertEqual(len(rising), 1)
        self.assertEqual(rising[0]["old_state"], "NORMAL")
        self.assertEqual(rising[0]["new_state"], "TEMPERATURE_HIGH")
        self.assertEqual(rising[0]["value"], 31.0)
        self.assertIn("31.0C", rising[0]["message"])

        falling = evaluate_transitions(high, normal, 30.0)
        self.assertEqual(len(falling), 1)
        self.assertEqual(falling[0]["old_state"], "TEMPERATURE_HIGH")
        self.assertEqual(falling[0]["new_state"], "NORMAL")

    def test_boundary_is_inclusive_normal(self) -> None:
        previous = {"temperature": 29.0, "humidity": 50.0, "status": "online"}
        at_threshold = {"temperature": 30.0, "humidity": 50.0, "status": "online"}
        self.assertEqual(evaluate_transitions(previous, at_threshold, 30.0), [])


if __name__ == "__main__":
    unittest.main()
