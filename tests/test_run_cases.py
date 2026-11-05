import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "eval-regression-gate"
sys.path.insert(0, str(SKILL / "scripts"))

import run_cases  # noqa: E402

FIXTURE = SKILL / "evals" / "files" / "ticket-router"

# The "model command" in these tests is this local stub. It never calls a model.
# It upper-cases stdin, reports the environment it was given, and misbehaves on
# request so that error handling can be tested.
STUB = """
import os, sys, time
text = sys.stdin.read()
if text == "crash":
    sys.stderr.write("first line\\nrate limited\\n")
    sys.exit(3)
if text == "hang":
    time.sleep(5)
if text == "flaky":
    print("YES" if os.environ["EVAL_TRIAL"] == "2" else "NO")
elif text == "env":
    print(os.environ["EVAL_CASE_ID"] + "/" + os.environ["EVAL_TRIAL"])
else:
    print(text.upper())
"""


def grade(grader, output):
    return run_cases.grade_one(grader, output)


class GraderTests(unittest.TestCase):
    def test_exact_ignores_outer_whitespace_only(self):
        self.assertEqual(grade({"type": "exact", "value": "PARIS"}, "  PARIS\n"), (True, ""))
        passed, reason = grade({"type": "exact", "value": "PARIS"}, "Paris")
        self.assertFalse(passed)
        self.assertIn('"Paris"', reason)
        self.assertFalse(grade({"type": "exact", "value": "PARIS"}, "PARIS, France")[0])

    def test_exact_ignore_case(self):
        self.assertTrue(grade({"type": "exact", "value": "PARIS", "ignore_case": True}, "paris")[0])

    def test_contains_and_negate(self):
        self.assertTrue(grade({"type": "contains", "value": "30 days"}, "within 30 days.")[0])
        self.assertFalse(grade({"type": "contains", "value": "30 days"}, "within 30 Days.")[0])
        self.assertTrue(grade({"type": "contains", "value": "30 days", "ignore_case": True},
                              "within 30 Days.")[0])
        self.assertTrue(grade({"type": "contains", "value": "sorry", "negate": True}, "done")[0])
        passed, reason = grade({"type": "contains", "value": "sorry", "negate": True}, "sorry!")
        self.assertFalse(passed)
        self.assertIn("must not appear", reason)

    def test_regex_searches_anywhere_and_can_be_negated(self):
        self.assertTrue(grade({"type": "regex", "pattern": "[0-9]+ days"}, "in 14 days")[0])
        self.assertFalse(grade({"type": "regex", "pattern": "^[0-9]+ days"}, "in 14 days")[0])
        self.assertTrue(grade({"type": "regex", "pattern": "DAYS", "ignore_case": True}, "14 days")[0])
        self.assertFalse(grade({"type": "regex", "pattern": "days", "negate": True}, "14 days")[0])

    def test_json_field_follows_keys_and_indexes(self):
        output = json.dumps({"items": [{"sku": "A1"}, {"sku": "B2"}], "a.b": 5})
        self.assertTrue(grade({"type": "json_field", "path": "items.1.sku", "value": "B2"}, output)[0])
        self.assertTrue(grade({"type": "json_field", "path": ["a.b"], "value": 5}, output)[0])
        passed, reason = grade({"type": "json_field", "path": "items.0.sku", "value": "B2"}, output)
        self.assertFalse(passed)
        self.assertEqual(reason, 'json_field: items.0.sku is "A1", expected "B2"')
        passed, reason = grade({"type": "json_field", "path": "items.2.sku", "value": "B2"}, output)
        self.assertEqual((passed, reason), (False, "json_field: no field at path items.2.sku"))

    def test_json_field_rejects_output_that_is_not_json(self):
        passed, reason = grade({"type": "json_field", "path": "a", "value": 1}, "Sure! {\"a\": 1}")
        self.assertEqual((passed, reason), (False, "json_field: output is not valid JSON"))

    def test_json_equality_separates_booleans_from_numbers(self):
        self.assertTrue(run_cases.json_equal(1, 1.0))
        self.assertFalse(run_cases.json_equal(True, 1))
        self.assertFalse(run_cases.json_equal(0, False))
        self.assertFalse(run_cases.json_equal("1", 1))
        self.assertTrue(run_cases.json_equal(None, None))
        self.assertTrue(run_cases.json_equal({"a": [1, {"b": None}]}, {"a": [1.0, {"b": None}]}))
        self.assertFalse(run_cases.json_equal({"a": [1, 2]}, {"a": [1]}))

    def test_every_grader_in_a_list_must_pass(self):
        graders = [{"type": "contains", "value": "a"}, {"type": "contains", "value": "b"}]
        self.assertEqual(run_cases.grade(graders, "ab"), (True, ""))
        passed, reason = run_cases.grade(graders, "a")
        self.assertFalse(passed)
        self.assertIn('"b"', reason)


class LoadCasesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def error_for(self, name, text):
        with self.assertRaises(run_cases.InputError) as caught:
            run_cases.load_cases(self.write(name, text))
        return str(caught.exception)

    def test_reads_json_lines_json_lists_and_wrapped_lists(self):
        case = {"id": "a", "input": "x", "grader": {"type": "exact", "value": "X"}}
        for name, text in (("c.jsonl", "\n" + json.dumps(case) + "\n\n"),
                           ("c.json", json.dumps([case])),
                           ("d.json", json.dumps({"cases": [case]}))):
            cases = run_cases.load_cases(self.write(name, text))
            self.assertEqual([c["id"] for c in cases], ["a"])
            self.assertEqual(cases[0]["input_text"], "x")

    def test_non_string_input_is_sent_as_json_text(self):
        case = {"id": "a", "input": {"b": 1, "a": [2]}, "grader": {"type": "exact", "value": "X"}}
        cases = run_cases.load_cases(self.write("c.jsonl", json.dumps(case)))
        self.assertEqual(cases[0]["input_text"], '{"a": [2], "b": 1}')

    def test_bad_cases_are_rejected_with_their_location(self):
        good = {"type": "exact", "value": "X"}
        bad_cases = {
            "duplicate case id": [{"id": "a", "input": "x", "grader": good},
                                  {"id": "a", "input": "y", "grader": good}],
            "needs a non-empty string 'id'": [{"input": "x", "grader": good}],
            "needs an 'input'": [{"id": "a", "grader": good}],
            "exactly one of 'grader' or 'graders'": [{"id": "a", "input": "x"}],
            "exactly one of 'grader' or 'graders' ": [
                {"id": "a", "input": "x", "grader": good, "graders": [good]}],
            "grader needs a 'type'": [{"id": "a", "input": "x", "grader": {"type": "llm"}}],
            "does not compile": [{"id": "a", "input": "x",
                                  "grader": {"type": "regex", "pattern": "("}}],
            "does not accept ignorecase": [{"id": "a", "input": "x", "grader": {
                "type": "exact", "value": "X", "ignorecase": True}}],
            "does not accept negate": [{"id": "a", "input": "x", "grader": {
                "type": "exact", "value": "X", "negate": True}}],
            "needs a 'value'": [{"id": "a", "input": "x",
                                 "grader": {"type": "json_field", "path": "a"}}],
            "needs a 'path'": [{"id": "a", "input": "x",
                                "grader": {"type": "json_field", "value": 1}}],
            "must be a non-empty list": [{"id": "a", "input": "x", "graders": []}],
        }
        for expected, cases in bad_cases.items():
            text = "\n".join(json.dumps(case) for case in cases)
            message = self.error_for("c.jsonl", text)
            self.assertIn(expected.strip(), message)
            self.assertIn("c.jsonl:%d" % len(cases), message)

    def test_broken_files_are_rejected(self):
        self.assertIn("c.jsonl:2 is not valid JSON", self.error_for("c.jsonl", "{}\n{oops\n"))
        self.assertIn("is not valid JSON", self.error_for("c.json", "[1,"))
        self.assertIn("must hold a list", self.error_for("c.json", '{"tests": []}'))
        self.assertIn("contains no cases", self.error_for("c.jsonl", "\n"))
        self.assertIn("unsupported cases file type", self.error_for("c.txt", "x"))
        with self.assertRaises(run_cases.InputError):
            run_cases.load_cases(self.dir / "missing.jsonl")

    def test_fingerprint_follows_input_and_graders_only(self):
        base = {"id": "a", "input": "x", "grader": {"type": "exact", "value": "X"}}
        noted = dict(base, notes="reviewed by QA")
        looser = dict(base, grader={"type": "contains", "value": "X"})
        other_input = dict(base, input="y")
        sha = [run_cases.load_cases(self.write("c%d.jsonl" % i, json.dumps(case)))[0]["sha"]
               for i, case in enumerate((base, noted, looser, other_input))]
        self.assertEqual(sha[0], sha[1])
        self.assertNotEqual(sha[0], sha[2])
        self.assertNotEqual(sha[0], sha[3])

    def test_yaml_without_pyyaml_explains_how_to_convert(self):
        saved = sys.modules.get("yaml", "absent")
        sys.modules["yaml"] = None  # makes "import yaml" raise ImportError
        try:
            message = self.error_for("c.yaml", "- id: a\n")
        finally:
            if saved == "absent":
                del sys.modules["yaml"]
            else:
                sys.modules["yaml"] = saved
        self.assertIn("PyYAML is not installed", message)
        self.assertIn("Convert it to JSON", message)

    def test_yaml_is_read_when_a_yaml_module_is_importable(self):
        case = {"id": "a", "input": "x", "grader": {"type": "exact", "value": "X"}}
        fake = types.ModuleType("yaml")
        fake.YAMLError = ValueError
        fake.safe_load = lambda text: [case]
        saved = sys.modules.get("yaml", "absent")
        sys.modules["yaml"] = fake
        try:
            cases = run_cases.load_cases(self.write("c.yml", "ignored by the fake module"))
        finally:
            if saved == "absent":
                del sys.modules["yaml"]
            else:
                sys.modules["yaml"] = saved
        self.assertEqual([c["id"] for c in cases], ["a"])


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.stub = self.dir / "stub_model.py"
        self.stub.write_text(STUB)
        self.out = self.dir / "out" / "results.jsonl"

    def cases_file(self, cases):
        path = self.dir / "cases.jsonl"
        path.write_text("\n".join(json.dumps(case) for case in cases))
        return str(path)

    def run_main(self, cases, extra=(), command=None):
        argv = ["--cases", self.cases_file(cases), "--out", str(self.out)] + list(extra)
        argv += ["--"] + (command or [sys.executable, str(self.stub)])
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = run_cases.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def records(self):
        return [json.loads(line) for line in self.out.read_text().splitlines()]

    def test_results_hold_one_line_per_trial_in_case_then_trial_order(self):
        cases = [{"id": "up", "input": "abc", "grader": {"type": "exact", "value": "ABC"}},
                 {"id": "wrong", "input": "abc", "grader": {"type": "exact", "value": "abc"}}]
        code, stdout, _ = self.run_main(cases, ["--trials", "2", "--label", "baseline"])
        records = self.records()
        self.assertEqual(code, 0)  # a failed grade is a result, not an error
        self.assertEqual(records[0]["type"], "meta")
        self.assertEqual((records[0]["label"], records[0]["cases"], records[0]["trials"]),
                         ("baseline", 2, 2))
        self.assertEqual([(r["case_id"], r["trial"], r["status"]) for r in records[1:]],
                         [("up", 1, "pass"), ("up", 2, "pass"),
                          ("wrong", 1, "fail"), ("wrong", 2, "fail")])
        self.assertEqual(records[1]["output"], "ABC\n")
        self.assertIn("exact: output is", records[3]["detail"])
        self.assertIn("pass rate: 50.0% (2/4)", stdout)
        self.assertIn("%s:4" % self.out, stdout)  # evidence: first failing line

    def test_case_id_and_trial_reach_the_command_and_flaky_cases_are_listed(self):
        cases = [{"id": "c-1", "input": "env", "grader": {"type": "exact", "value": "c-1/2"}},
                 {"id": "f-1", "input": "flaky", "grader": {"type": "exact", "value": "YES"}}]
        code, stdout, _ = self.run_main(cases, ["--trials", "3", "--json"])
        summary = json.loads(stdout)
        self.assertEqual(code, 0)
        self.assertEqual([r["status"] for r in self.records()[1:]],
                         ["fail", "pass", "fail", "fail", "pass", "fail"])
        self.assertEqual((summary["passes"], summary["fails"], summary["errors"]), (2, 4, 0))
        self.assertEqual([(row["case_id"], row["kind"], row["passes"])
                          for row in summary["not_passing"]],
                         [("c-1", "flaky", 1), ("f-1", "flaky", 1)])

    def test_a_failing_command_is_an_error_not_a_failed_grade(self):
        cases = [{"id": "boom", "input": "crash", "grader": {"type": "contains", "value": "x"}},
                 {"id": "fine", "input": "x", "grader": {"type": "contains", "value": "X"}}]
        code, stdout, _ = self.run_main(cases)
        records = self.records()
        self.assertEqual(code, 1)
        self.assertEqual(records[1]["status"], "error")
        self.assertEqual(records[1]["detail"], "command exited with code 3: rate limited")
        self.assertEqual(records[2]["status"], "pass")
        self.assertIn("ERRORS: 1 trial(s)", stdout)

    def test_a_timeout_is_an_error(self):
        cases = [{"id": "slow", "input": "hang", "grader": {"type": "contains", "value": "x"}}]
        code, _, _ = self.run_main(cases, ["--timeout", "0.5"])
        self.assertEqual(code, 1)
        self.assertEqual(self.records()[1]["status"], "error")
        self.assertIn("timed out", self.records()[1]["detail"])

    def test_parallel_jobs_keep_the_same_order(self):
        cases = [{"id": "c%02d" % i, "input": "v%d" % i,
                  "grader": {"type": "exact", "value": "V%d" % i}} for i in range(8)]
        code, _, _ = self.run_main(cases, ["--trials", "2", "--jobs", "4"])
        self.assertEqual(code, 0)
        self.assertEqual([(r["case_id"], r["trial"]) for r in self.records()[1:]],
                         [("c%02d" % i, t) for i in range(8) for t in (1, 2)])
        self.assertTrue(all(r["status"] == "pass" for r in self.records()[1:]))

    def test_long_output_is_truncated_and_marked(self):
        cases = [{"id": "long", "input": "abcdefghij", "grader": {"type": "contains", "value": "J"}}]
        self.run_main(cases, ["--max-output-chars", "4"])
        record = self.records()[1]
        self.assertEqual((record["status"], record["output"], record["output_truncated"]),
                         ("pass", "ABCD", True))

    def test_unusable_input_exits_with_code_2_and_a_message(self):
        good = [{"id": "a", "input": "x", "grader": {"type": "exact", "value": "X"}}]
        code, _, stderr = self.run_main(good, command=["no-such-binary-xyz"])
        self.assertEqual(code, 2)
        self.assertIn("cannot run command", stderr)
        self.assertFalse(self.out.exists())
        code, _, stderr = self.run_main([{"id": "a", "input": "x"}])
        self.assertEqual(code, 2)
        self.assertIn("exactly one of 'grader' or 'graders'", stderr)
        with self.assertRaises(SystemExit) as caught, redirect_stderr(io.StringIO()):
            run_cases.main(["--cases", "c.jsonl", "--out", "o.jsonl"])  # no command
        self.assertEqual(caught.exception.code, 2)


