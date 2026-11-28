"""Frame window labels, not line numbers, determine comparable intervals."""

import tempfile
import unittest
from pathlib import Path

from research.compare_native_frames import (
    QUEUE_MARKER,
    compare_workload,
    frame_report,
    parse_line,
    parse_queue_sample,
    read_run,
)


def line(first, p50=15000):
    return (
        f"VELKIN_FRAME_TIMINGS frames={first}..{first + 30} samples=30 "
        f"cpu_present_p50_us={p50} cpu_present_p95_us=28000 cpu_present_p99_us=41000\n"
    )


class NativeFrameTests(unittest.TestCase):
    def test_workload_uses_its_own_intervals(self):
        entries = [
            f"INFO gpu: {QUEUE_MARKER} frame={f} draws={draws} drawn={draws} dispatches=0 dispatched=0\n"
            for f, draws in ((16, 24), (32, 48), (64, 96), (128, 565))
        ]
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "err.log").write_text(
                "".join(entries) + line(31), encoding="utf8"
            )
            control = {}
            read_run(folder, queue_samples=control)
        candidate = dict(
            parse_queue_sample(v)
            for v in (entries[0], entries[1].replace("=48", "=49"))
        )
        rows = compare_workload({"control": control, "candidate": candidate}, 8, 91)
        self.assertEqual(
            [(r["after_frame"], r["through_frame"]) for r in rows],
            [(8, 16), (16, 32), (32, 64)],
        )
        self.assertEqual([r["matching_counts"] for r in rows], [True, False, None])
        with self.assertRaisesRegex(ValueError, "power-of-two"):
            parse_queue_sample(entries[0].replace("frame=16", "frame=17"))

    def test_stdout_stderr_and_utf16_mirror(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "stdout.log").write_text("boot\n" + line(1), encoding="utf-16")
            (root / "err.log").write_text(line(1) + line(31), encoding="utf8")
            control = read_run(root)
            self.assertEqual(sorted(control), [(1, 31), (31, 61)])
            (root / "stdout.log").write_text(line(1, 14999), encoding="utf8")
            with self.assertRaisesRegex(ValueError, "conflicting"):
                read_run(root)
            (root / "stdout.log").write_text(line(1) + line(1), encoding="utf8")
            with self.assertRaisesRegex(ValueError, "repeated"):
                read_run(root)

    def test_same_frame_numbers_with_separate_deltas(self):
        control = dict(parse_line(line(f)) for f in (1, 31, 61))
        candidate = dict(parse_line(line(f, 14998)) for f in (31, 61, 91))
        report = frame_report(control, candidate)
        self.assertEqual(report["window_count"], 2)
        self.assertEqual((report["from_frame"], report["to_frame"]), (31, 91))
        self.assertEqual(
            report["excluded_window_count"], {"control": 1, "candidate": 1}
        )
        self.assertEqual(
            report["windows"][0]["candidate_minus_control_ms"],
            {"p50": -0.002, "p95": 0, "p99": 0},
        )
        with self.assertRaisesRegex(ValueError, "do not match"):
            frame_report(control, candidate, first=1, last=61)

    def test_partial_or_disjoint_segments_are_not_combined(self):
        control = dict(parse_line(line(f)) for f in (1, 61))
        with self.assertRaisesRegex(ValueError, "missing"):
            frame_report(control)
        self.assertEqual(frame_report(control, first=61, last=91)["window_count"], 1)
        with self.assertRaisesRegex(ValueError, "aligned"):
            frame_report(control, first=2, last=31)
        with self.assertRaisesRegex(ValueError, "consecutive"):
            parse_line(line(1).replace("1..31", "1..32"))
        with self.assertRaisesRegex(ValueError, "quantiles"):
            parse_line(line(1, 30000))
