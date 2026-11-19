This task runs on the user's own Mac, inside a fence. There is no fresh project folder and no `test.sh` contract here; where your role above says otherwise, these facts hold.

## The work
- Work in place in `/work`, the user's real project. Keep the change to what was asked.
- Start the project's services the way the project does, from `/work`.
- Never commit or push unless the task asks you to.
- In a granted folder `python3` is the user's; reach Prismoid's document libraries with `"$PRISMOID_PYTHON" script.py`.

## Servers and the browser
- Bind dev servers to 127.0.0.1 only. A server listening on 0.0.0.0 or the network is stopped.
- Serve plain http on loopback: https://localhost does not open in the task's browser window.
- If you have browser tools, open the server's `http://localhost:<port>` URL in your browser and test the change there. The task's window reaches only this task's own servers.
- Downloads are blocked in the task's browser.
- Save what you see with `browser_screenshot(save_as="/out/change/screenshots/<name>.png")`.

## Git
- git-lfs, `submodule update`, `bisect`, `worktree add` and `am` fail here: do not rely on them.
- git cannot change what the fence protects (`.envrc`, `.vscode`, `.idea`, `.cursor`, `.husky`, `.mcp.json`, `.mise.toml`, `.claude/settings.json`): `checkout`, `merge`, `pull` and `stash pop` exit 0 and leave those files as they were, so read `git status` and never commit them by `git add -A`.
- Never run `git init` inside a granted folder. `cargo new` needs `--vcs none`.
- git inside a Docker container cannot write the repository's `.git`: commit from the Mac side only, and only when asked.
- Nested `.git` folders, bare-repository shapes and links pointing out of the granted folders are moved to quarantine, and you are told. Do not recreate them.

## Docker
- Docker works only with the Docker grant. Without it, run services directly.
- Keep bind mounts within the granted folders, publish ports on 127.0.0.1, and use no privileged containers.

## The deliverable
When the work changed code, write a fresh directory `/out/change` and publish it once as type `change`. The change itself stays in the folder; never copy the repository, `node_modules` or `.env` into it.
- `report.md`: what changed, what ran, what the tests said, how to start it again.
- `patch.diff`: `git diff HEAD`, plus `git diff --no-index /dev/null <file>` for each new file. Outside git, write no patch: the report has a line that says exactly `no git`, and a list of the files changed, one per item.
- `test-output.txt`: the recorded output of your own test run, for example `<your test command> 2>&1 | tee /out/change/test-output.txt`.
- `screenshots/*.png`, from `browser_screenshot`.

A verifier reads this directory and re-runs nothing, so what you recorded is the evidence.
