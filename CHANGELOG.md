# Changelog

All notable changes to this skill are recorded here. Versions follow [Semantic Versioning](https://semver.org).

## 0.1.0 - 2026-11-06

### Added

- `SKILL.md` with the gate protocol: six hard rules and a nine-step workflow.
- `scripts/run_cases.py`: runs a user-supplied command once per case and trial, grades stdout with exact, contains, regex and JSON-field graders, and writes results as JSON Lines.
- `scripts/gate.py`: compares baseline and candidate results with a seeded paired bootstrap interval, an exact sign-flip test (exact McNemar with one trial per case) and flaky-case detection, and returns PASS, FAIL or INCONCLUSIVE.
- References for designing cases, reading the statistics, and a CI workflow example.
- A 30-case fixture suite with recorded baseline, regression, noise-only and improvement results, eval cases, and trigger queries.
- Unit tests, a format validator, and a CI workflow.
