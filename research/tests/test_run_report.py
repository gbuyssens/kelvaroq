import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import run_report as report


class FrameReport(unittest.TestCase):
    def test_ansi_windows_and_exact_flips_keep_their_own_denominators(self):
        run = report.Run("run <1>")
        report.read_log(io.StringIO(
            "\x1b[32m10 fps, 100 ms/frame\x1b[0m\n"
            "texture refresh time 7 ms/frame\n"
            "submit thread per frame in ms: work and other time 86.0, awaiting relay 12.0, "
            "work by stage: descriptors 8.0, watch and coherence 52.0, other 26.0\n"
            "VELKIN_PRESENT frame=8 interval_us=31000\n"
            "VELKIN_PRESENT frame=9 interval_us=40000\n"
        ), run)
        self.assertEqual(run.exact, [31, 40])
        self.assertEqual(run.windows, [100])
        self.assertEqual(report.breakdown(run)[0], ("Suivi et cohérence", 52))
        page = report.render([run])
        self.assertIn("2 intervalles de présentation", page)
        self.assertIn("run &lt;1&gt;", page)
        self.assertNotIn("Travail d’envoi (global)", page)

    def test_side_by_side_with_aggregate_only_log_and_sparse_csv(self):
        first = report.Run("avant")
        report.read_log(io.StringIO(
            "10.0 fps, 100.0 ms/frame\n"
            "submit thread per frame in ms: work and other time 80, awaiting relay 12\n"
        ), first)
        second = report.Run("après")
        report.read_csv(io.StringIO("sequence,frame_us,watch_us,inplace_bytes,present_status\n1,,,,1\n3,30000,4000,0,0\n"), second)
        self.assertEqual(second.gap, 1)
        self.assertEqual(second.unsuccessful, 1)
        self.assertEqual(second.avg("inplace_bytes"), 0)
        self.assertIsNone(second.avg("from_card_bytes"))
        page = report.render([first, second])
        self.assertIn("moyennes de fenêtres", page)
        self.assertIn("Travail d’envoi (global)", page)
        self.assertEqual(page.count("<article>"), 2)
        self.assertEqual(report.quantile([1, 2, 99], .99), 99)

    def test_invalid_csv_has_no_plausible_measurements(self):
        with self.assertRaises(ValueError):
            report.read_csv(io.StringIO("a,b\n1,2\n"), report.Run("x"))


if __name__ == "__main__":
    unittest.main()
