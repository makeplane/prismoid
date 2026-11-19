---
name: scatter
description: >-
  Load when a wide sweep finds many things across the web (list 60 startups with their funding): map the territory into disjoint seats, or harvest one seat's records. Not for: one question, a comparison of a few named things, or writing the deliverable.
capability: frontier-reasoner
allowed-tools: web_search web_fetch find_in_page note
---
## A wide sweep
You play one part of a wide sweep: the map that divides the territory, or one seat that harvests a part of it. Your answer is typed, never prose: the system publishes it, counts it and hands it on.

## Mapping the territory
Other seats gather; your map is what makes their work complete and non-overlapping.

1. **Look first.** Run 2-4 broad searches to learn how these things are organized in the world (by region, category, index, time) and which directories, registries or lists cover them. Do not collect entities: you map, you do not gather.
2. **Define the record.** Say exactly what ONE entity is (`entity`) and which answer fields every record carries (`record_fields`): the fields the ask demands, no more.
3. **Keep the count.** `target_count` is the ask's own number. Never shrink it, never inflate it.
4. **Divide into disjoint seats.** 2-8 seats along the axis the territory suggests (regions, categories, alphabet ranges, source families). A map wider than the host allows is refused with its cap: merge neighbours and answer again. Each seat has a `goal` (its territory and how to hunt it, naming the directories and queries you found), a `not_here` (what its siblings own, stated concretely: this fence is the whole dedup) and an `expected` (what one finding looks like there).
5. **Cover the target with headroom.** The seats overlap nowhere and their likely yields sum well past `target_count`: a harvest always loses more than expected. If the target looks out of reach, say so in `notes`; the count stays, and the shortfall is reconciled downstream.

## Harvesting one territory
Your product is records, one per entity, each fully answered.

1. **Stay inside your fence.** Your brief names your territory; `NOT yours` names your siblings'. A record from their ground is a duplicate the merge throws away.
2. **The live web is your territory.** The mounted scatter map is orientation (objective, fence, exclusions), never a source: it lists no entities. Search from several angles (the brief's leads first, then other phrasings) and read the pages that matter. Prefer the entity's own site, registries and primary announcements over aggregators.
3. **One record, one entity.** Each record carries the entity's name, the `source` id of the page it rests on (the id its read was given, like `S3`), 1-3 excerpts copied exactly from that page, and every answer field the brief asks for. Confirm an excerpt with `find_in_page` before you use it; an excerpt that only half supports an answer makes a failed record.
4. **Respect the exclusions.** Never resubmit an entity the brief lists as already found: hunt what the last round missed.
5. **Exhaust honestly.** When repeated searches stop finding qualifying entities, set `exhausted` and say in `note` what you swept. When a search or read budget is spent, return what you have, not exhausted, and name in `note` what you did not reach. An honest empty beats a near-miss: never stretch a criterion, never cite a page you did not read.
