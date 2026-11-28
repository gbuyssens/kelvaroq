import tempfile
import unittest
from pathlib import Path

from research import compare_native_workload as workload


def pair(n):
    return f"{n:016x}/{n + 1:016x}"


def bindings(frame, n, address=1):
    return "".join(
        f'first bind pipeline={address:#x} frame={frame} stage="{stage}" size=8 hash={value:016x}\n'
        for stage, value in (("mesh", n), ("pixel", n + 1))
    )


def window(first):
    return (
        f"VELKIN_FRAME_TIMINGS frames={first}..{first + 30} samples=30 "
        "cpu_present_p50_us=16000 cpu_present_p95_us=36000 cpu_present_p99_us=41000\n"
    )


class WorkloadTests(unittest.TestCase):
    def test_detailed_mode_uses_presentx_ids_and_exact_samples_without_windows(self):
        def mark(frame, token, n):
            return f"VELKIN_MESH_BIND frame={frame} guest_token={token} mesh={n:016x} pixel={n + 1:016x}\n"

        def total(frame, draws, drawn, dispatches, dispatched):
            return f"VELKIN_QUEUE_TOTAL frame={frame} draws={draws} drawn={drawn} dispatches={dispatches} dispatched={dispatched}\n"

        data = (
            bindings(500, 1)
            + mark(11, 500, 1)
            + total(11, 100, 80, 9, 8)
            + "VELKIN_PRESENT frame=11\n"
            + "VELKIN_PRESENT frame=12 interval_us=70\n"
            + "VELKIN_PRESENT frame=13 interval_us=90\n"
            + mark(14, 508, 3)
            + "VELKIN_PRESENT frame=14 interval_us=20\n"
            + total(14, 113, 90, 14, 9)
        )
        with tempfile.TemporaryDirectory() as scratch:
            filename = Path(scratch) / "err.log"
            filename.write_text(data, encoding="utf8")
            run = workload.read_journal(filename)
            self.assertEqual(run.pairs, {pair(1): 11, pair(3): 14})
            segment = workload.compare(run, run, [pair(1), pair(3)], tail=0)[
                "anchored_intervals"
            ][0]["control"]
            self.assertEqual(
                segment["cpu_present_exact"],
                {"samples": 3, "nearest_rank_us": {"p50": 70, "p95": 90, "p99": 90}},
            )
            self.assertEqual(
                segment["queue_totals_if_exactly_covered"],
                {"draws": 13, "drawn": 10, "dispatches": 5, "dispatched": 1},
            )
            self.assertEqual(
                segment["queue_totals_source"], "cumulative_presentx_endpoints"
            )
            for missing in ("", "VELKIN_PRESENT frame=13\n"):
                filename.write_text(
                    data.replace("VELKIN_PRESENT frame=13 interval_us=90\n", missing),
                    encoding="utf8",
                )
                incomplete = workload.read_journal(filename)
                self.assertIsNone(workload.exact_present_span(incomplete, 11, 14))
            filename.write_text(data.replace("draws=113", "draws=13"), encoding="utf8")
            with self.assertRaisesRegex(ValueError, "decreasing"):
                workload.read_journal(filename)

    def test_pipeline_stage_pairing_and_bracketed_host_observation(self):
        other_stages = (
            'first bind pipeline=0xa frame=7 stage="mesh" size=8 hash=000000000000000a\n'
            'first bind pipeline=0xb frame=7 stage="pixel" size=8 hash=000000000000000b\n'
            'first bind pipeline=0xa frame=8 stage="pixel" size=8 hash=000000000000000b\n'
        )
        host = "host: 5.0 fps, 200.0 ms/frame (stuff), 87 draws/frame, dropped 19 without a host pipeline\n"
        data = (
            window(1)
            + bindings(32, 1)
            + host
            + other_stages
            + bindings(90, 1)
            + window(31)
            + window(61)
        )
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            (folder / "err.log").write_text(data, encoding="utf-16")
            (folder / "out.log").write_text(data, encoding="utf-8")
            run = workload.read_journal(folder)
        self.assertEqual(run.pairs, {pair(1): 32})
        found = workload.anchor_evidence(run, pair(1))
        self.assertEqual(found["frames_until_end_of_timing_window"], 29)
        observed = found["containing_window"][
            "host_samples_logged_between_timing_records"
        ]
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["draws_per_frame"], 87)
        self.assertEqual(found["first_complete_window_after_anchor"]["from_frame"], 61)

    def run_data(self, pairs, counts=None, windows=(1, 31, 61, 91, 121)):
        return workload.Run(
            dict(workload.timing.parse_line(window(f)) for f in windows),
            counts or {},
            pairs,
            {},
        )

    def test_order_and_session_bound(self):
        control = self.run_data({pair(1): 12, pair(3): 18, pair(5): 92})
        candidate = self.run_data({pair(1): 4, pair(5): 71, pair(7): 55})
        report = workload.compare(control, candidate, quiet=50, tail=30)
        self.assertEqual(
            [r["mesh_pixel"] for r in report["anchors"]], [pair(1), pair(5)]
        )
        self.assertTrue(all(r["reached"] for r in report["stop_bound"].values()))
        self.assertEqual(report["stop_bound"]["candidate"]["target_present_frame"], 101)
        self.assertEqual(
            report["anchored_intervals"][0]["workload_check"],
            "observed workloads differ",
        )
        for changed in ({pair(1): 80, pair(5): 7}, {pair(1): 80}):
            bad = workload.compare(control, self.run_data(changed), [pair(1), pair(5)])
            self.assertFalse(bad["all_anchors_present_in_order"]["candidate"])
            self.assertFalse(bad["anchored_intervals"])
        gap = self.run_data({pair(1): 50}, windows=(1, 61, 91))
        missed = workload.compare(gap, gap, [pair(1)], tail=30)
        self.assertFalse(missed["stop_bound"]["candidate"]["reached"])

    def test_queue_counts_must_exactly_cover_anchor_interval(self):
        counts = dict.fromkeys(workload.timing.QUEUE_FIELDS, 10)
        control = self.run_data({pair(1): 32, pair(3): 64}, {(32, 64): counts})
        for start, expected in ((64, True), (65, None)):
            candidate = self.run_data(
                {pair(1): start, pair(3): 128}, {(64, 128): counts}
            )
            parts = workload.compare(control, candidate, [pair(1), pair(3)])[
                "anchored_intervals"
            ][0]
            self.assertEqual(parts["matching_queue_totals"], expected)
        candidate = self.run_data(
            {pair(1): 64, pair(3): 128}, {(64, 128): counts | {"drawn": 5}}
        )
        parts = workload.compare(control, candidate, [pair(1), pair(3)])[
            "anchored_intervals"
        ][0]
        self.assertIs(parts["matching_queue_totals"], False)