class FixtureTests(unittest.TestCase):
    """The committed result sets are exactly what the stub produces."""

    def regenerate(self, variant):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "results.jsonl"
            with redirect_stdout(io.StringIO()):
                code = run_cases.main([
                    "--cases", str(FIXTURE / "cases.jsonl"), "--trials", "3", "--jobs", "4",
                    "--out", str(out), "--",
                    sys.executable, str(FIXTURE / "fake_model.py"), "--variant", variant])
            return code, out.read_text().splitlines()[1:]

    def test_regenerated_results_match_the_committed_fixtures(self):
        for variant, name in (("baseline", "baseline"), ("regression", "candidate-regression"),
                              ("noise", "candidate-noise"),
                              ("improvement", "candidate-improvement")):
            code, lines = self.regenerate(variant)
            committed = (FIXTURE / "results" / (name + ".jsonl")).read_text().splitlines()
            self.assertEqual(code, 0)
            self.assertEqual(len(committed), 91)  # 1 run line + 30 cases x 3 trials
            self.assertEqual(lines, committed[1:], "fixture %s is out of date" % name)

    def test_the_suite_uses_every_grader_type(self):
        cases = run_cases.load_cases(FIXTURE / "cases.jsonl")
        kinds = set(g["type"] for case in cases for g in case["graders"])
        self.assertEqual(len(cases), 30)
        self.assertEqual(kinds, {"exact", "contains", "regex", "json_field"})


if __name__ == "__main__":
    unittest.main()
