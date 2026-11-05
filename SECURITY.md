# Security

An agent skill is instructions and code that your coding agent will follow and run. Review a skill before installing it, the same way you would review any dependency.

## What this skill does on your machine

| | |
|---|---|
| Reads | The cases file you pass; the stdout and stderr of the command you supply; the two result files given to `gate.py` |
| Writes | `run_cases.py` writes the results file passed to `--out` and creates its parent folder if missing. `gate.py` writes only to stdout and stderr |
| Executes | The command you pass to `run_cases.py` after `--`, directly, without a shell. It runs once per case and trial, and concurrently when `--jobs` is above 1. `gate.py` executes nothing |
| Network | None. The bundled scripts import no network modules. `tools/validate.py` checks this in CI. The command you supply normally calls a model API; that traffic is the command's, not the skill's |
| Dependencies | Python standard library only. PyYAML is used only if it is already installed and the cases file is YAML |
| Pre-approved tools | None. `SKILL.md` has no `allowed-tools` entry, so your agent's normal permission prompts apply |
| Hooks, MCP servers, background processes | None |

Cautions specific to this skill:

- **Cost and side effects.** `run_cases.py` runs your command cases x trials times per side. Each run is normally a paid model call. An agent or tool-using command with side effects (sending email, writing to a database, charging a card) repeats them on every run. Point it at a command that is safe to repeat.
- **Results files hold data.** The results file stores each captured output (cut at `--max-output-chars`) and the command line. If the cases or outputs contain private data, treat the results the same way, and keep them out of version control and public CI artifacts. Do not pass API keys as command arguments; use environment variables, which are not recorded.
- **Case inputs reach your model.** Review a cases file from someone else before running it, as you would any input sent to your model or agent.
- **Fixtures.** `evals/files/ticket-router/fake_model.py` is a keyword router used in place of a model by the tests and examples. It reads stdin, prints one line, and contacts nothing.

The skill instructs the agent not to edit eval cases or results to change a verdict. Any edits the agent proposes go through your agent's normal review and permission flow.

## Reporting a vulnerability

Please report security problems privately through the repository's **Security** tab ("Report a vulnerability"). If that option is not available, open an issue that says a private channel is needed, without including exploit details.

## Verifying what you install

- Install from a tagged release and read the diff between tags before updating.
- The whole skill is the `skills/eval-regression-gate/` folder: one Markdown protocol, two Python scripts, three reference files, and eval fixtures.
