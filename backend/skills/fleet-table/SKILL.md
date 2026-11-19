---
name: fleet-table
description: >-
  Load when several models answered the same task and their answers must be laid side by side: one table row per answer, agreement only where it verbatim exists, disagreement left standing. Not for: answering the task yourself, recommending, or comparing options against criteria.
capability: fast-executor
allowed-tools: read_file
---
## Laying a fleet's answers side by side
Several models each answered the SAME task independently. Their answers are your sources at `/task/inputs/`, one file per answer, and the filename names the model that wrote it (`answer-<model>.md`). Read every one first. You lay them side by side so the reader can compare and choose: you are a clerk, not another voice. You add nothing, resolve nothing and break no ties, so here the table comes first, never a conclusion of your own.

## The table
Submit ONE document with exactly this skeleton: the title, then the table, nothing in between. No preamble, no summary, no recommendation.

    # <the task, restated as a short title>

    | Model | Position (one line) | Strongest point | Caveat it flags |
    |---|---|---|---|
    | <one row per answer file> |

    ## Where they actually agree

    <only points that genuinely appear in more than one answer, or exactly: No common ground.>

    ## Where they differ

    <each real disagreement, both sides stated fairly in one line apiece, or: No disagreements.>

One row for every answer you were given, and no row for an answer you were not.

## Quotes and attribution
The verifier enforces these mechanically; one violation fails the whole table.

- **Quotation marks are only for text copied from an answer, character for character.** Every quoted span is looked up verbatim in the answers. A paraphrase in quotation marks fails; a table with no quotes passes. When in doubt, paraphrase without quotes.
- **Table cells are your paraphrases:** plain prose, never quotation marks inside the table.
- In "Where they actually agree" you may quote the overlapping words: copy them exactly and name the models that wrote them.
- Never write a recommendation, a verdict, a winner, a vote count, "on balance", "overall", or any sentence that speaks for the answers as one voice. The reader chooses.

A table that misattributes is worse than no table: readers trust it because it looks mechanical. Copy exactly, attribute precisely, and let disagreement stand.
