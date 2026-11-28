# evals/cases

The golden cases and the case lists that live in code: `golden.jsonl` (the golden hundred, one JSON line per
case, `--suite golden`) and `golden.py` (its loader, the categories and the rules the list keeps),
`bot_golden.py` and `skill_golden.py` (the cases a published bot or skill ships with), and `lead_parity.py`
(the Lead's parity book, offline: `python -m evals.cases.lead_parity`). `golden.jsonl` lives here, not in
`datasets/`, because its rows are golden cases, not `OutcomeRow`s. See `../README.md`.
