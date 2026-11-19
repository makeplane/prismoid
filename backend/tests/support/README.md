# tests/support

Shared test code: plain modules a test imports. Nothing here is collected as a test, and nothing here
imports a test module or `tests.conftest`; `conftest.py` holds fixtures and hooks only and imports from here.

- Top level: small helpers more than one test file needs. `paths.py` (the fixed folders, so a test's own
  depth never decides a path), `markers.py` (shared pytest markers), `cast.py` (the people and workspaces a
  test acts as), `dispatchers.py` (dispatch doubles), `samples.py` (sample values), `databases.py` (where the
  suite's databases come from, read by `conftest.py` before any `prismoid` import), and the rest, each named by
  what it builds.
- `fakes/`: in-memory stand-ins for a dependency the code under test talks to (a frame, a CDP peer, a relay).
- `rigs/`: the shared setup of one family of test files (`test_device_chrome*` share `chrome_rig.py`).

Where a helper goes: used by one test file, it stays in that file; by two or more, it moves here, defined
once. A fake for a dependency goes in `fakes/`; the setup of one family of tests goes in `rigs/`.

All tiers and how to run them: [tests/README.md](../README.md).
