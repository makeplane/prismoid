# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Seatbelt fence's lists: data, in one place, grown from live runs.

`~` is the real user's home, expanded when a profile is built (`seatbelt.profile_for`), never the
sandboxed command's `$HOME`. The Prismoid home is a setting, so `profile_for` adds it to `READ_DENY`.
"""

from __future__ import annotations

from typing import Final

# Secrets and other apps' private data: never read or written, even inside a granted folder. A
# symlinked file directly inside one of these folders has its target denied too.
READ_DENY: Final[tuple[str, ...]] = (
    # SSH keys, cloud and GitHub credentials, GPG keys.
    "~/.ssh",
    "~/.aws",
    "~/.config/gh",
    "~/.gnupg",
    # Cloud and cluster credentials.
    "~/.kube",
    "~/.config/gcloud",
    "~/.azure",
    "~/.docker/config.json",
    # Registry and git credentials, tokens in rc files.
    "~/.netrc",
    "~/.npmrc",
    "~/.pypirc",
    "~/.git-credentials",
    "~/.config/git/credentials",
    "~/.cargo/credentials",
    "~/.cargo/credentials.toml",
    "~/.pgpass",
    "~/.vault-token",
    "~/.config/rclone",
    "~/.terraform.d",
    "~/.m2/settings.xml",
    # Other agents' keys and sessions.
    "~/.claude",
    "~/.codex",
    # Prismoid's own: the model keys in `config`, the CLI's session in `credentials`.
    "~/.prismoid",
    # Password managers.
    "~/.password-store",
    "~/.config/op",
    # Shell and REPL histories (pasted tokens, typed passwords).
    "~/.zsh_history",
    "~/.bash_history",
    "~/.local/share/fish/fish_history",
    "~/.python_history",
    "~/.node_repl_history",
    "~/.psql_history",
    # The login keychain.
    "~/Library/Keychains",
    # The user's browser profiles (cookies, saved passwords); Prismoid's lives in the Prismoid home.
    "~/Library/Application Support/Google/Chrome",
    "~/Library/Application Support/Google/Chrome Beta",
    "~/Library/Application Support/Google/Chrome Dev",
    "~/Library/Application Support/Google/Chrome Canary",
    "~/Library/Application Support/Chromium",
    "~/Library/Application Support/BraveSoftware",
    "~/Library/Application Support/Microsoft Edge",
    "~/Library/Application Support/Microsoft Edge Beta",
    "~/Library/Application Support/Microsoft Edge Dev",
    "~/Library/Application Support/Microsoft Edge Canary",
    "~/Library/Application Support/Arc",
    "~/Library/Application Support/Vivaldi",
    "~/Library/Application Support/com.operasoftware.Opera",
    "~/Library/Application Support/com.operasoftware.OperaGX",
    "~/Library/Application Support/Firefox",
    "~/Library/Safari",
    "~/Library/Containers/com.apple.Safari",
    "~/Library/Cookies",
    # Mail and Messages.
    "~/Library/Mail",
    "~/Library/Messages",
)

# The same, for folders known only by a pattern: `~/` is the home, the rest a regex over one name.
READ_DENY_PATTERNS: Final[tuple[str, ...]] = (
    # 1Password's group containers (`2BUA8C4S2C.com.1password`, the AgileBits ones).
    r"~/Library/Group Containers/[^/]*(1[Pp]assword|agilebits)[^/]*",
)

# Each frame caches in its own folder under here (`seatbelt.cache_root`), so one task cannot plant
# what another runs; B3 deletes it when the frame is destroyed (`chmod -R u+w` first: Go makes its
# module cache read-only). The user's own caches (`~/.npm`, `~/.cache`, `~/.cargo`, ...) are
# read-only: their tools run what is in them, outside the sandbox.
CACHE_ROOT: Final = "~/Library/Caches/Prismoid"

# Set on every command, so toolchains cache in the frame's cache folder (values are subfolders).
CACHE_ENV: Final[dict[str, str]] = {
    # npm, pnpm, yarn, corepack.
    "npm_config_cache": "npm",
    "PNPM_HOME": "pnpm",
    "npm_config_store_dir": "pnpm-store",
    "YARN_CACHE_FOLDER": "yarn",
    "COREPACK_HOME": "corepack",
    # uv (cache, managed Pythons, tools and their shims) and pip.
    "UV_CACHE_DIR": "uv",
    "UV_PYTHON_INSTALL_DIR": "uv-python",
    "UV_PYTHON_BIN_DIR": "uv-python/bin",
    "UV_TOOL_DIR": "uv-tools",
    "UV_TOOL_BIN_DIR": "uv-tools/bin",
    "PIP_CACHE_DIR": "pip",
    # Cargo's registry, git deps and installs.
    "CARGO_HOME": "cargo",
    # Go modules and build cache.
    "GOPATH": "go",
    "GOMODCACHE": "go/pkg/mod",
    "GOCACHE": "go-build",
    # Browsers Playwright downloads, pre-commit's hook environments, Homebrew downloads.
    "PLAYWRIGHT_BROWSERS_PATH": "ms-playwright",
    "PRE_COMMIT_HOME": "pre-commit",
    "HOMEBREW_CACHE": "homebrew",
    # matplotlib's font cache (`~/.matplotlib` is read-only here). No system command imports it.
    "MPLCONFIGDIR": "matplotlib",
    # Everything else that follows the XDG cache convention.
    "XDG_CACHE_HOME": "xdg",
}

# Paths that run code later, outside the sandbox: never written, wherever they are. Regexes over
# the real path; each also matches its folder itself, so a planted folder cannot be renamed in.
PROTECT_WRITE: Final[tuple[str, ...]] = (
    # direnv runs `.envrc` on `cd`.
    r"/\.envrc$",
    # Editor tasks and settings run commands when the folder is opened.
    r"/\.vscode$",
    r"/\.vscode/(tasks|settings)\.json$",
    r"/\.idea(/|$)",
    r"/\.cursor(/|$)",
    # Agents and tools configured per project run what these name.
    r"/\.claude(/|$)",
    r"/\.mcp\.json$",
    r"/\.mise\.toml$",
    # Git hooks by another road: `core.hooksPath` points here.
    r"/\.husky(/|$)",
    # Shell rc files, anywhere.
    r"/\.(zshrc|zshenv|zprofile|zlogin|zlogout|bashrc|bash_profile|bash_login|bash_logout|profile)$",
)

# In the home, what runs code at login, in a shell, an editor or git: never written, even when a
# write root holds it. A symlinked entry has its target protected too.
HOME_PROTECT_WRITE: Final[tuple[str, ...]] = (
    # Shell rc files and the shell config folders.
    "~/.zshrc",
    "~/.zshenv",
    "~/.zprofile",
    "~/.zlogin",
    "~/.zlogout",
    "~/.bashrc",
    "~/.bash_profile",
    "~/.bash_login",
    "~/.bash_logout",
    "~/.profile",
    "~/.config/fish",
    "~/.config/zsh",
    "~/.oh-my-zsh",
    # Launch agents and daemons run at login.
    "~/Library/LaunchAgents",
    "~/Library/LaunchDaemons",
    # Editor user settings and tasks.
    "~/Library/Application Support/Code/User",
    "~/Library/Application Support/Cursor/User",
    # Git's global config (hooksPath, fsmonitor, aliases).
    "~/.gitconfig",
    "~/.config/git",
    # Folders on the user's PATH, and cargo's, plus the env script rc files source.
    "~/.local/bin",
    "~/bin",
    "~/.cargo/bin",
    "~/.cargo/env",
    "~/.cargo/env.fish",
    "~/.cargo/config",
    "~/.cargo/config.toml",
    # SSH: authorized_keys, config (ProxyCommand), rc.
    "~/.ssh",
)

# Inside a repository that already existed in a write root (`FenceSpec.protected_git`), the only
# paths git's everyday work writes; config, hooks, info/, commondir, worktrees/... stay read-only.
# rebase-merge/, rebase-apply/ and sequencer/ stay read-only too: their todo lists run `exec` lines
# on the user's later `--continue`, outside the sandbox (so rebase is a v1 limit in such a repo).
GIT_WRITABLE_DIRS: Final[tuple[str, ...]] = ("objects", "refs", "logs")
GIT_WRITABLE_FILES: Final[tuple[str, ...]] = (  # each with its `.lock` sibling
    "index",
    "HEAD",
    "ORIG_HEAD",
    "FETCH_HEAD",
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "COMMIT_EDITMSG",
    "packed-refs",
    "packed-refs.new",
    # Merge and cherry-pick state: messages, mode, the rerere list, the auto-merge tree.
    "MERGE_MSG",
    "MERGE_MODE",
    "MERGE_RR",
    "AUTO_MERGE",
    "SQUASH_MSG",
    # gc's own lock and log.
    "gc.pid",
    "gc.log",
)
# Regexes over one name in the git dir: stash's temporary index.
GIT_WRITABLE_PATTERNS: Final[tuple[str, ...]] = (r"index\.stash\.[0-9]+(\.lock)?",)

# The device's default egress allowlist: the common package registries (a host allows its
# subdomains, `prismoid.schemas.egress.domain_allowed`); everything else asks.
REGISTRIES: Final[tuple[str, ...]] = (
    # npm.
    "registry.npmjs.org",
    # PyPI: the index and the files.
    "pypi.org",
    "files.pythonhosted.org",
    # GitHub: the site, the API, archives and release assets.
    "github.com",
    "githubusercontent.com",
    # crates.io: the index and the crates.
    "crates.io",
    # The Go module proxy and its checksum database.
    "proxy.golang.org",
    "sum.golang.org",
    # Homebrew: the formula API and the bottles on GitHub's container registry.
    "formulae.brew.sh",
    "ghcr.io",
)

# What the Prismoid Chrome may reach outside any task (its own background calls, service workers), on
# top of the live frames' lists: the proxy's floor if a task's request vetting is ever missed.
# Empty until it grows from live runs; until then such calls fail closed.
CHROME_BASE: Final[tuple[str, ...]] = ()

# The `SecurityOpt` values a container may name, matched whole: each keeps Docker's own confinement
# or tightens it. Anything else (a seccomp profile by value allows what it says) is refused.
SECURITY_OPTS: Final[frozenset[str]] = frozenset(
    {
        "no-new-privileges",
        "no-new-privileges=true",
        "no-new-privileges:true",
        "seccomp=builtin",
        "seccomp:builtin",
        "apparmor=docker-default",
        "apparmor:docker-default",
    }
)
