/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The Bot dial (ADR-045) - the single-select semantics, in the house idiom
 *  (renderToStaticMarkup + string assertions). What could rot silently: the head-per-name
 *  reduction, the Auto default, the direct framing in the copy, and the absent-when-empty
 *  rule (no published bot ⇒ no dial - authoring lives at /bots). */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { BotsControl, rosterHeads, type BotOut } from "~/components/composer/bots-control";

const bot = (over: Partial<BotOut> = {}): BotOut => ({
  name: "contract-reviewer",
  version: 1,
  status: "published",
  origin: "user",
  description: "Reviews contracts. Load when: an MSA. Not for: drafting.",
  job: "",
  use_when: [],
  not_for: [],
  capability: "frontier-reasoner",
  produces: "document",
  tools: ["frame.read_file"],
  skills: [],
  connectors: [],
  connector_why: {},
  golden: [],
  title: null,
  can_ask_user: true,
  eval_status: "green",
  eval_report: {},
  cases: [],
  instructions: "Review carefully.",
  instructions_sha256: "0".repeat(64),
  icon: null,
  icon_hue: null,
  pinned: false,
  work_mode: "ask",
  threads: "many",
  task_budget_usd: "2.00",
  project_id: null,
  owner_actor_id: "11111111-1111-4111-8111-111111111111",
  created_at: "2026-08-21T00:00:00Z",
  ...over,
});

describe("rosterHeads", () => {
  it("offers the published head by bare name, and an author's draft-only bot as name@draft", () => {
    const heads = rosterHeads([
      bot({ version: 1 }),
      bot({ version: 3, status: "draft" }), // a draft AHEAD of a head never outranks it
      bot({ version: 2 }),
      bot({ name: "another", status: "draft" }), // draft-only: offered, marked, @draft ref
    ]);
    expect(heads.map((h) => h.ref)).toEqual(["another@draft", "contract-reviewer"]);
    expect(heads.find((h) => h.name === "another")?.draft).toBe(true);
  });
});

describe("BotsControl", () => {
  it("renders nothing when there is no offerable bot at all", () => {
    const html = renderToStaticMarkup(
      <BotsControl bots={[bot({ status: "archived" })]} value={null} onChange={() => {}} />,
    );
    expect(html).toBe("");
  });

  it("Auto is the quiet default - no name on the pill until one is picked", () => {
    // (The popover's copy - "only that bot answers" - mounts on open, which static markup
    // never does; the accessible name is what a closed dial promises.)
    const html = renderToStaticMarkup(
      <BotsControl bots={[bot()]} value={null} onChange={() => {}} />,
    );
    expect(html).toContain("Bot: Auto");
    expect(html).not.toContain("contract-reviewer");
  });

  it("a picked bot rides the pill by name", () => {
    const html = renderToStaticMarkup(
      <BotsControl bots={[bot()]} value="contract-reviewer" onChange={() => {}} />,
    );
    expect(html).toContain("Bot: contract-reviewer");
  });
});
