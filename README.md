# eval-regression-gate

**An agent skill that runs an eval suite before and after a prompt, model or agent change and gates the change with a computed verdict: pass, fail, or inconclusive.**

Left alone, a coding agent asked "did my prompt change break anything?" tends to run a few examples once, compare two pass rates, and call a one-point drop a regression or a two-point rise an improvement. When a case fails, it is tempted to loosen the case. This skill replaces that with a protocol and two scripts: both versions run on the same cases with the same number of trials, the comparison is paired with a seeded confidence interval and an exact test, a suite that is too small to decide is reported as inconclusive, and the cases are never edited to get a pass.

The scripts do not call any model. You supply the command that makes one model call; `run_cases.py` runs it once per case and trial and grades the output. That keeps the skill provider-neutral and the scripts free of network code.

Works with Claude Code, and with any agent that reads the open [Agent Skills](https://agentskills.io) format (Codex, Cursor, GitHub Copilot, Gemini CLI and others).

## What it looks like

The examples use the bundled fixture: 30 support-ticket cases and a small local stub that stands in for the model.

Running the baseline, three trials per case:

```console
$ python3 scripts/run_cases.py --cases evals/files/ticket-router/cases.jsonl --trials 3 --label baseline --out .eval-gate/baseline.jsonl -- python3 evals/files/ticket-router/fake_model.py --variant baseline
suite: evals/files/ticket-router/cases.jsonl   cases: 30   trials per case: 3   label: baseline
trials: 90   pass: 65   fail: 25   error: 0   pass rate: 72.2% (65/90)
failing cases (no trial passed): 7
  outage-01  0/3  json_field: category is "other", expected "outage"  [.eval-gate/baseline.jsonl:23]
  outage-02  0/3  json_field: category is "other", expected "outage"  [.eval-gate/baseline.jsonl:26]
  outage-03  0/3  json_field: category is "other", expected "outage"  [.eval-gate/baseline.jsonl:29]
  outage-04  0/3  json_field: category is "other", expected "outage"  [.eval-gate/baseline.jsonl:32]
  outage-05  0/3  json_field: category is "other", expected "outage"  [.eval-gate/baseline.jsonl:35]
  outage-06  0/3  regex: pattern "\"priority\":\\s*\"high\"" does not match  [.eval-gate/baseline.jsonl:38]
  outage-07  0/3  json_field: category is "other", expected "outage"  [.eval-gate/baseline.jsonl:41]
flaky cases (trials disagree): 3
  mixed-01  1/3  json_field: category is "billing", expected "login"  [.eval-gate/baseline.jsonl:83]
  mixed-02  2/3  json_field: category is "billing", expected "login"  [.eval-gate/baseline.jsonl:88]
  mixed-03  2/3  json_field: category is "billing", expected "login"  [.eval-gate/baseline.jsonl:89]
results: .eval-gate/baseline.jsonl
```

After running the candidate the same way (`--variant regression`, written to `.eval-gate/candidate.jsonl`), the gate finds a real regression and names the cases:

```console
$ python3 scripts/gate.py --baseline .eval-gate/baseline.jsonl --candidate .eval-gate/candidate.jsonl
baseline:  .eval-gate/baseline.jsonl   (label: baseline)
candidate: .eval-gate/candidate.jsonl   (label: candidate)
cases: 30   trials per case: 3   (90 trials on each side)

pass rate    baseline 72.2% (65/90)   candidate 48.9% (44/90)
difference   -23.3 points   95% interval [-40.0, -8.9]   (paired bootstrap over cases, 10000 resamples, seed 0)
test         exact sign-flip test, p = 0.0117   (8 cases worse, 1 better, 21 unchanged)

regressed cases: 8
  refund-01  3/3 -> 0/3  baseline lines 2-4, candidate lines 2-4
      candidate: json_field: category is "billing", expected "refund"
  refund-02  3/3 -> 0/3  baseline lines 5-7, candidate lines 5-7
      candidate: json_field: category is "billing", expected "refund"
  refund-03  3/3 -> 0/3  baseline lines 8-10, candidate lines 8-10
      candidate: json_field: category is "billing", expected "refund"
  refund-04  3/3 -> 0/3  baseline lines 11-13, candidate lines 11-13
      candidate: regex: pattern "\"priority\":\\s*\"high\"" does not match
  refund-05  3/3 -> 0/3  baseline lines 14-16, candidate lines 14-16
      candidate: contains: "refund request" is not in the output
  refund-06  3/3 -> 0/3  baseline lines 17-19, candidate lines 17-19
      candidate: json_field: category is "billing", expected "refund"
  refund-07  3/3 -> 0/3  baseline lines 20-22, candidate lines 20-22
      candidate: json_field: category is "billing", expected "refund"
  mixed-03   2/3 -> 1/3  baseline lines 89-91, candidate lines 89-91
      candidate: json_field: category is "billing", expected "login"
improved cases: 1
  mixed-02  2/3 -> 3/3  baseline lines 86-88, candidate lines 86-88
      baseline: json_field: category is "billing", expected "login"
flaky cases (trials disagree on at least one side): 3
  mixed-01  1/3 -> 1/3  baseline lines 83-85, candidate lines 83-85
  mixed-02  2/3 -> 3/3  baseline lines 86-88, candidate lines 86-88
  mixed-03  2/3 -> 1/3  baseline lines 89-91, candidate lines 89-91

VERDICT: FAIL
  the candidate is worse than the baseline and the difference is statistically significant (p = 0.0117, below 0.05).
```

A one-point drop that is only noise is not called a regression, and not called safe either:

```console
$ python3 scripts/gate.py --baseline evals/files/ticket-router/results/baseline.jsonl --candidate evals/files/ticket-router/results/candidate-noise.jsonl
baseline:  evals/files/ticket-router/results/baseline.jsonl   (label: baseline)
candidate: evals/files/ticket-router/results/candidate-noise.jsonl   (label: candidate)
cases: 30   trials per case: 3   (90 trials on each side)

pass rate    baseline 72.2% (65/90)   candidate 71.1% (64/90)
difference   -1.1 points   95% interval [-5.6, +2.2]   (paired bootstrap over cases, 10000 resamples, seed 0)
test         exact sign-flip test, p = 1.0000   (2 cases worse, 1 better, 27 unchanged)

regressed cases: 2
  mixed-02  2/3 -> 1/3  baseline lines 86-88, candidate lines 86-88
      candidate: json_field: category is "billing", expected "login"
  mixed-03  2/3 -> 1/3  baseline lines 89-91, candidate lines 89-91
      candidate: json_field: category is "billing", expected "login"
improved cases: 1
  mixed-01  1/3 -> 2/3  baseline lines 83-85, candidate lines 83-85
      baseline: json_field: category is "billing", expected "login"
flaky cases (trials disagree on at least one side): 3
  mixed-01  1/3 -> 2/3  baseline lines 83-85, candidate lines 83-85
  mixed-02  2/3 -> 1/3  baseline lines 86-88, candidate lines 86-88
  mixed-03  2/3 -> 1/3  baseline lines 89-91, candidate lines 89-91

VERDICT: INCONCLUSIVE
  no significant difference (p = 1.0000), but a regression is not ruled out.
  the interval reaches -5.6 points, beyond the -5.0 point margin.
  30 cases is fewer than the 72 needed to rule out a 5.0 point drop. The smallest drop a suite of 30 cases can rule out is 11.6 points.
  Add cases or trials and re-run both sides. Do not report this as a pass or as a fail.
```

Removing the failing cases from one side does not produce a pass. The comparison is refused:

```console
$ grep -v '"refund-' .eval-gate/candidate.jsonl > .eval-gate/trimmed.jsonl
$ python3 scripts/gate.py --baseline .eval-gate/baseline.jsonl --candidate .eval-gate/trimmed.jsonl
gate.py: error: the two result sets cannot be compared:
  - 7 case(s) in the baseline are missing from the candidate: refund-01, refund-02, refund-03, refund-04, refund-05, refund-06, refund-07
Run both sides on the same cases file with the same --trials.
```

## Install

Pick one route. Installing through more than one gives you the skill twice.

**Claude Code (plugin)**

```
/plugin install eval-regression-gate --marketplace panhwerwaseem/eval-regression-gate
```

On Claude Code versions before 2.1.275, use two steps:

```
/plugin marketplace add panhwerwaseem/eval-regression-gate
/plugin install eval-regression-gate@panhwerwaseem-eval-regression-gate
```

**Any agent (skills CLI)**

```bash
npx skills add panhwerwaseem/eval-regression-gate
```

**Manual**

Copy the `skills/eval-regression-gate` folder into `.claude/skills/` (Claude Code) or `.agents/skills/` (cross-agent) in your project, or into the same folder under your home directory.

To pin a release, add the tag: `/plugin marketplace add panhwerwaseem/eval-regression-gate#v0.1.0` or `npx skills add panhwerwaseem/eval-regression-gate@v0.1.0`. Third-party plugins do not update automatically; run `claude plugin update eval-regression-gate@panhwerwaseem-eval-regression-gate` to update.

## Use

Describe the change and the skill loads on its own:

> I shortened the system prompt for the ticket router. Run the evals against main and tell me if anything got worse.

In Claude Code you can also call it directly with `/eval-regression-gate`.

## What is in the skill

| File | Purpose |
|---|---|
| `SKILL.md` | The protocol: six hard rules and a nine-step workflow from cases file to reported verdict |
| `scripts/run_cases.py` | Runs your command once per case and trial, feeds the case input on stdin, grades stdout with `exact`, `contains`, `regex` or `json_field` graders, and writes one result line per trial. A failed command is recorded as an error, not as a wrong answer |
| `scripts/gate.py` | Compares two result files. Reports per-case changes with the result lines as evidence, the pass-rate difference with a seeded paired bootstrap interval, an exact test p-value, and flaky cases. Returns PASS, FAIL or INCONCLUSIVE as exit code 0, 1 or 3 |
| `references/designing-cases.md` | The cases format, the graders, what the command must do, how to choose cases, and the results format |
| `references/reading-the-statistics.md` | What each number means, the verdict rule, paired versus unpaired comparison, and why small suites are inconclusive |
| `references/ci-example.md` | A GitHub Actions workflow that runs both sides on a pull request and maps the verdict to a job result |
| `evals/` | A 30-case ticket-routing suite with a local stub in place of a model, recorded results with a real regression, with noise only, and with a real improvement, plus eval cases and trigger queries |

### How the verdict is computed

The case is the unit of evidence. Trials of one case are repeats, so 30 cases run 3 times are 30 observations.

- **Interval.** Cases are resampled with replacement 10000 times, each keeping its baseline and candidate trials together, and the 2.5th and 97.5th percentiles of the pass-rate difference are reported. The generator is seeded, so the same files always give the same interval.
- **Exact test.** If the change did nothing, each changed case was as likely to move up as down. The p-value is the share of all up-or-down assignments whose total shift is at least as large as the observed one. With one trial per case this is the exact McNemar test: 6 cases worse and none better gives 2 × (1/64) = 0.03125, while 5 worse and none better gives 0.0625.
- **FAIL** when p is below 0.05 and the candidate is lower. **PASS** when p is below 0.05 and the candidate is higher, or when there is no significant difference, the interval does not reach below the margin (5 points by default), and the suite is large enough. **INCONCLUSIVE** otherwise.
- **Large enough** means: if a share *m* of inputs had broken, a suite of *n* cases would contain none of them with probability (1 − *m*)ⁿ. The gate needs that to be at most 0.025, so *n* ≥ ln(0.025) / ln(1 − *m*). That is 72 cases for a 5 point margin and 36 for a 10 point margin.

## Requirements

- Python 3.9 or later
- No packages to install. The scripts need no network access; the command you supply makes its own model calls

## What it reads, writes and contacts

- **Reads:** the cases file you pass, the stdout and stderr of the command you supply, and the two result files given to `gate.py`.
- **Writes:** `run_cases.py` writes the results file passed to `--out`, creating its folder if needed. `gate.py` writes nothing. The results file holds the captured model outputs and the command line that was run.
- **Runs:** only the command you pass after `--`, directly, without a shell, once per case and trial.
- **Contacts:** nothing. The scripts contain no network code; CI enforces this. Whatever your command contacts is up to your command.
- **Permissions:** the skill pre-approves no tools. Your agent asks before running commands, as usual.

See [SECURITY.md](SECURITY.md).

## Testing status

- The scripts have unit tests that run in CI on Python 3.9 and 3.12, together with a validator for the skill format and plugin manifests. The exact test is checked against hand-computed tables and brute-force enumeration, the bootstrap against hand-computed binomial limits and a pinned seed, and both scripts against every fixture. In the tests the model command is a local Python stub; no test calls a model.
- The eval cases in `skills/eval-regression-gate/evals/` are in the [skill-creator](https://github.com/anthropics/skills/tree/main/skills/skill-creator) format. A with-skill versus without-skill score has not been published yet; results are welcome as an issue or pull request.

## Development

```bash
python3 tools/validate.py              # skill format, manifests, no-network check
python3 -m unittest discover -s tests  # script unit tests
```

## License

[MIT](LICENSE)
