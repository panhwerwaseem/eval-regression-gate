#!/usr/bin/env python3
"""Run an eval suite by calling your own command once per case and trial.

For each case, the command given after "--" is started directly (no shell),
the case input is written to its stdin, and its stdout is graded. The command
is whatever makes your model or agent call. This script never calls a model
and contains no network code.

Cases file: JSON Lines (one case per line), or a JSON list of cases.

  {"id": "refund-01", "input": "I was charged twice.",
   "grader": {"type": "json_field", "path": "category", "value": "refund"}}

"input" is a string, or any JSON value (sent to the command as JSON text).
Use "grader" for one grader, or "graders" for a list that must all pass.

  exact       {"type": "exact", "value": "PARIS"}
              output equals value (leading and trailing whitespace ignored)
  contains    {"type": "contains", "value": "30 days"}
  regex       {"type": "regex", "pattern": "[0-9]+ days"}
              Python re.search finds a match anywhere in the output
  json_field  {"type": "json_field", "path": "items.0.sku", "value": "A1"}
              output parses as JSON and the field at path equals value

exact, contains and regex accept "ignore_case": true. contains and regex
accept "negate": true, which passes when the text is absent.

The command sees EVAL_CASE_ID and EVAL_TRIAL in its environment. A non-zero
exit or a timeout is recorded as status "error", not as a failed grade.

Results file (--out): one JSON object per line. Line 1 describes the run.
Every later line is one trial: case_id, trial, status (pass, fail or error),
detail, the captured output, and case_sha, a fingerprint of the case's input
and graders. gate.py refuses to compare two files whose fingerprints differ.

Examples:
  run_cases.py --cases evals/cases.jsonl --trials 3 --label baseline \\
      --out .eval-gate/baseline.jsonl -- python3 call_model.py --prompt prompts/v1.txt
  run_cases.py --cases evals/cases.jsonl --trials 3 --jobs 4 --label candidate \\
      --out .eval-gate/candidate.jsonl -- node run-agent.js
  run_cases.py --cases evals/cases.jsonl --out out.jsonl --json -- ./call.sh

Exit codes: 0 = every trial ran and was graded (failed grades are results, not
errors; judging them is gate.py's job), 1 = at least one trial ended in error
(the command failed or timed out), 2 = usage or input error.
Standard library only. Makes no network calls. Writes only to --out.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

GRADER_KEYS = {
    "exact": {"type", "value", "ignore_case"},
    "contains": {"type", "value", "ignore_case", "negate"},
    "regex": {"type", "pattern", "ignore_case", "negate"},
    "json_field": {"type", "path", "value"},
}
YAML_HELP = """%s is YAML and PyYAML is not installed. Convert it to JSON first:
  - on a machine that has PyYAML:
      python3 -c 'import sys, json, yaml; json.dump(yaml.safe_load(sys.stdin), sys.stdout, indent=2)' < cases.yaml > cases.json
  - with yq version 4:
      yq -o=json '.' cases.yaml > cases.json
  - or write the cases as JSON Lines (.jsonl), one case per line."""


class InputError(Exception):
    """The cases file or a command-line value cannot be used."""


def short(value, limit=80):
    """A value as compact JSON, cut to a readable length."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= limit else text[:limit - 3] + "..."


