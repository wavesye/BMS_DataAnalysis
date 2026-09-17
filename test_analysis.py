"""关键统计口径的回归验证；数据全部在临时目录内生成。"""
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from analysis_common import fit_models, paired_comparison, rate_table
from analyze_software import software_changes
from prepare_data import (ALARMS, Config, FlashDetector, aggregate_windows,
                          flash_features, prepare, scan_events, stage_csv)


def raw_frame(times, errors):
    n = len(times)
    data = pd.DataFrame({"message_time": pd.to_datetime(times, unit="s", utc=True).astype(str),
                         "vin": "VIN_TEST", "AFE_error": errors, "current_neg": 0,
                         "SOC": 50.0, "chargeAllow": 1, "Atemp_max": 30,
                         "Atemp_min": 25, "speed": 0, "software_version_gb": "3.03.06"})
    for col in ALARMS.values():
        data[col] = np.zeros(n)
    return data


class DataTests(unittest.TestCase):
    def process(self, data, chunksize):
        cfg = Config(chunksize=chunksize, window_seconds=60)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "raw.csv"
            data.to_csv(path, index=False)
            original = hashlib.sha256(path.read_bytes()).digest()
            with sqlite3.connect(str(Path(temp) / "sort.sqlite")) as db:
                quality = stage_csv(path, db, cfg)
                events, _ = scan_events(db, cfg, quality)
                windows = aggregate_windows(db, cfg, events, quality)
            self.assertEqual(original, hashlib.sha256(path.read_bytes()).digest())
        return windows, events, quality

    def test_counter_gap_reset_missing_alarm_and_chunk_independence(self):
        data = raw_frame([0, 1, 3, 4, 5, 20, 21, 22], [0, 2, 5, 0, 1, 7, 8, np.nan])
        data["sampling_alarm"] = [0, 1, 1, 0, 1, 1, 0, 255]
        windows, _, q = self.process(data, 2)
        self.assertEqual(windows.error_count.sum(), 7)
        self.assertEqual(windows.error_seconds.sum(), 5)
        self.assertEqual(windows.observed_seconds.sum(), 7)
        self.assertEqual(windows.sampling_onsets.sum(), 2)
        self.assertEqual(windows.sampling_active_seconds.sum(), 4)
        self.assertEqual(windows.sampling_seconds.sum(), 6)
        self.assertEqual(q["counter_decreases"], 1)
        self.assertEqual(q["gaps"], 1)
        shuffled = pd.concat([data.iloc[[4, 2, 0, 7, 1, 6, 3, 5]], data.iloc[[1]]], ignore_index=True)
        other, _, q2 = self.process(shuffled, 3)
        pd.testing.assert_frame_equal(windows, other)
        self.assertEqual(q2["duplicates"], 1)

    def test_flash_spans_chunks_and_has_no_future_count(self):
        data = raw_frame(range(40), np.zeros(40))
        data.loc[1:25, "current_neg"] = -700
        data.loc[1:25, "SOC"] = np.linspace(50, 70, 25)
        data.loc[26:, "SOC"] = 70
        windows, events, _ = self.process(data, 7)
        self.assertEqual(len(events), 1)
        self.assertTrue(events.iloc[0].is_flash)
        self.assertEqual(events.iloc[0].start, 1)
        self.assertEqual(events.iloc[0].end, 26)
        self.assertEqual(events.iloc[0].confirmed_at, 35)
        features = flash_features(np.array([0, 1, 25, 26, 35, 36]), events, Config(), True)
        np.testing.assert_equal(features["during_flash"], [0, 1, 1, 0, 0, 0])
        np.testing.assert_equal(features["prior_flash_count"], [0, 0, 0, 0, 0, 1])
        other, other_events, _ = self.process(data, 40)
        pd.testing.assert_frame_equal(events, other_events)
        pd.testing.assert_frame_equal(windows, other)

    def test_gap_breaks_ten_frames_and_file_start_is_censored(self):
        detector = FlashDetector(Config())
        detector.feed(0, 0, 50, 1)
        for t in range(1, 6):
            detector.feed(t, -700, 50, 1)
        for t in range(20, 36):
            detector.feed(t, -700, 70, 1)
        for t in range(36, 46):
            detector.feed(t, 0, 70, 1)
        self.assertEqual(detector.events, [])

    def test_missing_optional_fields_are_unknown(self):
        data = raw_frame(range(10), range(10))[["message_time", "vin", "AFE_error"]]
        windows, events, q = self.process(data, 3)
        self.assertEqual(windows.error_count.sum(), 9)
        self.assertEqual(windows.sampling_seconds.sum(), 0)
        self.assertTrue(windows.prior_flash_count.isna().all())
        self.assertIn("chargeAllow", q["missing_columns"])

    def test_software_transition_records_and_original_csv(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "raw"
            raw.mkdir()
            data = raw_frame(range(120), range(120))
            data.loc[60:, "software_version_gb"] = "3.03.07"
            data.to_csv(raw / "one.csv", index=False)
            prepare(raw, root / "cache", Config(chunksize=11, window_seconds=10))
            change = software_changes(root / "cache", "3.03.07").iloc[0]
            self.assertEqual(change.change_time, 60)
            self.assertEqual(change.last_old_time, 59)
            self.assertEqual(change.target_end, 119)
            pd.DataFrame({"vin": ["VIN_TEST"], "glue_time": ["1970-01-01 00:01:00"]}).to_csv(root / "glue.csv", index=False)
            for script in ("analyze_conditions.py", "analyze_software.py", "analyze_glue.py"):
                output = root / script.removesuffix(".py")
                command = [sys.executable, str(Path(__file__).parent / script), str(root / "cache"), str(output)]
                if script != "analyze_conditions.py":
                    command += ["--days", "1", "--exclude-days", "0", "--min-hours", "0.001"]
                if script == "analyze_glue.py":
                    command += ["--interventions", str(root / "glue.csv")]
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((output / "model_diagnostics.json").exists())
                self.assertEqual(len(pd.read_csv(output / "rates_by_vin.csv")), 8 if script == "analyze_conditions.py" else 16)


class ModelTests(unittest.TestCase):
    def test_poisson_offset_and_vin_covariance(self):
        rng = np.random.default_rng(7)
        n = 8000
        vin = np.repeat([f"V{i:03d}" for i in range(40)], n // 40)
        exposure = rng.uniform(60, 600, n)
        x = rng.normal(size=n)
        data = pd.DataFrame({"vin": vin, "window": np.arange(n), "window_seconds": 60,
                             "error_seconds": exposure, "software": "3.03.06",
                             "error_count": rng.poisson(exposure / 3600 * np.exp(2 + .5 * x))})
        from analysis_common import SCALES
        for feature in SCALES:
            data[feature + "_within"] = x if feature == "soc" else 0
            data[feature + "_between"] = 0
        for alarm in ALARMS:
            data[alarm + "_onsets"] = 0
            data[alarm + "_seconds"] = exposure
        with tempfile.TemporaryDirectory() as temp:
            fit_models(data, Path(temp), min_vins=20)
            result = pd.read_csv(Path(temp) / "model_coefficients.csv")
            beta = result.loc[result.term.eq("soc_within"), "coefficient"].iloc[0]
            self.assertAlmostEqual(beta, .5, delta=.06)
            diagnostics = json.loads((Path(temp) / "model_diagnostics.json").read_text())
            self.assertEqual(diagnostics[0]["status"], "ok")
            self.assertEqual(diagnostics[0]["vins"], 40)

    def test_paired_zero_baseline_does_not_invent_ratio(self):
        data = pd.DataFrame({"vin": ["A", "A", "B", "B"],
                             "period": ["before", "after", "before", "after"],
                             "error_count": [0, 2, 4, 2], "error_seconds": [3600] * 4})
        for alarm in ALARMS:
            data[alarm + "_onsets"] = 0
            data[alarm + "_seconds"] = 3600
            data[alarm + "_active_seconds"] = 0
        with tempfile.TemporaryDirectory() as temp:
            paired_comparison(rate_table(data, ["vin", "period"]), Path(temp))
            result = pd.read_csv(Path(temp) / "paired_by_vin.csv")
            row = result.loc[result.vin.eq("A") & result.metric.eq("afe")].iloc[0]
            self.assertTrue(pd.isna(row.rate_ratio))
            self.assertEqual(row.rate_difference, 2)


if __name__ == "__main__":
    unittest.main()
