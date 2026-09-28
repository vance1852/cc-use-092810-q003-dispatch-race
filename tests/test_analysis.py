from __future__ import annotations

import unittest
from pathlib import Path

from battery_assurance.analysis import analyze, bootstrap_mean_interval
from battery_assurance.jsonio import load_observations, load_protocol


ROOT = Path(__file__).resolve().parents[1]


class AnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.protocol = load_protocol(ROOT / "fixtures" / "demo_protocol.json")
        self.rows = load_observations(ROOT / "fixtures" / "demo_observations.jsonl", self.protocol)

    def test_same_snapshot_is_deterministic(self) -> None:
        first = analyze(self.protocol, self.rows)
        second = analyze(self.protocol, self.rows)
        self.assertEqual(first, second)
        self.assertEqual(first["conclusion"], "pass")
        self.assertEqual(first["included_count"], 6)

    def test_missing_stratum_is_insufficient(self) -> None:
        rows = tuple(row for row in self.rows if row.stratum_key == "clear-aisle")
        result = analyze(self.protocol, rows)
        self.assertEqual(result["conclusion"], "insufficient")
        self.assertEqual(result["insufficient"][0]["stratum"], "cross-traffic")

    def test_bootstrap_seed_controls_result(self) -> None:
        values = [row.metrics["completion_seconds"] for row in self.rows]
        self.assertEqual(
            bootstrap_mean_interval(values, seed=42, samples=200),
            bootstrap_mean_interval(values, seed=42, samples=200),
        )


if __name__ == "__main__":
    unittest.main()