def read_case_objects(path):
    """Return [(location, parsed object)] from a .jsonl, .json or YAML file."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError("cannot read %s: %s" % (path, exc))
    suffix = path.suffix.lower()
    if suffix in (".jsonl", ".ndjson"):
        items = []
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                items.append(("%s:%d" % (path, number), json.loads(line)))
            except ValueError as exc:
                raise InputError("%s:%d is not valid JSON: %s" % (path, number, exc))
        return items
    if suffix == ".json":
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise InputError("%s is not valid JSON: %s" % (path, exc))
    elif suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            raise InputError(YAML_HELP % path)
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise InputError("%s is not valid YAML: %s" % (path, exc))
    else:
        raise InputError("%s: unsupported cases file type; use .jsonl or .json" % path)
    if isinstance(data, dict):
        data = data.get("cases")
    if not isinstance(data, list):
        raise InputError("%s must hold a list of cases, or an object with a 'cases' list" % path)
    return [("%s: cases[%d]" % (path, index), item) for index, item in enumerate(data)]


def check_grader(grader, where):
    """Raise InputError unless grader is a well-formed grader object."""
    if not isinstance(grader, dict) or grader.get("type") not in GRADER_KEYS:
        raise InputError("%s: grader needs a 'type' that is one of %s"
                         % (where, ", ".join(sorted(GRADER_KEYS))))
    kind = grader["type"]
    unknown = sorted(set(grader) - GRADER_KEYS[kind])
    if unknown:
        raise InputError("%s: %s grader does not accept %s" % (where, kind, ", ".join(unknown)))
    for flag in ("ignore_case", "negate"):
        if flag in grader and not isinstance(grader[flag], bool):
            raise InputError("%s: '%s' must be true or false" % (where, flag))
    if kind in ("exact", "contains"):
        if not isinstance(grader.get("value"), str):
            raise InputError("%s: %s grader needs a string 'value'" % (where, kind))
        if kind == "contains" and not grader["value"]:
            raise InputError("%s: contains grader needs a non-empty 'value'" % where)
    elif kind == "regex":
        if not isinstance(grader.get("pattern"), str):
            raise InputError("%s: regex grader needs a string 'pattern'" % where)
        try:
            re.compile(grader["pattern"])
        except re.error as exc:
            raise InputError("%s: regex pattern does not compile: %s" % (where, exc))
    else:
        if "value" not in grader:
            raise InputError("%s: json_field grader needs a 'value'" % where)
        path = grader.get("path")
        parts = path if isinstance(path, list) else [path]
        if not parts or any(isinstance(p, bool) or not isinstance(p, (str, int)) or p == ""
                            for p in parts):
            raise InputError("%s: json_field grader needs a 'path' such as \"a.b.0\" "
                             "or a list of keys and indexes" % where)


def fingerprint(input_text, graders):
    """Short hash of what a case sends and how it is graded."""
    canonical = json.dumps({"input": input_text, "graders": graders}, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def load_cases(path):
    """Read and validate the cases file. Returns a list of case dicts."""
    cases = []
    seen = {}
    for where, raw in read_case_objects(path):
        if not isinstance(raw, dict):
            raise InputError("%s: a case must be a JSON object" % where)
        case_id = raw.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise InputError("%s: a case needs a non-empty string 'id'" % where)
        if case_id in seen:
            raise InputError("%s: duplicate case id %r (first used at %s)"
                             % (where, case_id, seen[case_id]))
        seen[case_id] = where
        if "input" not in raw:
            raise InputError("%s: case %r needs an 'input'" % (where, case_id))
        if ("grader" in raw) == ("graders" in raw):
            raise InputError("%s: case %r needs exactly one of 'grader' or 'graders'"
                             % (where, case_id))
        graders = raw["graders"] if "graders" in raw else [raw["grader"]]
        if not isinstance(graders, list) or not graders:
            raise InputError("%s: case %r 'graders' must be a non-empty list" % (where, case_id))
        for grader in graders:
            check_grader(grader, "%s: case %r" % (where, case_id))
        value = raw["input"]
        input_text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        cases.append({"id": case_id, "input_text": input_text, "graders": graders,
                      "sha": fingerprint(input_text, graders)})
    if not cases:
        raise InputError("%s contains no cases" % path)
    return cases


def json_equal(left, right):
    """JSON equality: true is not 1, but 1 equals 1.0."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(json_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return set(left) == set(right) and all(json_equal(left[k], right[k]) for k in left)
    return type(left) is type(right) and left == right


def lookup(data, path):
    """Follow a dotted path or a list of keys. Returns (found, value)."""
    parts = path if isinstance(path, list) else path.split(".")
    node = data
    for part in parts:
        if isinstance(node, dict) and str(part) in node:
            node = node[str(part)]
        elif isinstance(node, list) and str(part).isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return False, None
    return True, node


