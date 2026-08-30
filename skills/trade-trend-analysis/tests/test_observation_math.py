"""Focused deterministic math tests for source-level observations."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_observations import _breadth, _daily_returns, _dominance, _return  # noqa: E402


class ObservationMathTests(unittest.TestCase):
    def test_flat_members_are_not_relabelled_as_down(self):
        day = "2026-08-21"
        breadth = _breadth([{
            "date": day, "up_count": 5, "down_count": 0, "flat_count": 5,
            "non_up_count": 5, "breadth_denominator": 10,
            "breadth_coverage": "FULL",
        }], [day])
        self.assertEqual(breadth["up_ratio_today"], 0.5)
        self.assertEqual(breadth["down_ratio_today"], 0.0)
        self.assertEqual(breadth["balance_path_5d"], [0.5])

    def test_up_vs_non_up_only_does_not_invent_down_or_balance(self):
        day = "2026-08-21"
        breadth = _breadth([{
            "date": day, "up_count": 9, "down_count": None, "flat_count": None,
            "non_up_count": 1, "breadth_denominator": 10,
            "breadth_coverage": "UP_VS_NON_UP_ONLY",
        }], [day])
        self.assertEqual(breadth["up_ratio_today"], 0.9)
        self.assertIsNone(breadth["down_ratio_today"])
        self.assertEqual(breadth["balance_path_5d"], [None])

    def test_incomplete_full_or_up_down_accounting_never_yields_balance(self):
        day = "2026-08-21"
        for row in ({
                "date": day, "up_count": 5, "down_count": 3, "flat_count": None,
                "non_up_count": None, "breadth_denominator": 100,
                "breadth_coverage": "FULL",
        }, {
                "date": day, "up_count": 5, "down_count": 3, "flat_count": None,
                "non_up_count": 3, "breadth_denominator": 100,
                "breadth_coverage": "UP_DOWN_ONLY",
        }):
            with self.subTest(coverage=row["breadth_coverage"]):
                breadth = _breadth([row], [day])
                self.assertIsNone(breadth["down_ratio_today"])
                self.assertEqual(breadth["balance_path_5d"], [None])

    def test_return_windows_and_dominance_do_not_cross_missing_dates(self):
        closes = [110.0, 100.0, 90.0, None, 80.0, 70.0]
        self.assertAlmostEqual(_return(closes, 1), 0.1)
        self.assertIsNone(_return(closes, 3))
        self.assertIsNone(_daily_returns(closes, 5))
        self.assertAlmostEqual(_dominance([0.01, -0.02, 0.03]), 0.5)


if __name__ == "__main__":
    unittest.main()
