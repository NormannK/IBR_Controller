import sys
import time
import csv
import os
import logging
import re
from datetime import datetime
from collections import deque
from typing import Optional

from ibrdll import IbrDll

# ------------------ Configuration ------------------

DLL_PATH = r"C:\IBR_DDK\DLL\x64\ibr_ddk.dll"
SETUP_PATH = r"C:\IMB_Test\IMB_Test.ddk"
MODULE_NUMBER = 1
OUTPUT_DIR = "Measurements"

# Maximum number of "passes" (read all selected gauges once) we'd like to fit into
# one measurement interval when running in streaming mode.
MAX_PASSES_PER_INTERVAL = 50

DEFAULT_GAUGE_DESCRIPTIONS = {
    1: "M1",
    2: "M2",
    3: "M3",
    4: "M4",
    5: "M5",
    6: "M6",
    7: "M7",
    8: "M8",
}


def _format_for_delta(delta: float) -> str:
    # Adaptive precision based on range of values.
    if delta < 1e-6:
        return "{:.8f}"
    if delta < 1e-5:
        return "{:.7f}"
    if delta < 1e-4:
        return "{:.6f}"
    if delta < 1e-3:
        return "{:.5f}"
    if delta < 1e-2:
        return "{:.4f}"
    return "{:.3f}"


