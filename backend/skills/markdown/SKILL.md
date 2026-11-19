---
name: markdown
description: >-
  Load when the user wants text as Markdown source - a README, a .md page, notes or a changelog in GitHub-flavoured Markdown, or other text converted to it. Not for: a Slack message (slack), an email (email), or a Word or PDF file.
capability: fast-executor
---
You write Markdown that renders the same on GitHub, in an editor's preview and in any CommonMark reader.

## The marks
- Headings are `#` marks with a space, one `#` title, then `##` sections in order; never skip a level.
- A blank line before and after every heading, list, table, quote and code block. Most broken renders are a missing blank line.
- Bold is `**text**`, italic `*text*`, inline code single backticks.
- Code blocks are fenced with three backticks and a language tag (`python`, `bash`, `json`); four spaces of indent is never a code block on purpose.
- Lists keep one marker per list (`-` for bullets, `1.` for steps); nest with two spaces for `-`, three for `1.`.
- Tables are GitHub pipe tables: a header row, a `---` separator row, one row per record, pipes inside a cell escaped as `\|`.
- Links are `[text](https://...)`; images `![alt text](path)`, always with alt text.
- Characters Markdown would read as marks (`*`, `_`, `#` at a line start, `<`) are escaped with a backslash when they are meant literally.

## The rules
- **Source, not a render.** When the user wants Markdown to paste somewhere else, give it inside one fence tagged `markdown`, so the source survives this screen's own rendering. When they want a readable answer, just answer.
- **Keep every fact.** Converting is not rewriting: every number, name, link and date in the source stays.
- **Structure follows the content.** A README opens with what the thing is and how to start, then usage, then the rest. A changelog is newest first, one heading per version.
- No raw HTML where a Markdown mark exists.

## Gotchas
- A table cell cannot hold a list or a line break; split the content or use a list instead.
- Smart quotes and long dashes pasted from documents break code; keep code plain ASCII.
