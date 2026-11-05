import io
import itertools
import json
import math
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "eval-regression-gate"
sys.path.insert(0, str(SKILL / "scripts"))

import gate  # noqa: E402

RESULTS = SKILL / "evals" / "files" / "ticket-router" / "results"
REFUND_CASES = ["refund-0%d" % i for i in range(1, 8)]
OUTAGE_CASES = ["outage-0%d" % i for i in range(1, 8)]
PINNED_INTERVAL = (-0.305556, 0.277778)  # seed 7, 2000 resamples, see BootstrapTests


def mcnemar(worse, better):
    """Exact McNemar p-value through the gate's test, one trial per case."""
    deltas = [-1] * worse + [1] * better
    return gate.exact_sign_flip_p([abs(d) for d in deltas], abs(sum(deltas)))


def exact(deltas):
    return gate.exact_sign_flip_p([abs(d) for d in deltas if d], abs(sum(deltas)))


class ExactTestTests(unittest.TestCase):
    def test_mcnemar_tables_computed_by_hand(self):
        # p = 2 * P(X <= min(worse, better)) for X ~ Binomial(worse + better, 1/2)
        self.assertEqual(mcnemar(5, 0), 2 * 1 / 32.0)                 # 0.0625
        self.assertEqual(mcnemar(6, 0), 2 * 1 / 64.0)                 # 0.03125
        self.assertEqual(mcnemar(8, 1), 2 * (1 + 9) / 512.0)          # 0.0390625
        self.assertEqual(mcnemar(7, 2), 2 * (1 + 9 + 36) / 512.0)     # 0.1796875
        self.assertEqual(mcnemar(10, 2), 2 * (1 + 12 + 66) / 4096.0)  # 0.03857421875
        self.assertEqual(mcnemar(1, 0), 1.0)
        self.assertEqual(mcnemar(3, 3), 1.0)
        self.assertEqual(mcnemar(0, 0), 1.0)

    def test_direction_does_not_change_the_two_sided_p_value(self):
        self.assertEqual(mcnemar(8, 1), mcnemar(1, 8))

    def test_mcnemar_matches_the_binomial_formula_on_a_grid(self):
        for worse in range(0, 13):
            for better in range(0, 13):
                total, low = worse + better, min(worse, better)
                tail = sum(math.comb(total, i) for i in range(low + 1)) / float(2 ** total)
                expected = 1.0 if worse == better else min(1.0, 2 * tail)
                self.assertAlmostEqual(mcnemar(worse, better), expected, places=12)

    def test_multi_trial_cases_computed_by_hand(self):
        # Differences -2, -1, +1 passed trials. The eight sign patterns of
        # (2, 1, 1) sum to 4, 2, 2, 0, 0, -2, -2, -4. Six are at least 2 from zero.
        self.assertEqual(exact([-2, -1, 1]), 6 / 8.0)
        # (2, 2, 2, 1): only -7, -5, +5, +7 are at least 5 from zero: 4 of 16.
        self.assertEqual(exact([-2, -2, -2, 1]), 4 / 16.0)
        # Six cases that each lost all three trials: 2 of 64 patterns.
        self.assertEqual(exact([-3] * 6), 2 / 64.0)
        # Unchanged cases carry no information.
        self.assertEqual(exact([-3] * 6 + [0] * 50), 2 / 64.0)
        self.assertEqual(exact([-2, 2]), 1.0)

    def test_matches_brute_force_enumeration(self):
        weights = [3, 1, 2, 2, 1, 3, 1, 2]
        sums = [sum(sign * w for sign, w in zip(signs, weights))
                for signs in itertools.product((1, -1), repeat=len(weights))]
        for observed in range(1, sum(weights) + 1):
            expected = sum(1 for s in sums if abs(s) >= observed) / float(len(sums))
            self.assertEqual(gate.exact_sign_flip_p(weights, observed), expected)

    def test_many_changed_cases_do_not_overflow(self):
        # 2^1100 does not fit in a float; the counting must stay in integers.
        # With 1100 unit weights every sum is even, so p = 1 - P(sum = 0).
        expected = 1 - math.comb(1100, 550) / 2 ** 1100
        self.assertAlmostEqual(gate.exact_sign_flip_p([1] * 1100, 2), expected, places=12)
        self.assertEqual(gate.exact_sign_flip_p([1] * 1100, 1100), 0.0)  # 2^-1099 underflows

    def test_normal_approximation_is_close_and_only_used_above_the_budget(self):
        for weights, observed in (([1] * 400, 44), ([3] * 300, 120), ([1, 2, 3] * 100, 70)):
            self.assertAlmostEqual(gate.normal_sign_flip_p(weights, observed),
                                   gate.exact_sign_flip_p(weights, observed), delta=0.002)
        self.assertEqual(gate.normal_sign_flip_p([1, 1], 0), 1.0)
        deltas = [-1] * 8 + [1]
        self.assertEqual(gate.paired_test(deltas), (20 / 512.0, True))
        saved = gate.EXACT_WORK_LIMIT
        gate.EXACT_WORK_LIMIT = 10
        try:
            p_value, is_exact = gate.paired_test(deltas)
        finally:
            gate.EXACT_WORK_LIMIT = saved
        self.assertFalse(is_exact)
        self.assertAlmostEqual(p_value, math.erfc((7 - 1) / 3.0 / math.sqrt(2)), places=12)


