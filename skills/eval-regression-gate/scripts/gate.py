#!/usr/bin/env python3
"""Compare baseline and candidate eval results and return PASS, FAIL or INCONCLUSIVE.

Both files are results written by run_cases.py (JSON Lines, one trial per
line). They must hold the same cases, with the same inputs and graders, and
the same number of trials per case. Otherwise the comparison is refused.

What is computed, with the case as the unit of analysis:

  difference   candidate pass rate minus baseline pass rate
  interval     percentile confidence interval for the difference from a paired
               bootstrap: cases are resampled with replacement, each case
               keeping its baseline and its candidate trials together. The
               random generator is seeded, so the interval is reproducible.
  exact test   two-sided exact sign-flip (paired permutation) test of "the
               change made no difference". With one trial per case this is the
               exact McNemar test on the cases whose outcome changed. Above
               a very large number of changed cases a normal approximation
               is used instead, and the output says so.
  flaky cases  cases whose trials disagree within one results file

Verdict, with alpha = 1 - confidence:

  FAIL          p < alpha and the candidate pass rate is lower
  PASS          p < alpha and the candidate pass rate is higher, or
                no significant difference, the interval's lower end is not
                below -margin, and there are at least --min-cases cases
  INCONCLUSIVE  anything else, and whenever error trials are present
                (unless --count-errors-as-failures is given)

Examples:
  gate.py --baseline .eval-gate/baseline.jsonl --candidate .eval-gate/candidate.jsonl
  gate.py --baseline base.jsonl --candidate cand.jsonl --margin 0.10 --json
  gate.py --baseline base.jsonl --candidate cand.jsonl --count-errors-as-failures

Exit codes: 0 = PASS, 1 = FAIL, 2 = usage or input error (including results
that cannot be compared), 3 = INCONCLUSIVE.
Standard library only. Reads the two files. Writes only to stdout and stderr.
"""
import argparse
import functools
import json
import math
import random
import sys
from pathlib import Path

STATUSES = ("pass", "fail", "error")
# The exact test costs about (changed cases) x (sum of their trial differences)
# steps. Above this budget a normal approximation is used and labelled as such.
EXACT_WORK_LIMIT = 10000000


class InputError(Exception):
    """A results file cannot be read, or the two files cannot be compared."""