class MeasurementSession:
    """
    Two acquisition modes are supported:

    1) Software streaming (default for low rates): continuously poll all selected sensors
       in evenly-spaced "passes", store values in a ring buffer, and at each output tick
       (e.g., 0.1 Hz => every 10 s) compute stats over the last interval.

       This avoids a big "burst" of 200–300 back-to-back DLL calls right at log time.

    2) Burst oversampling (kept for high output rates): run N back-to-back passes, average,
       then sleep the remainder of the interval.
    """

    def __init__(
        self,
        ibr: IbrDll,
        gauge_addresses,
        gauge_descriptions,
        frequency_hz: float,
        duration_hours: Optional[float],
        csv_filename: str,
        use_streaming: Optional[bool] = None,
    ):
        self.ibr = ibr
        self.gauge_addresses = list(gauge_addresses)
        self.gauge_descriptions = dict(gauge_descriptions)
        if not self.gauge_addresses:
            raise ValueError("At least one gauge address is required.")
        if len(set(self.gauge_addresses)) != len(self.gauge_addresses):
            raise ValueError("Gauge addresses must be unique.")

        self.frequency_hz = float(frequency_hz)
        if self.frequency_hz <= 0:
            raise ValueError("Frequency must be greater than zero.")
        self.measurement_interval = 1.0 / self.frequency_hz
        if duration_hours is not None and float(duration_hours) <= 0:
            raise ValueError("Duration must be greater than zero, or None for no time limit.")
        self.duration_seconds = None if duration_hours is None else float(duration_hours) * 3600.0
        self.csv_filename = csv_filename
        self.total_samples = 0
        self._closed = False

        # Default: streaming when interval >= 1s (typical for 0.1 Hz etc.)
        self.use_streaming = (self.measurement_interval >= 1.0) if use_streaming is None else bool(use_streaming)

        # Fast reader: pre-allocates ctypes objects and avoids per-call allocations.
        self._read_all = self.ibr.make_fast_reader(MODULE_NUMBER, self.gauge_addresses)

        # Pass-time estimator (seconds per "read all gauges once")
        self._pass_s_est: Optional[float] = None

        # Burst-only
        self.oversample_count = 1

        self.csv_file = open(csv_filename, mode="w", newline="")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(
            ["Timestamp"] + [self.gauge_descriptions.get(addr, f"Gauge {addr}") for addr in self.gauge_addresses]
        )

    def _calibrate_pass_time(self, loops: int = 10) -> float:
        """Measure how long it currently takes to read all selected gauges once."""
        # Warm up
        for _ in range(3):
            self._read_all()
            self.ibr.pump_messages()

        t0 = time.perf_counter()
        for _ in range(loops):
            self._read_all()
            self.ibr.pump_messages()
        dt = time.perf_counter() - t0
        return max(dt / float(loops), 1e-6)

    def _smooth_pass_time(self, pass_s: float) -> None:
        if self._pass_s_est is None:
            self._pass_s_est = pass_s
        else:
            self._pass_s_est = 0.85 * self._pass_s_est + 0.15 * pass_s

    # ----------- Streaming mode -----------

    def _stream_target_period(self) -> float:
        """
        Choose a target period between passes (read-all-gauges once).

        - We aim to spread passes fairly evenly through the measurement interval.
        - We also cap the number of passes per interval.
        - If the device is slow, we can't go faster than the actual pass time.
        """
        est = max(self._pass_s_est or 1e-6, 1e-6)
        budget = self.measurement_interval * 0.90  # keep slack
        desired_passes = int(budget / est)
        desired_passes = max(1, min(desired_passes, MAX_PASSES_PER_INTERVAL))
        period = self.measurement_interval / float(desired_passes)
        return max(period, est)

    def _run_streaming(self, deadline_monotonic: Optional[float]):
        buffer_seconds = max(30.0, self.measurement_interval * 3.0)
        buffers = [deque() for _ in self.gauge_addresses]  # (t_monotonic, value)

        target_period = self._stream_target_period()
        logging.info(
            f"Streaming target pass period: {target_period:.4f}s "
            f"(~{1.0/target_period:.2f} passes/s, cap {MAX_PASSES_PER_INTERVAL} passes/interval)"
        )

        next_log_t = time.monotonic() + self.measurement_interval
        last_warn_t = 0.0

        while deadline_monotonic is None or time.monotonic() <= deadline_monotonic:
            loop_t0 = time.perf_counter()

            # One "pass": read all gauges once
            rcs, vals = self._read_all()
            t_now = time.monotonic()

            # Pumping messages on the acquisition thread can reduce stalls with some DLLs/drivers.
            self.ibr.pump_messages(limit=50)

            # Store values into ring buffers
            for i, (rc, v) in enumerate(zip(rcs, vals)):
                if rc == 0:
                    buffers[i].append((t_now, v))

            # Prune old values
            cutoff_buf = t_now - buffer_seconds
            for dq in buffers:
                while dq and dq[0][0] < cutoff_buf:
                    dq.popleft()

            # Update pass-time estimate and target period
            pass_s = time.perf_counter() - loop_t0
            if pass_s > 0:
                self._smooth_pass_time(pass_s)
                target_period = self._stream_target_period()

            # Emit a row whenever we cross the next output tick
            if t_now >= next_log_t:
                timestamp = datetime.now().astimezone().isoformat()
                row = [timestamp]

                cutoff_win = t_now - self.measurement_interval

                for dq in buffers:
                    c = 0
                    s = 0.0
                    mn = float("inf")
                    mx = float("-inf")

                    # Values are ordered; scan and aggregate those inside the window.
                    for (t, v) in dq:
                        if t < cutoff_win:
                            continue
                        c += 1
                        s += v
                        if v < mn:
                            mn = v
                        if v > mx:
                            mx = v

                    if c == 0:
                        row.append("error")
                    else:
                        avg = s / float(c)
                        fmt = _format_for_delta(mx - mn)
                        row.append(fmt.format(avg))

                self.csv_writer.writerow(row)
                self.csv_file.flush()
                self.total_samples += 1
                print(" | ".join(row))

                # Schedule next tick (catch up if we're behind)
                while next_log_t <= t_now:
                    next_log_t += self.measurement_interval

                # If we are consistently behind, warn (no more than once per 5s)
                if (
                    (t_now - last_warn_t) > 5.0
                    and (self._pass_s_est is not None)
                    and (self._pass_s_est > self.measurement_interval)
                ):
                    logging.warning(
                        f"Device pass time (~{self._pass_s_est:.3f}s) exceeds measurement interval "
                        f"({self.measurement_interval:.3f}s). Output rate cannot be met."
                    )
                    last_warn_t = t_now

            # Pace passes to avoid bursty back-to-back DLL calls
            elapsed = time.perf_counter() - loop_t0
            sleep_s = target_period - elapsed
            if deadline_monotonic is not None:
                sleep_s = min(sleep_s, max(0.0, deadline_monotonic - time.monotonic()))
            if sleep_s > 0:
                time.sleep(sleep_s)

    # ----------- Burst oversampling (legacy) -----------

    def _update_oversample_count(self, pass_s: float) -> None:
        """Adapt oversample_count to fit into measurement_interval with margin."""
        self._smooth_pass_time(pass_s)

        budget = self.measurement_interval * 0.90
        max_fit = int(budget / max(self._pass_s_est or 1e-6, 1e-6))
        self.oversample_count = max(1, min(max_fit, MAX_PASSES_PER_INTERVAL))

    def _run_burst(self, deadline_monotonic: Optional[float]):
        while deadline_monotonic is None or time.monotonic() <= deadline_monotonic:
            now_local = datetime.now().astimezone().isoformat()
            row = [now_local]

            sums = [0.0] * len(self.gauge_addresses)
            counts = [0] * len(self.gauge_addresses)
            mins = [float("inf")] * len(self.gauge_addresses)
            maxs = [float("-inf")] * len(self.gauge_addresses)

            read_start = time.perf_counter()

            for _ in range(self.oversample_count):
                rcs, vals = self._read_all()
                self.ibr.pump_messages(limit=50)

                for i, (rc, v) in enumerate(zip(rcs, vals)):
                    if rc == 0:
                        counts[i] += 1
                        sums[i] += v
                        if v < mins[i]:
                            mins[i] = v
                        if v > maxs[i]:
                            maxs[i] = v

            read_duration = time.perf_counter() - read_start
            if self.oversample_count > 0:
                self._update_oversample_count(read_duration / float(self.oversample_count))

            for i in range(len(self.gauge_addresses)):
                if counts[i] == 0:
                    row.append("error")
                else:
                    avg = sums[i] / float(counts[i])
                    fmt = _format_for_delta(maxs[i] - mins[i])
                    row.append(fmt.format(avg))

            self.csv_writer.writerow(row)
            self.csv_file.flush()
            self.total_samples += 1
            print(" | ".join(row))

            remaining = self.measurement_interval - read_duration
            if deadline_monotonic is not None:
                remaining = min(remaining, max(0.0, deadline_monotonic - time.monotonic()))
            if remaining > 0:
                time.sleep(remaining)
            else:
                logging.warning(
                    f"Read loop exceeded interval ({read_duration:.3f}s > {self.measurement_interval:.3f}s). "
                    f"Oversample now {self.oversample_count}x (auto-adjusted)."
                )

    # ----------- Public run -----------

    def run(self):
        try:
            logging.info("Initializing device.")
            rc = self.ibr.init_device(SETUP_PATH)
            if rc != 0:
                logging.critical(f"Device initialization failed (rc={rc}).")
                return 1

            pass_s = self._calibrate_pass_time()
            self._smooth_pass_time(pass_s)
            logging.info(f"Initial pass time: {pass_s:.4f}s (read all gauges once)")

            deadline_monotonic = (
                None if self.duration_seconds is None else time.monotonic() + self.duration_seconds
            )

            if self.use_streaming:
                logging.info("Acquisition mode: software streaming (continuous polling + windowed averaging).")
                self._run_streaming(deadline_monotonic)
            else:
                self._update_oversample_count(pass_s)
                logging.info(f"Acquisition mode: burst oversampling ({self.oversample_count}x to start).")
                self._run_burst(deadline_monotonic)
            return 0

        except KeyboardInterrupt:
            logging.warning("Measurement manually interrupted by user.")
            print("\nMeasurement interrupted.")
            return 130
        except Exception as e:
            logging.exception(f"Unexpected error occurred: {e}")
            print(f"\nUnexpected error occurred: {e}")
            return 1
        finally:
            self.finish()

    def finish(self):
        if self._closed:
            return
        self._closed = True
        logging.info("Deinitializing device.")
        try:
            self.ibr.deinit_device()
        except Exception as e:
            logging.warning(f"Deinitialization failed: {e}")
        try:
            self.csv_file.close()
        except Exception as e:
            logging.warning(f"Failed to close CSV file: {e}")

        logging.info(f"Total samples collected: {self.total_samples}")
        print(f"Total samples: {self.total_samples}")


