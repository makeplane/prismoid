# evals/runners

A runner takes one dataset row and returns one `PrismoidTrace`; it is the only part of an eval that touches the
system under test. `run_trials` (in `__init__.py`) is the shared loop and tells infra errors from agent
failures. `summary.py` writes the run's report. See `../README.md`.
