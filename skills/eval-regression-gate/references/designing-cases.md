# Designing eval cases

How to write the cases file that `scripts/run_cases.py` runs, what the command
must do, and the results format that `scripts/gate.py` reads.

## Contents

- [The cases file](#the-cases-file)
- [Graders](#graders)
- [The command](#the-command)
- [Choosing cases](#choosing-cases)
- [How many cases and trials](#how-many-cases-and-trials)
- [Changing the suite](#changing-the-suite)
- [The results file](#the-results-file)

## The cases file

JSON Lines, one case per line (`.jsonl`), or a JSON list of cases (`.json`,
either a bare list or an object with a `cases` list). YAML is read only when
PyYAML is already installed; otherwise the script exits with code 2 and says
how to convert the file.

```json
{"id": "refund-01", "input": "I was charged twice. Please refund one payment.", "grader": {"type": "json_field", "path": "category", "value": "refund"}}
{"id": "login-05", "input": "Please help me change my password.", "graders": [{"type": "contains", "value": "reset link", "ignore_case": true}, {"type": "contains", "value": "refund", "negate": true}]}
```

| Field | Meaning |
|---|---|
| `id` | Unique, stable name. It is how a case is matched between baseline and candidate, so never reuse an ID for a different case |
| `input` | What is written to the command's stdin. A string is sent as is. Any other JSON value (for example a list of chat messages) is sent as JSON text with sorted keys |
| `grader` | One grader object |
| `graders` | A list of grader objects that must all pass. Use `grader` or `graders`, not both |

Other fields (`notes`, `tags`, `source`) are allowed and ignored.

## Graders

All four are deterministic. The output is the command's stdout, decoded as
UTF-8.

| Type | Fields | Passes when |
|---|---|---|
| `exact` | `value` | The output equals `value` after leading and trailing whitespace is removed from both |
| `contains` | `value` | `value` appears somewhere in the output |
| `regex` | `pattern` | Python's `re.search` finds `pattern` somewhere in the output. Anchor with `^` and `$` yourself if the whole output must match |
| `json_field` | `path`, `value` | The whole output parses as JSON and the field at `path` equals `value` |

Options:

- `ignore_case: true` on `exact`, `contains` and `regex`.
- `negate: true` on `contains` and `regex`: the case passes when the text is
  absent. Use it for "must not apologise", "must not mention a competitor".
- `json_field` paths are dotted keys, with numbers as list indexes:
  `items.0.sku`. If a key itself contains a dot, give the path as a list:
  `["a.b", 0]`.
- `json_field` compares as JSON: `1` equals `1.0`, `true` does not equal `1`,
  and objects and lists must match in full.
- `json_field` fails if the output is not valid JSON on its own. Code fences
  or a sentence around the JSON count as invalid. If the product strips such
  wrappers before parsing, do the same stripping in the command.

Unknown grader types and unknown options are rejected when the file is loaded,
so a typo cannot silently turn a check off.

Prefer the strictest grader that still accepts every correct answer. `exact`
suits labels and short answers. `json_field` suits structured output.
`contains` and `regex` suit a required fact inside free text, and are easy to
make too loose: a `contains` on "refund" also passes for "we cannot refund".

## The command

The part after `--` is run once per case and trial, directly, without a shell.

- It reads the case input from stdin and prints the model's answer to stdout.
  Nothing else should go to stdout; send logs to stderr.
- It exits 0 when it got an answer. A non-zero exit or a timeout is recorded
  as status `error`, which `gate.py` keeps apart from a wrong answer. Do not
  catch API errors and print an empty answer: that turns an outage into a
  failed case.
- It sees `EVAL_CASE_ID` and `EVAL_TRIAL` in its environment, for logging or
  for a per-trial sampling seed.
- The baseline and candidate commands should differ only in the thing under
  test: the prompt file, the model ID, the tool list. Keep temperature, token
  limits and every other setting the same.
- It should call the same code path the product uses. A hand-written copy of
  the prompt in the eval script drifts from the real one.

## Choosing cases

- **Start from real inputs.** Use inputs that reached the system, with
  private data removed, before inventing any.
- **One behaviour per case.** A case that checks three things at once fails
  without saying which one broke. Use several cases, or `graders` when the
  checks belong together.
- **Cover each behaviour several times.** A single case per behaviour cannot
  tell a real break from a flaky trial. In the bundled fixture, the planted
  regression is visible because all seven refund cases fail together.
- **Include the hard and the boring.** Edge inputs, adversarial inputs, and
  the plain common ones that must keep working.
- **Keep cases that fail today.** A known failure is a measurement. It is how
  an improvement shows up later. Deleting it raises the pass rate without
  improving anything.
- **Add a case for every regression found in production.**
- **Do not tune the prompt against the same cases you gate on** without
  saying so. A prompt adjusted until these cases pass says less about unseen
  inputs. Keep some cases aside if that matters to the decision.

## How many cases and trials

Cases are the unit of evidence. `gate.py` will not return PASS for "no
regression" below a minimum number of cases, because below it even a suite in
which no case got worse cannot rule out the drop you care about. At the
default 95% confidence:

| Margin (largest drop a PASS may leave possible) | Minimum cases |
|---|---|
| 2 points | 183 |
| 3 points | 122 |
| 5 points (default) | 72 |
| 10 points | 36 |
| 15 points | 23 |
| 20 points | 17 |

The formula is in `references/reading-the-statistics.md`. A FAIL, or a PASS
for a significant improvement, can come from fewer cases when enough of them
change in one direction.

Trials measure flakiness. Use 3 or more unless the output is known to be
deterministic. More trials make each case's result steadier. They do not
replace cases: 20 cases run 50 times are still 20 cases.

## Changing the suite

`run_cases.py` stores a fingerprint of each case's input and graders in the
results. `gate.py` refuses to compare two files if a case is missing on one
side, added on one side, or has a different fingerprint.

So after any change to the cases file, re-run the baseline version against
the new file. Make suite changes on their own, with a stated reason, apart
from the prompt change they would otherwise be mixed up with.

## The results file

One JSON object per line. `gate.py` reads only these fields, so results from
another harness can be converted to this format.

```json
{"type": "meta", "label": "baseline", "cases_file": "evals/cases.jsonl", "cases": 30, "trials": 3, "command": ["python3", "call_model.py"]}
{"type": "trial", "case_id": "refund-01", "case_sha": "2a0d80fd0d8b657c", "trial": 1, "status": "pass", "detail": "", "output": "..."}
```

| Field | Required by `gate.py` | Meaning |
|---|---|---|
| `case_id` | yes | The case's `id` |
| `trial` | yes | Integer, unique within the case |
| `status` | yes | `pass`, `fail` or `error` |
| `case_sha` | no | Fingerprint of input and graders. Without it the gate warns that it could not check the cases are the same |
| `detail` | no | Why the trial did not pass. Shown next to regressed cases |
| `output` | no | The captured stdout, cut at `--max-output-chars`. For reading, not used by the gate |

The `meta` line is optional; only its `label` is shown. The recorded command
line is kept there for the record, so do not pass secrets as command
arguments.
