import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path


# The production DLL wrapper is Windows-only.  Replace it while importing main so
# these hardware-independent tests can run on any CI worker.
fake_ibrdll = types.ModuleType("ibrdll")
fake_ibrdll.IbrDll = object
sys.modules.setdefault("ibrdll", fake_ibrdll)
main = importlib.import_module("main")


class FakeIbr:
    def __init__(self, init_rc=0):
        self.init_rc = init_rc
        self.deinit_calls = 0

    def make_fast_reader(self, _device, addresses):
        return lambda: ([0] * len(addresses), [1.0] * len(addresses))

    def pump_messages(self, **_kwargs):
        return 0

    def init_device(self, _setup_path):
        return self.init_rc

    def deinit_device(self):
        self.deinit_calls += 1
        return 0


class SensorSelectionTests(unittest.TestCase):
    def test_all_selects_sorted_sensors(self):
        self.assertEqual(main.parse_sensor_selection("all", {3, 1, 2}), [1, 2, 3])

    def test_ranges_reverse_ranges_and_duplicates(self):
        self.assertEqual(main.parse_sensor_selection("1-3, 3 5-4", set(range(1, 7))), [1, 2, 3, 5, 4])

    def test_invalid_sensor_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Invalid sensor"):
            main.parse_sensor_selection("1,7")

    def test_empty_selection_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            main.parse_sensor_selection("  ")


class FormattingTests(unittest.TestCase):
    def test_precision_tracks_sample_spread(self):
        self.assertEqual(main._format_for_delta(0.0).format(1.23456789), "1.23456789")
        self.assertEqual(main._format_for_delta(0.02).format(1.23456789), "1.235")


class MeasurementSessionTests(unittest.TestCase):
    def make_session(self, ibr=None, **overrides):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        values = {
            "ibr": ibr or FakeIbr(),
            "gauge_addresses": [1, 2],
            "gauge_descriptions": {1: "A", 2: "B"},
            "frequency_hz": 1.0,
            "duration_hours": 0.001,
            "csv_filename": str(Path(self.temp_dir.name) / "measurement.csv"),
        }
        values.update(overrides)
        return main.MeasurementSession(**values)

    def test_constructor_rejects_invalid_configuration(self):
        with self.assertRaisesRegex(ValueError, "At least one"):
            self.make_session(gauge_addresses=[])
        with self.assertRaisesRegex(ValueError, "unique"):
            self.make_session(gauge_addresses=[1, 1])
        with self.assertRaisesRegex(ValueError, "greater than zero"):
            self.make_session(frequency_hz=0)
        with self.assertRaisesRegex(ValueError, "Duration"):
            self.make_session(duration_hours=0)

    def test_stream_period_respects_pass_cap_and_device_speed(self):
        session = self.make_session(frequency_hz=0.1)
        self.addCleanup(session.finish)
        session._pass_s_est = 0.001
        self.assertEqual(session._stream_target_period(), 0.2)
        session._pass_s_est = 12.0
        self.assertEqual(session._stream_target_period(), 12.0)

    def test_init_failure_still_closes_resources(self):
        ibr = FakeIbr(init_rc=42)
        session = self.make_session(ibr=ibr)
        self.assertEqual(session.run(), 1)
        self.assertTrue(session.csv_file.closed)
        self.assertEqual(ibr.deinit_calls, 1)
        session.finish()
        self.assertEqual(ibr.deinit_calls, 1)


if __name__ == "__main__":
    unittest.main()