def load_results(path):
    """Return (meta, cases). cases maps case_id to its fingerprint and trials."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError("cannot read %s: %s" % (path, exc))
    meta, cases = {}, {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        where = "%s:%d" % (path, number)
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise InputError("%s is not valid JSON: %s" % (where, exc))
        if not isinstance(record, dict):
            raise InputError("%s: expected a JSON object" % where)
        if record.get("type") == "meta":
            meta = record
            continue
        case_id, trial, status = record.get("case_id"), record.get("trial"), record.get("status")
        if not isinstance(case_id, str) or not case_id:
            raise InputError("%s: trial record needs a string 'case_id'" % where)
        if isinstance(trial, bool) or not isinstance(trial, int):
            raise InputError("%s: trial record needs an integer 'trial'" % where)
        if status not in STATUSES:
            raise InputError("%s: 'status' must be one of %s" % (where, ", ".join(STATUSES)))
        entry = cases.setdefault(case_id, {"sha": record.get("case_sha"), "trials": {}})
        if record.get("case_sha") != entry["sha"]:
            raise InputError("%s: case %r has two different fingerprints in one file"
                             % (where, case_id))
        if trial in entry["trials"]:
            raise InputError("%s: case %r trial %d appears twice" % (where, case_id, trial))
        entry["trials"][trial] = {"status": status, "line": number,
                                  "detail": str(record.get("detail") or "")}
    if not cases:
        raise InputError("%s contains no trial records" % path)
    return meta, cases


def name_list(ids, limit=10):
    extra = len(ids) - limit
    return ", ".join(ids[:limit]) + (" and %d more" % extra if extra > 0 else "")


def comparability_problems(base, cand):
    """Reasons the two result sets cannot be compared. Empty when they can."""
    problems = []
    missing = sorted(set(base) - set(cand))
    added = sorted(set(cand) - set(base))
    if missing:
        problems.append("%d case(s) in the baseline are missing from the candidate: %s"
                        % (len(missing), name_list(missing)))
    if added:
        problems.append("%d case(s) in the candidate are not in the baseline: %s"
                        % (len(added), name_list(added)))
    edited = sorted(c for c in set(base) & set(cand)
                    if base[c]["sha"] and cand[c]["sha"] and base[c]["sha"] != cand[c]["sha"])
    if edited:
        problems.append("%d case(s) have a different input or grader on the two sides: %s"
                        % (len(edited), name_list(edited)))
    counts = sorted(set(len(entry["trials"]) for side in (base, cand) for entry in side.values()))
    if len(counts) > 1:
        problems.append("cases do not all have the same number of trials (found %s)"
                        % ", ".join(str(c) for c in counts))
    return problems


def exact_sign_flip_p(weights, observed):
    """Exact two-sided p-value of the sign-flip test.

    weights are the absolute per-case differences in passed trials (zeros
    left out) and observed is the absolute value of their signed sum. Under
    "no difference" each case's sign is + or - with equal chance, so the
    p-value is the share of the 2^m sign patterns whose sum is at least as
    far from zero as the observed one. All weights 1 gives exact McNemar.
    """
    if not weights or observed == 0:
        return 1.0
    ways = {0: 1}
    for weight in weights:
        step = {}
        for total, count in ways.items():
            step[total + weight] = step.get(total + weight, 0) + count
            step[total - weight] = step.get(total - weight, 0) + count
        ways = step
    extreme = sum(count for total, count in ways.items() if abs(total) >= observed)
    return extreme / (1 << len(weights))  # integer division done exactly, then rounded once


def normal_sign_flip_p(weights, observed):
    """Normal approximation to the same test, with a continuity correction."""
    if not weights or observed == 0:
        return 1.0
    step = functools.reduce(math.gcd, weights)  # possible sums are 2 * step apart
    z = (observed - step) / math.sqrt(sum(w * w for w in weights))
    return min(1.0, math.erfc(z / math.sqrt(2.0)))


def paired_test(deltas):
    """Return (p_value, exact) for per-case differences in passed trials."""
    weights = [abs(d) for d in deltas if d]
    observed = abs(sum(deltas))
    if len(weights) * sum(weights) <= EXACT_WORK_LIMIT:
        return exact_sign_flip_p(weights, observed), True
    return normal_sign_flip_p(weights, observed), False


def bootstrap_interval(deltas, trials, resamples, confidence, seed):
    """Percentile interval for the pass-rate difference, resampling cases.

    Only random.Random(seed).random() is used, because Python guarantees that
    sequence is stable across versions for a given seed.
    """
    rng = random.Random(seed)
    draw = rng.random
    count = len(deltas)
    sums = []
    for _ in range(resamples):
        total = 0
        for _ in range(count):
            total += deltas[int(draw() * count)]
        sums.append(total)
    sums.sort()
    tail = int(round(resamples * (1.0 - confidence) / 2.0))
    scale = float(count * trials)
    return sums[tail] / scale, sums[resamples - 1 - tail] / scale


def min_cases_for(margin, alpha):
    """Cases needed before a suite in which no case got worse can rule out a
    drop of `margin`: the smallest n with (1 - margin)^n <= alpha / 2."""
    return int(math.ceil(math.log(alpha / 2.0) / math.log(1.0 - margin)))


def smallest_drop(cases, alpha):
    """The smallest drop a suite of this size can rule out (inverse of the above)."""
    return 1.0 - (alpha / 2.0) ** (1.0 / cases)


def line_ranges(numbers):
    """[5, 6, 7, 12] -> '5-7,12'."""
    numbers = sorted(numbers)
    parts, start, prev = [], numbers[0], numbers[0]
    for number in numbers[1:] + [None]:
        if number is not None and number == prev + 1:
            prev = number
            continue
        parts.append(str(start) if start == prev else "%d-%d" % (start, prev))
        start = prev = number
    return ",".join(parts)


def side_summary(path, meta, cases, order):
    passes = sum(1 for c in order for t in cases[c]["trials"].values() if t["status"] == "pass")
    errors = sum(1 for c in order for t in cases[c]["trials"].values() if t["status"] == "error")
    trials = sum(len(cases[c]["trials"]) for c in order)
    return {"file": str(path), "label": meta.get("label"), "passes": passes, "trials": trials,
            "errors": errors, "pass_rate": round(passes / float(trials), 6)}


def case_row(case_id, base, cand, trials):
    """One per-case comparison row with the result-file lines as evidence."""
    row = {"case_id": case_id, "trials": trials}
    for name, entry in (("baseline", base[case_id]), ("candidate", cand[case_id])):
        runs = [entry["trials"][key] for key in sorted(entry["trials"])]
        row[name + "_passes"] = sum(1 for run in runs if run["status"] == "pass")
        row[name + "_lines"] = line_ranges([run["line"] for run in runs])
        row[name + "_detail"] = next((run["detail"] for run in runs if run["status"] != "pass"), "")
    row["delta"] = row["candidate_passes"] - row["baseline_passes"]
    return row


def decide(result, args, alpha, missing_fingerprints):
    """Fill in result['verdict'] and result['reasons'] from the statistics."""
    diff, p_value = result["difference"], result["test"]["p_value"]
    low, cases = result["interval"]["low"], result["cases"]
    errors = result["baseline"]["errors"] + result["candidate"]["errors"]
    p_text = "p %s" % format_p(p_value)
    reasons = []
    if errors and not args.count_errors_as_failures:
        verdict = "INCONCLUSIVE"
        reasons.append(
            "%d error trial(s) (baseline %d, candidate %d). An error is a failed command, not "
            "a model answer. Re-run those results, or pass --count-errors-as-failures if the "
            "errors are the behaviour under test."
            % (errors, result["baseline"]["errors"], result["candidate"]["errors"]))
    elif p_value < alpha and diff < 0:
        verdict = "FAIL"
        reasons.append("the candidate is worse than the baseline and the difference is "
                       "statistically significant (%s, below %g)." % (p_text, alpha))
    elif p_value < alpha and diff > 0:
        verdict = "PASS"
        reasons.append("the candidate is better than the baseline and the difference is "
                       "statistically significant (%s, below %g)." % (p_text, alpha))
    else:
        if low < -args.margin:
            reasons.append("the interval reaches %+.1f points, beyond the -%.1f point margin."
                           % (low * 100, args.margin * 100))
        if cases < result["thresholds"]["min_cases"]:
            reasons.append(
                "%d cases is fewer than the %d needed to rule out a %.1f point drop. The smallest "
                "drop a suite of %d cases can rule out is %.1f points."
                % (cases, result["thresholds"]["min_cases"], args.margin * 100, cases,
                   smallest_drop(cases, alpha) * 100))
        if reasons:
            verdict = "INCONCLUSIVE"
            reasons.insert(0, "no significant difference (%s), but a regression is not ruled out."
                           % p_text)
            reasons.append("Add cases or trials and re-run both sides. Do not report this as a "
                           "pass or as a fail.")
        else:
            verdict = "PASS"
            reasons.append("no significant difference (%s), and the interval rules out a drop "
                           "larger than the %.1f point margin." % (p_text, args.margin * 100))
    if errors and args.count_errors_as_failures:
        reasons.append("%d error trial(s) were counted as failures." % errors)
    if missing_fingerprints:
        reasons.append("warning: some results carry no case_sha, so it was not checked that "
                       "inputs and graders are the same on both sides.")
    result["verdict"], result["reasons"] = verdict, reasons


def compare(args):
    """Load both files and return the full comparison as a dict."""
    base_meta, base = load_results(args.baseline)
    cand_meta, cand = load_results(args.candidate)
    problems = comparability_problems(base, cand)
    if problems:
        raise InputError("the two result sets cannot be compared:\n  - " + "\n  - ".join(problems)
                         + "\nRun both sides on the same cases file with the same --trials.")
    order = sorted(base)
    trials = len(base[order[0]]["trials"])
    alpha = 1.0 - args.confidence
    rows = [case_row(case_id, base, cand, trials) for case_id in order]
    deltas = [row["delta"] for row in rows]
    p_value, exact = paired_test(deltas)
    low, high = bootstrap_interval(deltas, trials, args.resamples, args.confidence, args.seed)
    if not exact:
        test_name = "sign-flip test, normal approximation"
    elif trials == 1:
        test_name = "exact McNemar test"
    else:
        test_name = "exact sign-flip test"
    min_cases = args.min_cases if args.min_cases is not None else min_cases_for(args.margin, alpha)
    result = {
        "cases": len(order),
        "trials_per_case": trials,
        "baseline": side_summary(args.baseline, base_meta, base, order),
        "candidate": side_summary(args.candidate, cand_meta, cand, order),
        "difference": round(sum(deltas) / float(len(order) * trials), 6),
        "interval": {"low": round(low, 6), "high": round(high, 6), "confidence": args.confidence,
                     "method": "paired bootstrap over cases, percentile",
                     "resamples": args.resamples, "seed": args.seed},
        "test": {"name": test_name, "p_value": p_value,
                 "cases_worse": sum(1 for d in deltas if d < 0),
                 "cases_better": sum(1 for d in deltas if d > 0),
                 "cases_unchanged": sum(1 for d in deltas if d == 0)},
        "thresholds": {"margin": args.margin, "alpha": round(alpha, 6), "min_cases": min_cases},
        "regressed": sorted((r for r in rows if r["delta"] < 0),
                            key=lambda r: (r["delta"], r["case_id"])),
        "improved": sorted((r for r in rows if r["delta"] > 0),
                           key=lambda r: (-r["delta"], r["case_id"])),
        "flaky": [r for r in rows if trials > 1 and (0 < r["baseline_passes"] < trials
                                                     or 0 < r["candidate_passes"] < trials)],
    }
    missing = any(not base[c]["sha"] or not cand[c]["sha"] for c in order)
    decide(result, args, alpha, missing)
    return result


def format_p(p_value):
    return "< 0.0001" if p_value < 0.0001 else "= %.4f" % p_value


def print_rows(title, rows, limit, detail_side):
    print("%s: %d" % (title, len(rows)))
    width = max([len(row["case_id"]) for row in rows[:limit]] or [0])
    for row in rows[:limit]:
        print("  %-*s  %d/%d -> %d/%d  baseline lines %s, candidate lines %s" % (
            width, row["case_id"], row["baseline_passes"], row["trials"],
            row["candidate_passes"], row["trials"], row["baseline_lines"], row["candidate_lines"]))
        if detail_side and row[detail_side + "_detail"]:
            print("      %s: %s" % (detail_side, row[detail_side + "_detail"]))
    if len(rows) > limit:
        print("  ... and %d more (use --json or raise --max-list)" % (len(rows) - limit))


def print_report(result, limit):
    base, cand, test, interval = (result[k] for k in ("baseline", "candidate", "test", "interval"))
    print("baseline:  %s   (label: %s)" % (base["file"], base["label"] or "none"))
    print("candidate: %s   (label: %s)" % (cand["file"], cand["label"] or "none"))
    print("cases: %d   trials per case: %d   (%d trials on each side)" % (
        result["cases"], result["trials_per_case"], base["trials"]))
    print("")
    print("pass rate    baseline %.1f%% (%d/%d)   candidate %.1f%% (%d/%d)" % (
        base["pass_rate"] * 100, base["passes"], base["trials"],
        cand["pass_rate"] * 100, cand["passes"], cand["trials"]))
    print("difference   %+.1f points   %g%% interval [%+.1f, %+.1f]   "
          "(paired bootstrap over cases, %d resamples, seed %d)" % (
              result["difference"] * 100, interval["confidence"] * 100, interval["low"] * 100,
              interval["high"] * 100, interval["resamples"], interval["seed"]))
    print("test         %s, p %s   (%d cases worse, %d better, %d unchanged)" % (
        test["name"], format_p(test["p_value"]), test["cases_worse"], test["cases_better"],
        test["cases_unchanged"]))
    if base["errors"] or cand["errors"]:
        print("error trials baseline %d, candidate %d" % (base["errors"], cand["errors"]))
    print("")
    print_rows("regressed cases", result["regressed"], limit, "candidate")
    print_rows("improved cases", result["improved"], limit, "baseline")
    if result["trials_per_case"] == 1:
        print("flaky cases: not measured (1 trial per case)")
    else:
        print_rows("flaky cases (trials disagree on at least one side)", result["flaky"], limit, None)
    print("")
    print("VERDICT: %s" % result["verdict"])
    for reason in result["reasons"]:
        print("  " + reason)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--baseline", required=True, help="results file for the current version")
    parser.add_argument("--candidate", required=True, help="results file for the changed version")
    parser.add_argument("--margin", type=float, default=0.05,
                        help="largest pass-rate drop a PASS may leave possible (default 0.05)")
    parser.add_argument("--confidence", type=float, default=0.95,
                        help="confidence level of the interval; alpha = 1 - confidence (default 0.95)")
    parser.add_argument("--resamples", type=int, default=10000,
                        help="bootstrap resamples (default 10000)")
    parser.add_argument("--seed", type=int, default=0, help="bootstrap seed (default 0)")
    parser.add_argument("--min-cases", type=int, default=None,
                        help="fewest cases for a PASS without a significant improvement "
                             "(default: computed from --margin and --confidence)")
    parser.add_argument("--count-errors-as-failures", action="store_true",
                        help="judge results that contain error trials instead of "
                             "returning INCONCLUSIVE")
    parser.add_argument("--max-list", type=int, default=20,
                        help="rows shown per list in text output (default 20)")
    parser.add_argument("--json", action="store_true", help="print the full comparison as JSON")
    args = parser.parse_args(argv)
    if not 0 < args.margin < 1:
        parser.error("--margin must be between 0 and 1, e.g. 0.05 for five points")
    if not 0.5 <= args.confidence < 1:
        parser.error("--confidence must be at least 0.5 and below 1")
    if args.resamples < 1000:
        parser.error("--resamples must be at least 1000")
    if args.min_cases is not None and args.min_cases < 1:
        parser.error("--min-cases must be at least 1")
    if args.max_list < 0:
        parser.error("--max-list must not be negative")
    return args


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        result = compare(args)
    except InputError as exc:
        print("gate.py: error: %s" % exc, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_report(result, args.max_list)
    return {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 3}[result["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
