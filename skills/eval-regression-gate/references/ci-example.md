# CI example

A GitHub Actions workflow that runs the eval suite on both sides of a pull
request and turns the gate's verdict into a job result. Treat it as a starting
point: the paths, the secret name and the model command are placeholders for
your project. Other CI systems need the same four steps and the same exit
codes.

## What the job does

1. Checks out the pull request and the base branch side by side.
2. Runs the baseline: the base branch's code and prompt, on the pull
   request's cases file.
3. Runs the candidate: the pull request's code and prompt, on the same cases
   file with the same number of trials.
4. Runs `scripts/gate.py` and maps its exit code to the job result.

Using the pull request's cases file for both sides keeps the comparison valid
when the pull request also adds cases.

## Workflow

```yaml
name: Eval gate

on:
  pull_request:
    paths:
      - "prompts/**"
      - "evals/**"

permissions:
  contents: read

jobs:
  eval-gate:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    env:
      MODEL_API_KEY: ${{ secrets.MODEL_API_KEY }}
      SKILL: candidate/.claude/skills/eval-regression-gate
    steps:
      - uses: actions/checkout@v4
        with:
          path: candidate
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.base_ref }}
          path: baseline
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Run baseline
        run: |
          python3 "$SKILL/scripts/run_cases.py" \
            --cases candidate/evals/cases.jsonl --trials 3 --jobs 4 \
            --label baseline --out results/baseline.jsonl \
            --cwd baseline -- python3 tools/call_model.py

      - name: Run candidate
        run: |
          python3 "$SKILL/scripts/run_cases.py" \
            --cases candidate/evals/cases.jsonl --trials 3 --jobs 4 \
            --label candidate --out results/candidate.jsonl \
            --cwd candidate -- python3 tools/call_model.py

      - name: Gate
        run: |
          code=0
          python3 "$SKILL/scripts/gate.py" \
            --baseline results/baseline.jsonl \
            --candidate results/candidate.jsonl > results/gate.txt || code=$?
          cat results/gate.txt
          {
            echo '```'
            cat results/gate.txt
            echo '```'
          } >> "$GITHUB_STEP_SUMMARY"
          if [ "$code" -eq 3 ]; then
            echo "::warning::Eval gate is INCONCLUSIVE. See the job summary."
            exit 0
          fi
          exit "$code"

      - name: Keep the results
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: eval-results
          path: results/
```

`tools/call_model.py` stands for your own script: it reads one case input on
stdin, calls your model with the prompt from the checkout it runs in, and
prints the answer. `--cwd` makes the same command line use the base branch's
files for the baseline and the pull request's files for the candidate.

## Choices to make

- **INCONCLUSIVE: warn or block.** The example lets INCONCLUSIVE through with
  a warning, which suits a suite that is still small. Replace `exit 0` with
  `exit 1` to block until the suite can support a verdict. Either way the
  summary shows the verdict; do not describe a warned run as passed.
- **Run failures.** `run_cases.py` exits 1 when any trial ended in error (an
  API failure or a timeout), which stops the job before the gate. That is
  intended: re-run the job instead of gating on partial results.
- **Cost.** Every run makes cases x trials x 2 model calls. The `paths`
  filter limits the job to pull requests that touch prompts or cases. Adjust
  it to wherever your prompts, tool definitions and model settings live.
- **Rate limits.** `--jobs` sets how many calls run at once. Lower it if the
  provider throttles.
- **Thresholds.** Put `--margin`, `--confidence` and `--min-cases` in the
  workflow file if you change them, so a change to a threshold is reviewed
  like any other change. Leave `--seed` alone.

## Things to know

- **Secrets and forks.** GitHub does not pass repository secrets to workflows
  triggered by pull requests from forks, so the model calls will fail there.
  Do not switch the trigger to `pull_request_target` to get around this
  without understanding that it runs with secrets while handling code from
  the fork.
- **The results contain model outputs.** The uploaded artifact holds every
  captured output, cut at `--max-output-chars`. If the cases contain private
  data, restrict who can download artifacts or drop the upload step.
- **A stored baseline.** To halve the cost you can keep the baseline results
  from the base branch (for example as an artifact of the main branch build)
  instead of re-running them. The gate will refuse the comparison whenever
  the cases file has changed since, and an old baseline also mixes in any
  change the model provider made in between. Re-running both sides in one job
  avoids both problems.
- **Skill location.** The example assumes the skill is checked in under
  `.claude/skills/`. Point `SKILL` at wherever the skill folder lives in your
  repository.