def grade_one(grader, output):
    """Apply one grader. Returns (passed, reason); reason is empty on a pass."""
    kind = grader["type"]
    fold = grader.get("ignore_case", False)
    negate = grader.get("negate", False)
    if kind == "exact":
        got, want = output.strip(), grader["value"].strip()
        same = got.lower() == want.lower() if fold else got == want
        return same, "" if same else "exact: output is %s, expected %s" % (short(got), short(want))
    if kind == "contains":
        value = grader["value"]
        found = value.lower() in output.lower() if fold else value in output
        if found != negate:
            return True, ""
        return False, "contains: %s %s the output" % (
            short(value), "must not appear in, but is in," if negate else "is not in")
    if kind == "regex":
        found = re.search(grader["pattern"], output, re.IGNORECASE if fold else 0) is not None
        if found != negate:
            return True, ""
        return False, "regex: pattern %s %s" % (
            short(grader["pattern"]), "must not match, but matches" if negate else "does not match")
    try:
        data = json.loads(output)
    except ValueError:
        return False, "json_field: output is not valid JSON"
    label = short(grader["path"]) if isinstance(grader["path"], list) else grader["path"]
    found, value = lookup(data, grader["path"])
    if not found:
        return False, "json_field: no field at path %s" % label
    if json_equal(value, grader["value"]):
        return True, ""
    return False, "json_field: %s is %s, expected %s" % (label, short(value), short(grader["value"]))


def grade(graders, output):
    """All graders must pass. Returns (passed, reason of the first failure)."""
    for grader in graders:
        passed, reason = grade_one(grader, output)
        if not passed:
            return False, reason
    return True, ""


def run_trial(case, trial, command, timeout, cwd, max_output):
    """Run the command once for one case and return its result record."""
    env = os.environ.copy()
    env["EVAL_CASE_ID"] = case["id"]
    env["EVAL_TRIAL"] = str(trial)
    record = {"type": "trial", "case_id": case["id"], "case_sha": case["sha"], "trial": trial}
    try:
        proc = subprocess.run(command, input=case["input_text"].encode("utf-8"),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout, env=env, cwd=cwd)
    except subprocess.TimeoutExpired:
        record.update(status="error", detail="command timed out after %gs" % timeout, output="")
        return record
    output = proc.stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        record["status"] = "error"
        record["detail"] = "command exited with code %d%s" % (
            proc.returncode, ": " + stderr[-1][:200] if stderr else "")
    else:
        passed, reason = grade(case["graders"], output)
        record["status"] = "pass" if passed else "fail"
        record["detail"] = reason
    record["output"] = output[:max_output]
    if len(output) > max_output:
        record["output_truncated"] = True
    return record


def summarise(cases, records, args):
    """Count outcomes per case. Line numbers refer to the results file."""
    per_case = {}
    for index, record in enumerate(records):
        entry = per_case.setdefault(
            record["case_id"], {"pass": 0, "fail": 0, "error": 0, "line": None, "detail": ""})
        entry[record["status"]] += 1
        if record["status"] != "pass" and entry["line"] is None:
            entry["line"] = index + 2  # line 1 of the results file is the run description
            entry["detail"] = record["detail"]
    rows = []
    for case in cases:
        entry = per_case[case["id"]]
        if entry["pass"] == args.trials:
            continue
        kind = "error" if entry["error"] else ("flaky" if entry["pass"] else "failing")
        rows.append({"case_id": case["id"], "kind": kind, "passes": entry["pass"],
                     "trials": args.trials, "detail": entry["detail"],
                     "evidence": "%s:%d" % (args.out, entry["line"])})
    total = len(records)
    passes = sum(1 for r in records if r["status"] == "pass")
    errors = sum(1 for r in records if r["status"] == "error")
    return {"cases_file": args.cases, "label": args.label, "results": args.out,
            "cases": len(cases), "trials_per_case": args.trials, "trials": total,
            "passes": passes, "fails": total - passes - errors, "errors": errors,
            "pass_rate": round(passes / float(total), 6), "not_passing": rows}


