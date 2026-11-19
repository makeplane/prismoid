# ai/plugins - folder rules
Scope: `backend/prismoid/ai/plugins/`. Narrows ../CLAUDE.md and the root CLAUDE.md; never contradicts them.

## Purpose
The Agent Plugins v1 reader and writer (`format.py`): it finds a bundle's components at fixed locations
(`skills/`, `mcp.json`, our namespace's bots), parses `plugin.json` and `mcp.json`, and renders a bundle's
`plugin.json` (`render_plugin_manifest`). It never parses a skill or a bot.

## Where a thing goes
- A spec rule about the bundle itself: `format.py`.
- Not here: a skill's or a bot's rules -> `ai/skills/format.py`, `ai/bots/format.py`; walking each found
  directory through its own door, refusing `stdio` servers, sending HTTP servers through the connectors
  create path and landing everything as drafts or pending connectors -> `operations/plugins.py`.

## Rules
- Follow the spec's failure rules: a bad `plugin.json` refuses the bundle (`PluginError`); a bad component
  or entry is skipped with a note; unknown namespaces are ignored unread.
- Check a URL's scheme with `urlsplit`, never a prefix literal.
- Never execute or trust anything the bundle carries - nothing it holds goes live without its owner.

## Checked by machine
- `.importlinter`: `plugins` sits under `deps` and over `verification`, so it may read the bots format's
  path constants.

## Verify
`uv run pytest tests/unit/ai/plugins -q`; an install end to end:
`uv run pytest tests/integration/bots/test_plugin_install.py -q`.
