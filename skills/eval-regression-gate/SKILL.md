---
name: eval-regression-gate
description: Runs a provider-neutral eval suite against a prompt, model or agent change and gates the change with a pass, fail or inconclusive verdict computed from paired statistics (seeded bootstrap interval, exact test, flaky-case detection). Use when a prompt, system message, model version, tool definition or agent setting was changed and the user asks whether it got better or worse, wants to compare eval results before and after, wants a regression test or a CI gate for LLM output, or says evals, eval suite, prompt regression, pass rate dropped, did my prompt change break anything, or is this improvement real or noise.
license: MIT
compatibility: Requires Python 3.9 or later to run the bundled scripts. The scripts need no network access and no extra packages. The command you supply makes the model calls.
metadata:
  author: panhwerwaseem
  version: "0.1.0"
---

# Eval regression gate

A prompt, model or agent change is judged only when both versions have been run
on the same cases with the same number of trials, and `scripts/gate.py` has
printed PASS, FAIL or INCONCLUSIVE for that pair of result files. The verdict,
with the number of cases and trials behind it, is the result. A pass rate read
off one run is not.

Paths in this file are relative to this skill's directory.

## Hard rules

1. **Same cases, same trials, both sides.** Run the baseline and the candidate
   with the same cases file and the same `--trials`. If the cases file changed,
   re-run the baseline before comparing. `gate.py` refuses mismatched files;
   do not trim or edit a results file to get past that refusal.
2. **Never change the suite to change the verdict.** Do not delete, skip,
   reword or loosen a failing case or its grader, and do not hand-edit results.
   If a case is wrong, say why, get the user's agreement, change it in a
   separate step, and re-run both sides.
3. **Report the verdict the gate printed.** INCONCLUSIVE is not a pass and not
   a fail. Do not say "no regression", "improved" or "safe to ship" from pass
   rates alone, and do not round INCONCLUSIVE up or down.
4. **State the size with every number.** Each pass rate, difference or verdict
   you report carries the number of cases and trials per case, and the
   interval. "72% to 71%" on its own is not a result.
5. **Fix the seed and the thresholds before looking.** Keep `--seed`,
   `--margin`, `--confidence`, `--resamples` and `--min-cases` at their
   defaults, or at values the user agreed to before the candidate was run. Do
   not change them, or re-run one side, until the verdict comes out differently.
6. **Errors are not answers.** A trial with status `error` (the command
   failed or timed out) says nothing about the model. Fix the command and
   re-run. Use `--count-errors-as-failures` only when the user confirms that
   the crash is the behaviour under test.

## Workflow

### 1. Find or write the cases

Look for an existing cases file first. If there is none, write one with the
user: `references/designing-cases.md` gives the format, the four graders, and
how to choose cases. Tell the user how many model calls the run will make
(cases x trials x 2 sides) before starting.

### 2. Pin down the command that makes one call

`scripts/run_cases.py` does not call a model. It starts a command you give it,
once per case and trial, writes the case input to its stdin, and grades its
stdout. Find or write that command (a small script around the project's own
client code), and check it by hand on one input before running the suite. The
baseline and candidate commands must differ only in the thing under test.

### 3. Run the baseline

Run the current version: the base branch, the old prompt file, or the old
model ID.

```bash
python3 scripts/run_cases.py --cases <cases file> --trials 3 --label baseline \
    --out .eval-gate/baseline.jsonl -- <command for the current version>
```

Use at least 3 trials unless the output is known to be deterministic; one
trial cannot show flaky cases. Exit code 1 means some trials ended in error:
fix that before going on (rule 6).

### 4. Run the candidate

Same cases file, same `--trials`, same model settings, as close in time to the
baseline run as practical.

```bash
python3 scripts/run_cases.py --cases <cases file> --trials 3 --label candidate \
    --out .eval-gate/candidate.jsonl -- <command for the changed version>
```

### 5. Gate

```bash
python3 scripts/gate.py --baseline .eval-gate/baseline.jsonl --candidate .eval-gate/candidate.jsonl
```

It prints both pass rates, the difference with a confidence interval, an exact
test p-value, the regressed, improved and flaky cases with the result-file
lines that show them, and the verdict. Exit codes: 0 PASS, 1 FAIL, 3
INCONCLUSIVE, 2 input error. `references/reading-the-statistics.md` explains
each number and the verdict rule.

### 6. Read the changed cases

For every regressed case, open the candidate lines the gate named and read the
recorded `output`. Say what the candidate got wrong and which part of the
change caused it. A case listed as both changed and flaky is weak evidence on
its own.

### 7. Act on the verdict

| Verdict | What to do |
|---|---|
| FAIL | Fix the change, not the suite. Re-run the whole candidate side, then gate again. |
| INCONCLUSIVE, interval beyond the margin | The measurement is too noisy. If the changed cases are mostly flaky, raise `--trials` on both sides. Otherwise add cases. |
| INCONCLUSIVE, too few cases | More trials will not help. Add cases, or tell the user the smallest drop this suite can rule out (the gate prints it) and let them choose a margin. |
| INCONCLUSIVE, error trials | Fix the command, re-run that side in full. |
| PASS | Report it with the numbers. A PASS covers only the behaviour the cases test. |

Adding cases, raising trials or changing a threshold means re-running both
sides and saying so in the report.

### 8. Report

Give the user this, filled in from the gate output:

```
Change:      <what changed: prompt, model, tool or setting>
Suite:       <cases file>, <N> cases x <T> trials per side
Pass rate:   baseline <x>% (<a>/<n>), candidate <y>% (<b>/<n>)
Difference:  <d> points, <c>% interval [<low>, <high>] (paired bootstrap, seed <s>)
Test:        <test name>, p = <p> (<w> cases worse, <b> better, <u> unchanged)
Verdict:     PASS | FAIL | INCONCLUSIVE, with the gate's reason
Regressed:   <case IDs and what the candidate got wrong>
Flaky:       <case IDs whose trials disagree>
Thresholds:  margin <m>, confidence <c>, minimum cases <k>; who agreed to any non-default
Not done:    <error trials, suite changes proposed, behaviour the cases do not cover>
```

### 9. Wire it into CI if asked

`references/ci-example.md` has a workflow that runs both sides on a pull
request and maps the three verdicts to job results.

## Edge cases

- **No baseline exists** (a new prompt). One run gives a pass rate and a list
  of failing cases, nothing more. Do not claim a comparison.
- **The suite changed in the same pull request.** Run the baseline version
  against the new cases file, so both sides use it.
- **Temperature 0.** Many providers are still not fully deterministic. Run 3
  trials once to check before settling on 1.
- **Agents with tools or side effects.** The command runs cases x trials
  times per side. It must be safe to repeat: use sandboxes, fakes or dry-run
  modes for anything that writes, sends or charges.
- **A model-graded check.** The graders here are deterministic. If the command
  calls a judge model and prints its verdict, the judge's noise becomes part
  of the result. Say so in the report.
- **Old baseline results.** A baseline recorded weeks ago mixes the change
  with any provider-side drift. Prefer re-running it.
- **Results from another tool.** `gate.py` accepts any file in the format
  described in `references/designing-cases.md`. Without `case_sha` it warns
  that it could not check the cases match.
- **`.eval-gate/`** is scratch output and may contain model outputs. Do not
  commit it unless the user wants the results kept.