class BootstrapTests(unittest.TestCase):
    def test_same_seed_gives_the_same_interval(self):
        deltas = [-3, -1, 0, 0, 2, 0, 1, -2, 0, 0, 3, -1]
        first = gate.bootstrap_interval(deltas, 3, 2000, 0.95, 7)
        self.assertEqual(first, gate.bootstrap_interval(deltas, 3, 2000, 0.95, 7))
        self.assertLessEqual(first[0], sum(deltas) / 36.0)
        self.assertGreaterEqual(first[1], sum(deltas) / 36.0)

    def test_interval_is_pinned_for_a_known_seed(self):
        # Fails if the resampling scheme or the generator ever changes.
        deltas = [-3, -1, 0, 0, 2, 0, 1, -2, 0, 0, 3, -1]
        low, high = gate.bootstrap_interval(deltas, 3, 2000, 0.95, 7)
        self.assertEqual((round(low, 6), round(high, 6)), PINNED_INTERVAL)

    def test_no_variation_gives_a_zero_width_interval(self):
        self.assertEqual(gate.bootstrap_interval([0] * 25, 3, 1000, 0.95, 0), (0.0, 0.0))
        self.assertEqual(gate.bootstrap_interval([-1] * 25, 1, 1000, 0.95, 0), (-1.0, -1.0))

    def test_limits_computed_by_hand_from_the_binomial_distribution(self):
        # One of 4 cases got worse. A resample holds it j times, j ~ Binomial(4, 1/4):
        # P(j = 4) = 0.0039 and P(j >= 3) = 0.0508, so the 2.5% point is j = 3.
        # P(j = 0) = 0.316, so the 97.5% point is j = 0.
        for seed in (0, 1, 2):
            self.assertEqual(gate.bootstrap_interval([-1, 0, 0, 0], 1, 10000, 0.95, seed),
                             (-0.75, 0.0))
        # The same case with three trials each: the scale changes, the answer does not.
        self.assertEqual(gate.bootstrap_interval([-3, 0, 0, 0], 3, 10000, 0.95, 0), (-0.75, 0.0))
        # One of 10 worse, j ~ Binomial(10, 1/10): P(j >= 4) = 0.0128, P(j >= 3) = 0.0702.
        self.assertEqual(gate.bootstrap_interval([-1] + [0] * 9, 1, 10000, 0.95, 0), (-0.3, 0.0))
        # One worse and one better: the mean is -1, 0 or +1 with chance 1/4, 1/2, 1/4.
        self.assertEqual(gate.bootstrap_interval([-1, 1], 1, 10000, 0.95, 0), (-1.0, 1.0))


