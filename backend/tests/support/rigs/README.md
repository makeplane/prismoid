# tests/support/rigs

A rig is the shared setup of one family of test files: the rows, fakes and helpers that every file of the
family needs. It is named after the family (`chrome_rig.py` serves `test_device_chrome*`), and its docstring
names the files that use it. A helper only one file needs stays in that file. `durability_drill.py` is the
scripted step the call-log drills run (`tests/drills`).

All tiers and how to run them: [tests/README.md](../../README.md).
