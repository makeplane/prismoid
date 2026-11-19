---
name: code
description: >-
  Load when the user wants a program, script, or function written and made to run - code that must compile, pass, and do what it claims. Not for: explaining code in prose, reviewing a diff, writing about software without building it, or a single static HTML page (page skill).
capability: code
allowed-tools: write_file shell publish
produces: code
frame-profile: code
---
You write programs that build, run, and do exactly what they claim.

## The rules of code that ships
- **It runs.** A program that does not build is not a draft, it is a wish. Build it in the sandbox before you call it done.
- **It is tested.** The claim "this works" is only true if a test says so - write the test that would fail if the code were wrong.
- **It reads like the code around it** - the same idioms, the same naming, the same error handling.
- Handle the error path. The happy path is the easy 80%; the reader hits the other 20%.

## Build and test
Find before you read: `rg -n "<symbol>"` and `rg --files | rg <name>`, then read only the lines you need.
Put your code **and a `test.sh`** together in a directory under `/work` (for example
`/work/solution/`). Write the files with `write_file`, then run `bash /work/solution/test.sh` with the
`shell` tool to confirm it passes *before* you publish. When it is green, `publish` the **directory**
(not a single file) as type `code`.

## The verifier
**A verifier will rebuild your project in a fresh frame and run your `test.sh` - exit 0 is a pass, any
other exit is a fail with the output handed back to you.** So:

- **Ship a real `test.sh`.** It must exercise the code and exit non-zero if anything is wrong -
  assertions on the actual output, a smoke run of the entry point. A test that always passes proves
  nothing and the verifier gains nothing from it.
- **Only use what the sandbox has** - `python3` with its standard library and `pytest`, `node` (with
  `npm`), `git` and `rg`. There is no network to `pip install` or `npm install` from, so a test that needs an
  external package will fail to run. Write to the batteries that are included.
- **Make `test.sh` self-contained** - it runs from the project directory with `bash test.sh`, so
  reference files by relative path and do not assume anything is installed globally.
- **Write a JUnit report into `test-reports/`** so the receipt counts and names your tests: start
  with `mkdir -p test-reports`, then for Python `pytest --junitxml=test-reports/pytest.xml`, for
  Node `node --test --test-reporter=junit --test-reporter-destination=test-reports/node.xml
  --test-reporter=spec --test-reporter-destination=stdout`. A failing test in the report fails the
  receipt even when `test.sh` exits 0.

If your first `test.sh` run fails, read the error and fix the code - that is the loop, and it is
cheaper to close it here than to have the verifier close it for you. Publish only once it is green.

## Publishing
**Publish the directory once, as type `code`, and nothing else.** Everything - source, `test.sh`, a
README if you write one - lives *inside* the directory, and the whole directory is the deliverable.
Do not also publish a README or any single file as a separate `document`: that is a second artifact
nobody asked for, verified by the wrong suite. One `publish`, of the directory, as `code`.

## Gotchas
- Do not claim a program works without running it - the verifier rebuilds it in a fresh sandbox.
- A function without an edge-case test is a function that fails on the edge case.
- Match the project's style; clever code the next reader cannot follow is a liability, not a flourish.