def print_summary(summary):
    print("suite: %s   cases: %d   trials per case: %d   label: %s" % (
        summary["cases_file"], summary["cases"], summary["trials_per_case"], summary["label"]))
    print("trials: %d   pass: %d   fail: %d   error: %d   pass rate: %.1f%% (%d/%d)" % (
        summary["trials"], summary["passes"], summary["fails"], summary["errors"],
        summary["pass_rate"] * 100, summary["passes"], summary["trials"]))
    titles = (("failing", "failing cases (no trial passed)"),
              ("flaky", "flaky cases (trials disagree)"),
              ("error", "cases with error trials (command failed or timed out)"))
    for kind, title in titles:
        rows = [row for row in summary["not_passing"] if row["kind"] == kind]
        if not rows:
            continue
        print("%s: %d" % (title, len(rows)))
        width = max(len(row["case_id"]) for row in rows)
        for row in rows:
            print("  %-*s  %d/%d  %s  [%s]" % (width, row["case_id"], row["passes"],
                                               row["trials"], row["detail"], row["evidence"]))
    if summary["trials_per_case"] == 1:
        print("note: 1 trial per case cannot show flaky cases. Use --trials 3 or more to see them.")
    if summary["errors"]:
        print("ERRORS: %d trial(s) produced no gradable output. These are not model failures. "
              "Fix the command and re-run before using gate.py." % summary["errors"])
    print("results: %s" % summary["results"])


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", required=True, help="cases file (.jsonl or .json)")
    parser.add_argument("--out", required=True, help="results file to write (JSON Lines)")
    parser.add_argument("--trials", type=int, default=1,
                        help="times to run each case (default 1; use 3 or more to see flakiness)")
    parser.add_argument("--jobs", type=int, default=1, help="commands to run at once (default 1)")
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="seconds before one command is killed and recorded as an error")
    parser.add_argument("--label", default="run", help="name stored in the results, e.g. baseline")
    parser.add_argument("--cwd", default=None, help="directory to run the command in")
    parser.add_argument("--max-output-chars", type=int, default=4000,
                        help="characters of each output kept in the results file (default 4000)")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="the command that makes one model call, after --")
    args = parser.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("give the command that makes one model call after --")
    if args.trials < 1 or args.jobs < 1 or args.timeout <= 0 or args.max_output_chars < 0:
        parser.error("--trials and --jobs must be at least 1, --timeout above 0, "
                     "--max-output-chars not negative")
    return args


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        cases = load_cases(Path(args.cases))
    except InputError as exc:
        print("run_cases.py: error: %s" % exc, file=sys.stderr)
        return 2
    out_path = Path(args.out)
    try:  # find an unwritable --out before any model call is paid for
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if out_path.is_dir():
            raise OSError("it is a directory")
    except OSError as exc:
        print("run_cases.py: error: cannot write %s: %s" % (args.out, exc), file=sys.stderr)
        return 2
    tasks = [(case, trial) for case in cases for trial in range(1, args.trials + 1)]

    def work(task):
        return run_trial(task[0], task[1], args.command, args.timeout, args.cwd,
                         args.max_output_chars)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            records = list(pool.map(work, tasks))  # map keeps case and trial order
    except OSError as exc:
        print("run_cases.py: error: cannot run command: %s" % exc, file=sys.stderr)
        return 2

    meta = {"type": "meta", "label": args.label, "cases_file": args.cases,
            "cases": len(cases), "trials": args.trials, "command": args.command}
    try:
        with out_path.open("w", encoding="utf-8") as handle:
            for record in [meta] + records:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as exc:
        print("run_cases.py: error: cannot write %s: %s" % (args.out, exc), file=sys.stderr)
        return 2

    summary = summarise(cases, records, args)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print_summary(summary)
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