class ThresholdTests(unittest.TestCase):
    def test_min_cases_is_the_smallest_n_with_enough_power(self):
        self.assertEqual(gate.min_cases_for(0.05, 0.05), 72)
        self.assertEqual(gate.min_cases_for(0.10, 0.05), 36)
        self.assertEqual(gate.min_cases_for(0.02, 0.05), 183)
        for margin in (0.02, 0.05, 0.10, 0.20):
            cases = gate.min_cases_for(margin, 0.05)
            self.assertLessEqual((1 - margin) ** cases, 0.025)
            self.assertGreater((1 - margin) ** (cases - 1), 0.025)

    def test_smallest_drop_is_the_inverse(self):
        self.assertAlmostEqual(gate.smallest_drop(30, 0.05), 0.1157, places=4)
        self.assertLessEqual(gate.smallest_drop(72, 0.05), 0.05)
        self.assertGreater(gate.smallest_drop(71, 0.05), 0.05)

    def test_line_ranges(self):
        self.assertEqual(gate.line_ranges([7, 5, 6]), "5-7")
        self.assertEqual(gate.line_ranges([5, 6, 7, 12]), "5-7,12")
        self.assertEqual(gate.line_ranges([3]), "3")
        self.assertEqual(gate.line_ranges([2, 4, 5]), "2,4-5")


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, outcomes, sha=True, edited=()):
        """outcomes maps case_id to a string of trial results: P, F or E."""
        lines = [json.dumps({"type": "meta", "label": name})]
        for case_id, marks in outcomes.items():
            for trial, mark in enumerate(marks, 1):
                record = {"type": "trial", "case_id": case_id, "trial": trial, "detail": "",
                          "status": {"P": "pass", "F": "fail", "E": "error"}[mark]}
                if sha:
                    record["case_sha"] = ("edited-" if case_id in edited else "sha-") + case_id
                lines.append(json.dumps(record))
        path = self.dir / (name + ".jsonl")
        path.write_text("\n".join(lines) + "\n")
        return str(path)

    def run_gate(self, baseline, candidate, extra=()):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = gate.main(["--baseline", baseline, "--candidate", candidate, "--json"]
                             + list(extra))
        return code, (json.loads(stdout.getvalue()) if code != 2 else None), stderr.getvalue()

    def suite(self, cases, marks="P"):
        return {"c%03d" % i: marks for i in range(cases)}

    def test_identical_results_on_enough_cases_pass(self):
        outcomes = self.suite(80)
        code, result, _ = self.run_gate(self.write("b", outcomes), self.write("c", outcomes))
        self.assertEqual((code, result["verdict"]), (0, "PASS"))
        self.assertEqual(result["test"]["p_value"], 1.0)
        self.assertEqual((result["cases"], result["trials_per_case"]), (80, 1))
        self.assertEqual(result["thresholds"]["min_cases"], 72)

    def test_identical_results_on_a_small_suite_are_inconclusive(self):
        outcomes = self.suite(30, "PPP")
        code, result, _ = self.run_gate(self.write("b", outcomes), self.write("c", outcomes))
        self.assertEqual((code, result["verdict"]), (3, "INCONCLUSIVE"))
        self.assertIn("30 cases is fewer than the 72 needed", " ".join(result["reasons"]))
        # A wider margin is a weaker claim that 30 cases can support.
        code, result, _ = self.run_gate(self.write("b", outcomes), self.write("c", outcomes),
                                        ["--margin", "0.12"])
        self.assertEqual((code, result["verdict"]), (0, "PASS"))
        self.assertEqual(result["thresholds"]["min_cases"], 29)

    def test_six_cases_worse_and_none_better_is_a_fail(self):
        base = self.suite(20)
        cand = dict(base, c000="F", c001="F", c002="F", c003="F", c004="F", c005="F")
        code, result, _ = self.run_gate(self.write("b", base), self.write("c", cand))
        self.assertEqual((code, result["verdict"]), (1, "FAIL"))
        self.assertEqual(result["test"]["name"], "exact McNemar test")
        self.assertEqual(result["test"]["p_value"], 0.03125)
        self.assertEqual(result["difference"], -0.3)
        self.assertEqual([row["case_id"] for row in result["regressed"]],
                         ["c00%d" % i for i in range(6)])
        self.assertEqual(result["regressed"][0]["candidate_lines"], "2")

    def test_five_cases_worse_is_not_significant_and_not_a_pass(self):
        base = self.suite(100)
        cand = dict(base, c000="F", c001="F", c002="F", c003="F", c004="F")
        code, result, _ = self.run_gate(self.write("b", base), self.write("c", cand))
        self.assertEqual(result["test"]["p_value"], 0.0625)
        self.assertEqual((code, result["verdict"]), (3, "INCONCLUSIVE"))
        self.assertLess(result["interval"]["low"], -0.05)
        self.assertIn("beyond the -5.0 point margin", " ".join(result["reasons"]))

    def test_six_cases_better_and_none_worse_passes_on_a_small_suite(self):
        base = dict(self.suite(20), c000="F", c001="F", c002="F", c003="F", c004="F", c005="F")
        code, result, _ = self.run_gate(self.write("b", base), self.write("c", self.suite(20)))
        self.assertEqual((code, result["verdict"]), (0, "PASS"))
        self.assertIn("better than the baseline", result["reasons"][0])
        self.assertEqual(len(result["improved"]), 6)

    def test_flaky_cases_are_reported_per_side(self):
        base = dict(self.suite(10, "PPP"), c000="PFP", c001="FFF")
        cand = dict(self.suite(10, "PPP"), c000="PPP", c001="FFF", c002="FPF")
        _, result, _ = self.run_gate(self.write("b", base), self.write("c", cand))
        self.assertEqual([(r["case_id"], r["baseline_passes"], r["candidate_passes"])
                          for r in result["flaky"]], [("c000", 2, 3), ("c002", 3, 1)])
        self.assertEqual(result["test"]["name"], "exact sign-flip test")
        self.assertEqual(result["baseline"]["passes"], 26)
        self.assertEqual(result["candidate"]["passes"], 25)

    def test_error_trials_make_the_verdict_inconclusive_unless_counted(self):
        base = self.suite(20)
        cand = dict(base, c000="E", c001="E", c002="E", c003="E", c004="E", c005="E")
        files = (self.write("b", base), self.write("c", cand))
        code, result, _ = self.run_gate(*files)
        self.assertEqual((code, result["verdict"]), (3, "INCONCLUSIVE"))
        self.assertEqual(result["candidate"]["errors"], 6)
        self.assertIn("6 error trial(s)", result["reasons"][0])
        code, result, _ = self.run_gate(*files, extra=["--count-errors-as-failures"])
        self.assertEqual((code, result["verdict"]), (1, "FAIL"))
        self.assertIn("counted as failures", " ".join(result["reasons"]))

    def test_removed_added_or_edited_cases_are_refused(self):
        base = self.suite(20)
        fewer = dict(base)
        del fewer["c007"]
        code, _, stderr = self.run_gate(self.write("b", base), self.write("c", fewer))
        self.assertEqual(code, 2)
        self.assertIn("1 case(s) in the baseline are missing from the candidate: c007", stderr)
        code, _, stderr = self.run_gate(self.write("b", fewer), self.write("c", base))
        self.assertEqual(code, 2)
        self.assertIn("1 case(s) in the candidate are not in the baseline: c007", stderr)
        code, _, stderr = self.run_gate(self.write("b", base),
                                        self.write("c", base, edited=("c003",)))
        self.assertEqual(code, 2)
        self.assertIn("different input or grader on the two sides: c003", stderr)

    def test_unequal_trial_counts_are_refused(self):
        code, _, stderr = self.run_gate(self.write("b", self.suite(20, "PPP")),
                                        self.write("c", self.suite(20, "P")))
        self.assertEqual(code, 2)
        self.assertIn("same number of trials (found 1, 3)", stderr)

    def test_results_without_fingerprints_are_compared_with_a_warning(self):
        outcomes = self.suite(80)
        code, result, _ = self.run_gate(self.write("b", outcomes, sha=False),
                                        self.write("c", outcomes, sha=False))
        self.assertEqual(code, 0)
        self.assertIn("no case_sha", result["reasons"][-1])

    def test_broken_result_files_are_input_errors(self):
        good = self.write("b", self.suite(3))
        for name, text, expected in (
                ("bad.jsonl", "{not json\n", "bad.jsonl:1 is not valid JSON"),
                ("status.jsonl", '{"case_id": "a", "trial": 1, "status": "ok"}\n', "'status' must be"),
                ("twice.jsonl", '{"case_id": "a", "trial": 1, "status": "pass"}\n' * 2,
                 "twice.jsonl:2: case 'a' trial 1 appears twice"),
                ("empty.jsonl", '{"type": "meta"}\n', "contains no trial records")):
            path = self.dir / name
            path.write_text(text)
            code, _, stderr = self.run_gate(good, str(path))
            self.assertEqual(code, 2)
            self.assertIn(expected, stderr)
        code, _, stderr = self.run_gate(good, str(self.dir / "missing.jsonl"))
        self.assertEqual(code, 2)
        self.assertIn("cannot read", stderr)

    def test_bad_thresholds_are_usage_errors(self):
        for extra in (["--margin", "0"], ["--margin", "5"], ["--confidence", "95"],
                      ["--resamples", "10"], ["--min-cases", "0"]):
            with self.assertRaises(SystemExit) as caught, redirect_stderr(io.StringIO()):
                gate.main(["--baseline", "b", "--candidate", "c"] + extra)
            self.assertEqual(caught.exception.code, 2)


