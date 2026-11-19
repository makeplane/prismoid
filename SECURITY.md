# Security policy

Prismoid runs code that AI models write, holds model API keys and user credentials, and reaches the
internet on users' behalf, so we take security reports seriously and are grateful for them.

## Reporting a vulnerability

**Please do not open a public issue, discussion or pull request for a security problem.**

Report it privately through GitHub's private vulnerability reporting: open the repository's
**Security** tab and choose **Report a vulnerability**. Only the maintainers can see the report.

Please include:

- what an attacker can do, and what they need first (an account, a project membership, a crafted
  web page or document the agent reads, network position, ...);
- the steps to reproduce, ideally against a fresh local install (`just dev` or `prismoid up`);
- the commit you tested and the Machine driver in use (`docker`, `sandbar` or `device`);
- any logs, with secrets removed.

## What to expect

| Step | Target |
|---|---|
| Acknowledge your report | within 3 business days |
| Confirm or dispute it, with a first severity assessment | within 10 business days |
| Fix a confirmed critical or high severity issue | within 30 days |
| Fix a confirmed medium or low severity issue | in a regular release, usually within 90 days |

We will keep you updated as we work on it, agree a disclosure date with you, and credit you in the
advisory unless you prefer to stay anonymous. These are targets, not guarantees, for a project
maintained by a small team.

## Supported versions

Prismoid has no tagged releases yet. Security fixes land on the `main` branch only; run a recent
`main` to get them.

| Version | Supported |
|---|---|
| `main` | yes |
| anything older | no |

## Scope

In scope: the API, worker, CLI, web app, the `docker`, `sandbar` and `device` Machine drivers, the
egress proxy, and the deployment files in `deploy/`. Prompt injection that makes an agent cross a
boundary it should not (reach a host off its allowlist, read another user's data, run a tool it was
not granted, leak a secret) is in scope.

Out of scope: problems that need an operator to disable a documented protection, reports against a
deployment that skipped the hardening checklist in
[docs/security/threat-model.md](docs/security/threat-model.md), and the known limits listed there
(for example, the `docker` driver sharing the host kernel).

See [docs/security/threat-model.md](docs/security/threat-model.md) for the trust boundaries and
what each sandbox does and does not guarantee.
