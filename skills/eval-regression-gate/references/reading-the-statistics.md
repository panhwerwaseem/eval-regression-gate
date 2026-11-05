# Reading the statistics

What each number printed by `scripts/gate.py` means, how the verdict is
reached, and why a small suite so often ends in INCONCLUSIVE.

## Contents

- [The unit is the case](#the-unit-is-the-case)
- [Paired, not unpaired](#paired-not-unpaired)
- [The interval](#the-interval)
- [The exact test](#the-exact-test)
- [The verdict rule](#the-verdict-rule)
- [Why small suites are inconclusive](#why-small-suites-are-inconclusive)
- [Flaky cases and trials](#flaky-cases-and-trials)
- [What invalidates a result](#what-invalidates-a-result)
- [Limits of these methods](#limits-of-these-methods)

## The unit is the case

Trials of one case are repeats of the same question, so they are not
independent pieces of evidence. The gate therefore reduces every case to one
number per side, the count of its trials that passed, and does all statistics
across cases. Thirty cases run three times are thirty observations, not
ninety.

Pass rate is passed trials divided by all trials. Every case has the same
number of trials on both sides (the gate refuses anything else), so this is
also the average of the per-case pass rates. The difference is candidate
minus baseline, in percentage points.

## Paired, not unpaired

Both versions answer the same cases, so each case gives a pair of results.
Most cases come out the same on both sides and cancel. Only the cases that
changed carry information. Comparing two pass rates as if they came from
different inputs throws that away.

Example: 100 cases, one trial each, baseline 80% and candidate 76%, where 6
cases got worse and 2 got better.

- Unpaired, the standard error of the difference is
  sqrt(0.80 x 0.20 / 100 + 0.76 x 0.24 / 100), about 5.9 points.
- Paired, the per-case difference is -1 for 6 cases, +1 for 2 and 0 for 92.
  Its standard error is sqrt((0.08 - 0.04 x 0.04) / 100), 2.8 points.

The paired view is about twice as sharp here. The less the two versions
disagree, the bigger the gain. This is why the baseline and candidate must be
run on the same cases, and why a pass rate copied from last month's run of a
different suite cannot be compared with today's.

## The interval

`95% interval [-10.0, +1.0]` (the gate's output for the example above) is a
confidence interval for the difference in pass rate, from a paired bootstrap:

1. Draw as many cases as the suite has, at random with replacement. Each
   drawn case brings its baseline and its candidate result together.
2. Compute the difference in pass rate on that resample.
3. Repeat `--resamples` times (default 10000) and take the 2.5th and 97.5th
   percentiles.

Read it as the range of true differences the data are compatible with. An
interval that includes zero does not show "no change". It shows the data
cannot tell. What matters for the gate is how far below zero it reaches.

The random generator is seeded (`--seed`, default 0), so the same two files
always give the same interval. Changing the seed moves the ends slightly. Do
not search for a seed that tips a borderline result.

## The exact test

The p-value answers: if the change made no difference, so that each changed
case was as likely to move one way as the other, how often would the total
shift be at least as large as the one observed?

- **One trial per case.** This is the exact McNemar test. Only the cases
  whose outcome changed count. With `w` worse and `b` better, the p-value is
  the two-sided binomial probability of a split at least that uneven among
  `w + b` fair coin flips. Examples: 5 worse and 0 better gives 2 x (1/32) =
  0.0625. 6 worse and 0 better gives 2 x (1/64) = 0.03125.
- **Several trials per case.** The same idea, weighted: a case that went
  from 3/3 to 0/3 counts three times as much as one that went from 3/3 to
  2/3. The gate enumerates every way of flipping the direction of each
  changed case and reports the share of those whose total is at least as far
  from zero as the observed total. This is a sign-flip (paired permutation)
  test. It is exact, and with one trial per case it is identical to McNemar.

Above a very large number of changed cases the gate switches to a normal
approximation of the same test and labels the line accordingly.

How uneven the split must be before p drops below 0.05, with one trial per
case:

| Cases better | Cases worse needed |
|---|---|
| 0 | 6 |
| 1 | 8 |
| 2 | 10 |
| 3 | 12 |

So one, two or five cases flipping, with none going the other way, is never
significant at this level, whatever the pass rates look like.

## The verdict rule

With alpha = 1 - confidence (0.05 by default):

| Verdict | Condition |
|---|---|
| FAIL | p is below alpha and the candidate's pass rate is lower |
| PASS | p is below alpha and the candidate's pass rate is higher |
| PASS | p is not below alpha, the interval's lower end is not below -margin, and the suite has at least the minimum number of cases |
| INCONCLUSIVE | Everything else, and any comparison with error trials unless `--count-errors-as-failures` is given |

`--margin` (default 0.05, five points) is the largest drop that a PASS is
allowed to leave possible. It is not a tolerated regression: a statistically
significant drop fails even when it is smaller than the margin.

The exit codes are 0 for PASS, 1 for FAIL, 3 for INCONCLUSIVE, and 2 when the
files cannot be read or compared.

## Why small suites are inconclusive

A pass rate measured on few cases is a rough number. For a single pass rate
near 80%, the usual 95% margin of error, 1.96 x sqrt(0.8 x 0.2 / n), is:

| Cases | Margin of error |
|---|---|
| 50 | 11.1 points |
| 100 | 7.8 points |
| 200 | 5.5 points |
| 400 | 3.9 points |
| 1000 | 2.5 points |

Pairing helps, but it cannot create evidence that is not there. Take the best
possible outcome: no case got worse. If the change really broke a share `m`
of all inputs, the chance that a suite of `n` cases happens to contain none of
them is (1 - m)^n. The gate requires that chance to be at most alpha / 2,
the same one-sided error as the interval's lower end:

    n >= ln(alpha / 2) / ln(1 - m)

At 95% confidence that is 72 cases for a 5 point margin and 36 for a 10 point
margin. Turned around, a suite of 30 cases can rule out a drop of 11.6 points
at best, which the gate prints. `--min-cases` defaults to this number.

The bootstrap alone would not catch this. When no case changed, every
resample is identical and the interval collapses to [0, 0], which looks like
certainty on five cases as much as on five hundred. The minimum is there for
that reason.

What to do with INCONCLUSIVE:

- **Too few cases.** Add cases. More trials do not help with this.
- **Interval too wide, changed cases mostly flaky.** Raise the trials on both
  sides so each case's result is steadier.
- **Interval too wide, changed cases stable.** Some cases really got worse
  and some better. Read them. Add cases around the behaviour that got worse.
- **Or accept a wider margin**, as an explicit decision by the person who
  owns the risk, made before the next run.

## Flaky cases and trials

A case is flaky on a side when its trials there disagree (for example 2 of 3
passed). The gate lists them. One trial per case cannot show this, and the
gate says so.

A flaky case that appears under "regressed" may have moved by chance. The
exact test already allows for that: chance moves go both ways and cancel. But
when you explain a result, do not build the story on a case that went from
2/3 to 1/3.

A high share of flaky cases means the suite measures noise. Before adding
trials, check whether the graders are too strict about wording, and whether
sampling settings are the same on both sides.

## What invalidates a result

- **Re-running until it passes.** Each extra attempt is another chance of a
  lucky result. If a run must be repeated, say how many runs were made and
  report all of them.
- **Changing the thresholds or the seed after seeing the result.**
- **Editing or removing cases between the two runs.** The gate refuses this
  when the fingerprints differ, but a baseline re-run on a weakened suite is
  still a weaker suite. Report suite changes.
- **Different settings on the two sides.** Temperature, token limit, model
  ID, tool list and retrieval index must match except for the change under
  test.
- **Runs far apart in time.** A hosted model can change between runs. Run
  both sides close together when you can.
- **Comparing many candidates and keeping the best.** With ten candidates,
  one will look good by chance. Confirm the chosen one with a fresh run.
- **Error trials.** A timeout is not a wrong answer. The gate returns
  INCONCLUSIVE until they are re-run or explicitly counted.

## Limits of these methods

- A percentile bootstrap interval is approximate. With few changed cases it
  tends to be somewhat too narrow. Near the margin, treat it as rough.
- The exact test asks whether there is any difference, not how large it is.
  On a very large suite a tiny difference becomes significant. Read the
  difference and the interval alongside the p-value.
- Every case counts equally. If some cases matter more than others, a single
  pass rate hides that. Read the regressed list.
- The cases are treated as a fair sample of the inputs you care about. A PASS
  says nothing about behaviour the suite does not cover.
- Graders are pass or fail. Partial credit and scored rubrics are outside
  this gate.
