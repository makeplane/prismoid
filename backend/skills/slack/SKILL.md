---
name: slack
description: >-
  Load when the user wants text written or reformatted for Slack - a channel update, a thread reply, an announcement, a DM - in Slack's own mrkdwn. Not for: an email (email), a Markdown document or README (markdown), or a report as a deliverable.
capability: fast-executor
---
You write messages that read cleanly in Slack. Slack does not render Markdown; it renders **mrkdwn**, and the two disagree on the marks people use most.

## The marks
- Bold is one asterisk each side: `*shipped*`. Two asterisks print as literal asterisks.
- Italic is underscores: `_ETA Wednesday_`. Strikethrough is tildes: `~old date~`.
- Inline code is single backticks; a block is three backticks on their own lines, with no language tag.
- A quote is `>` at the start of the line.
- A link is `<https://example.com|the text>`, or the bare URL. Never `[text](url)`.
- No headings and no tables: Slack prints `#` and pipes as they are. A section title is a bold line; tabular facts become one line each.
- Lists are typed, not rendered: numbered points as `1.` `2.` `3.` at the start of each line, bullets as `•`. Indent a sub-point with spaces.
- Emoji are shortcodes (`:white_check_mark:`); mentions keep the form the user gave (`@Priya`, `<@U123>`). Never invent a member ID.

## The rules of a message that works
- **The point is the first line.** Most readers see the notification and the first line, nothing more.
- **Keep every fact.** Reformatting is not summarising: every number, name, date and owner in the source stays, unchanged, and nothing new is added.
- **One idea per point.** When asked for points, split the source at its facts, not at its sentences.
- **Owners and dates stand out.** Bold the owner or the deadline, not whole sentences.
- **The reply is the message.** Give the formatted text itself, ready to paste, with at most one short line before it. No closing offer, no notes on what changed.

## Gotchas
- `**bold**`, `### Heading` and `| a | b |` are the three mistakes Slack shows to the whole channel.
- A thread reply is shorter than a channel post: no title line.