def parse_sensor_selection(selection: str, valid_sensors=None):
    if valid_sensors is None:
        valid_sensors = set(DEFAULT_GAUGE_DESCRIPTIONS.keys())

    s = (selection or "").strip().lower()
    if not s:
        raise ValueError("Sensor selection cannot be empty.")

    if s in {"all", "*"}:
        return sorted(valid_sensors)

    tokens = re.split(r"[,\s]+", s)
    result = []
    seen = set()

    def _add_sensor(n: int):
        if n not in valid_sensors:
            raise ValueError(f"Invalid sensor #{n}. Valid sensors: {sorted(valid_sensors)}")
        if n not in seen:
            seen.add(n)
            result.append(n)

    for tok in tokens:
        if not tok:
            continue
        if "-" in tok:
            a, b = tok.split("-", 1)
            start = int(a)
            end = int(b)
            step = 1 if start <= end else -1
            for n in range(start, end + step, step):
                _add_sensor(n)
        else:
            _add_sensor(int(tok))

    if not result:
        raise ValueError("No valid sensors selected.")
    return result


def main():
    if not os.path.isfile(DLL_PATH):
        print(f"Missing DLL file: {DLL_PATH}")
        sys.exit(1)
    if not os.path.isfile(SETUP_PATH):
        print(f"Missing setup file: {SETUP_PATH}")
        sys.exit(1)

    try:
        sensor_sel = input(
            "Enter sensor addresses to use (1–8). Examples: 4,5,6 | 1 4 8 | 1-3,8 | 'all'\n"
            "Leave blank to select by count (first N sensors): "
        ).strip()

        if sensor_sel:
            gauge_addresses = parse_sensor_selection(sensor_sel)
        else:
            num_sensors = int(input("Enter number of sensors to use (1–8): "))
            if not 1 <= num_sensors <= 8:
                raise ValueError("Sensor count must be between 1 and 8.")
            gauge_addresses = list(DEFAULT_GAUGE_DESCRIPTIONS.keys())[:num_sensors]

        freq_input = input("Enter frequency in Hz (0.001–100): ").strip()
        frequency_hz = float(freq_input)
        if not 0.001 <= frequency_hz <= 100:
            raise ValueError("Frequency must be between 0.001 and 100 Hz.")

        duration_input = input("Enter duration in hours (leave blank for no time limit): ").strip()
        duration_hours = float(duration_input) if duration_input else None

        custom_names = input("Do you want to enter custom names for the sensors? (y/n): ").strip().lower()
        if custom_names == "y":
            gauge_descriptions = {}
            for addr in gauge_addresses:
                name = input(f"Enter name for sensor address {addr}: ").strip()
                gauge_descriptions[addr] = name if name else f"Gauge {addr}"
        else:
            gauge_descriptions = {addr: DEFAULT_GAUGE_DESCRIPTIONS[addr] for addr in gauge_addresses}

    except ValueError as e:
        print(f"Invalid input: {e}")
        sys.exit(1)

    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    csv_filename = os.path.join(OUTPUT_DIR, f"measurement_{timestamp}.csv")
    log_filename = os.path.join(OUTPUT_DIR, f"measurement_{timestamp}.log")

    logging.basicConfig(
        filename=log_filename,
        filemode="a",
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))

    logging.info("----- Measurement Configuration -----")
    logging.info(f"Gauge addresses: {gauge_addresses}")
    logging.info(f"Gauge descriptions: {gauge_descriptions}")
    logging.info(f"Frequency: {frequency_hz} Hz")
    logging.info(f"Duration (hours): {'infinite' if duration_hours is None else duration_hours}")
    logging.info("-------------------------------------")

    session = MeasurementSession(
        ibr=IbrDll(DLL_PATH),
        gauge_addresses=gauge_addresses,
        gauge_descriptions=gauge_descriptions,
        frequency_hz=frequency_hz,
        duration_hours=duration_hours,
        csv_filename=csv_filename,
        use_streaming=None,  # default heuristic
    )
    return session.run()


if __name__ == "__main__":
    raise SystemExit(main())