class FixtureTests(unittest.TestCase):
    """The three planted result sets and the answer each must give."""

    def run_gate(self, candidate, extra=()):
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ["--baseline", str(RESULTS / "baseline.jsonl"), "--candidate", str(candidate)]
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = gate.main(argv + list(extra))
        return code, stdout.getvalue(), stderr.getvalue()

    def result(self, name):
        code, stdout, _ = self.run_gate(RESULTS / name, ["--json"])
        return code, json.loads(stdout)

    def test_real_regression_fails_and_names_every_refund_case(self):
        code, result = self.result("candidate-regression.jsonl")
        self.assertEqual((code, result["verdict"]), (1, "FAIL"))
        regressed = {row["case_id"]: row for row in result["regressed"]}
        for case_id in REFUND_CASES:
            self.assertEqual((regressed[case_id]["baseline_passes"],
                              regressed[case_id]["candidate_passes"]), (3, 0))
        self.assertEqual(regressed["refund-01"]["candidate_lines"], "2-4")
        self.assertIn('expected "refund"', regressed["refund-01"]["candidate_detail"])
        # Seven cases lost 3 trials, one lost 1, one gained 1: observed sum -21.
        # Of 2^9 sign patterns, 3 reach -21 or lower, so p = 2 * 3 / 512.
        self.assertEqual(result["test"]["p_value"], 6 / 512.0)
        self.assertEqual((result["cases"], result["trials_per_case"]), (30, 3))
        self.assertEqual((result["baseline"]["passes"], result["candidate"]["passes"]), (65, 44))
        self.assertEqual((result["interval"]["low"], result["interval"]["high"]),
                         (-0.4, -0.088889))

    def test_noise_only_is_inconclusive_and_never_a_fail(self):
        code, result = self.result("candidate-noise.jsonl")
        self.assertEqual((code, result["verdict"]), (3, "INCONCLUSIVE"))
        self.assertEqual(result["test"]["p_value"], 1.0)
        changed = [row["case_id"] for row in result["regressed"] + result["improved"]]
        self.assertEqual(sorted(changed), ["mixed-01", "mixed-02", "mixed-03"])
        self.assertEqual(sorted(row["case_id"] for row in result["flaky"]), sorted(changed))
        self.assertEqual((result["interval"]["low"], result["interval"]["high"]),
                         (-0.055556, 0.022222))

    def test_real_improvement_passes_and_names_every_outage_case(self):
        code, result = self.result("candidate-improvement.jsonl")
        self.assertEqual((code, result["verdict"]), (0, "PASS"))
        improved = {row["case_id"]: row for row in result["improved"]}
        for case_id in OUTAGE_CASES:
            self.assertEqual((improved[case_id]["baseline_passes"],
                              improved[case_id]["candidate_passes"]), (0, 3))
        self.assertEqual(result["regressed"], [])
        self.assertEqual(result["test"]["p_value"], 2 / 512.0)
        self.assertEqual((result["interval"]["low"], result["interval"]["high"]),
                         (0.111111, 0.411111))

    def test_baseline_against_itself_shows_no_change(self):
        code, result = self.result("baseline.jsonl")
        self.assertEqual((code, result["verdict"]), (3, "INCONCLUSIVE"))
        self.assertEqual((result["difference"], result["regressed"], result["improved"]),
                         (0.0, [], []))

    def test_deleting_a_failing_case_from_the_candidate_is_refused(self):
        lines = (RESULTS / "candidate-regression.jsonl").read_text().splitlines()
        with tempfile.TemporaryDirectory() as tmp:
            trimmed = Path(tmp) / "trimmed.jsonl"
            trimmed.write_text("\n".join(l for l in lines if '"refund-' not in l) + "\n")
            code, _, stderr = self.run_gate(trimmed)
        self.assertEqual(code, 2)
        self.assertIn("7 case(s) in the baseline are missing from the candidate", stderr)

    def test_text_report_states_cases_trials_seed_and_verdict(self):
        code, stdout, _ = self.run_gate(RESULTS / "candidate-regression.jsonl")
        self.assertEqual(code, 1)
        for expected in ("cases: 30   trials per case: 3", "baseline 72.2% (65/90)",
                         "candidate 48.9% (44/90)", "-23.3 points", "10000 resamples, seed 0",
                         "p = 0.0117", "refund-07  3/3 -> 0/3", "VERDICT: FAIL"):
            self.assertIn(expected, stdout)
        self.assertEqual(stdout, self.run_gate(RESULTS / "candidate-regression.jsonl")[1])


class ValidateRepoTests(unittest.TestCase):
    def test_repository_passes_its_own_validator(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import validate
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = validate.main(["validate.py", str(ROOT)])
        self.assertEqual(code, 0, buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
