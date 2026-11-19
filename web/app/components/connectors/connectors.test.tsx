/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The connectors vocabulary (ADR-054) - pure logic and faces, house idiom
 *  (renderToStaticMarkup + string assertions). Each describe block names a law. */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { components } from "~/lib/api/types.gen";
import { BRAND } from "./brand-icons";
import { ConnectorIcon } from "./connector-icon";
import { categoriesOf, filterCards, mergeCatalog, type CatalogEntry } from "./catalog";
import {
  POLICY_META,
  groupSummary,
  policyGroups,
  withGroupPolicy,
  withPolicy,
  type ConnectorToolOut,
} from "./policies";
import { suggestConnector } from "./suggest";
import { settleFlow, type ConnectFlow, type ConnectorRow } from "./vocabulary";

const tool = (over: Partial<ConnectorToolOut> = {}): ConnectorToolOut => ({
  name: "search",
  description: "",
  read_only: true,
  destructive: null,
  policy: "allow",
  policy_source: "default",
  ...over,
});

const row = (over: Partial<ConnectorRow> = {}): ConnectorRow => ({
  id: "11111111-1111-4111-8111-111111111111",
  name: "Gmail",
  slug: "gmail",
  server_url: "https://mcp.example.com/mcp",
  status: "live",
  auth_kind: "oauth",
  tools: [tool(), tool({ name: "send_email", read_only: false, policy: "ask" })],
  instructions: null,
  last_error: null,
  auto_attach: true,
  catalog_slug: null,
  // The backend's ONE recipe, as it would ship for this Gmail row (slug + name + tool
  // tokens, generic verbs stoplisted): the chip reads these, never re-derives them.
  suggest_words: ["email", "gmail", "send email"],
  created_at: "2026-08-30T00:00:00Z",
  ...over,
});

const entry = (over: Partial<CatalogEntry> = {}): CatalogEntry => ({
  slug: "notion",
  name: "Notion",
  vendor: "Notion",
  description: "Pages and databases.",
  category: "Productivity",
  server_url: "https://mcp.notion.com/mcp",
  auth: "oauth",
  icon: "notion",
  docs_url: "",
  auth_note: "",
  keywords: ["notion"],
  ...over,
});

describe("POLICY_META speaks every policy the wire can utter", () => {
  it("a new value is a compile error, not a silent chip", () => {
    const wire: components["schemas"]["ToolPolicy"][] = ["allow", "ask", "off"];
    expect(Object.keys(POLICY_META).sort()).toEqual([...wire].sort());
  });
});

describe("the policy grouping", () => {
  it("read-only tools group apart from write tools - unannotated goes with write, destructive never hides among reads", () => {
    const groups = policyGroups([
      tool({ name: "search", read_only: true }),
      tool({ name: "send", read_only: false, destructive: true, policy: "ask" }),
      tool({ name: "mystery", read_only: null, policy: "ask" }),
    ]);
    expect(groups.readOnly.map((r) => r.name)).toEqual(["search"]);
    expect(groups.write.map((r) => r.name)).toEqual(["send", "mystery"]);
    expect(groups.write[0].destructive).toBe(true);
  });

  it("a group summary reads mixed only when rows disagree", () => {
    const agree = policyGroups([tool(), tool({ name: "b" })]).readOnly;
    expect(groupSummary(agree)).toBe("allow");
    const disagree = policyGroups([tool(), tool({ name: "b", policy: "off" })]).readOnly;
    expect(groupSummary(disagree)).toBe("mixed");
  });

  it("one tri-state click sends every standing override plus the change - replace semantics honored", () => {
    const tools = [
      tool({ name: "a", policy: "off", policy_source: "override" }),
      tool({ name: "b", policy: "ask", policy_source: "default" }),
    ];
    expect(withPolicy(tools, "b", "allow")).toEqual({ a: "off", b: "allow" });
  });

  it("bulk-set touches every row in its group and nothing outside it", () => {
    const tools = [
      tool({ name: "a", policy: "off", policy_source: "override" }),
      tool({ name: "b" }),
      tool({ name: "c" }),
    ];
    expect(withGroupPolicy(tools, ["b", "c"], "ask")).toEqual({ a: "off", b: "ask", c: "ask" });
  });
});

describe("the suggestion grammar", () => {
  it("typing about email offers the connector whose TOOLS speak email - catalog listing not required", () => {
    const hit = suggestConnector("email this to me when done", null, [row()], []);
    expect(hit).toMatchObject({ slug: "gmail", connectorId: row().id });
  });

  it("an attached connector is never offered - absent, not disabled", () => {
    expect(suggestConnector("email this to me", null, [row()], [row().id])).toBeNull();
  });

  it("a live roster match outranks the catalog entry", () => {
    const mine = row({
      name: "Notion",
      slug: "notion",
      catalog_slug: "notion",
      tools: [tool()],
      suggest_words: ["notion"],
    });
    const hit = suggestConnector("put it in notion", [entry()], [mine], []);
    expect(hit?.connectorId).toBe(mine.id);
  });

  it("a bare generic verb never offers a credential - phrases carry the intent (found live)", () => {
    // "Create a Notion page" must not offer Gmail via create_draft's first token...
    const mailish = row({
      tools: [tool({ name: "create_draft", read_only: false })],
      suggest_words: ["draft", "gmail", "create draft"],
    });
    expect(suggestConnector("Create a summary page for the team", null, [mailish], [])).toBeNull();
    // ...but the two-token phrase stays specific and real.
    expect(
      suggestConnector("create draft replies for these threads", null, [mailish], []),
    ).toMatchObject({ slug: "gmail" });
  });

  it("no keyword hit, no chip - and word boundaries hold", () => {
    expect(suggestConnector("summarize the quarter", [entry()], [], [])).toBeNull();
    expect(
      suggestConnector(
        "a nonlinear equation",
        [entry({ slug: "linear", keywords: ["linear"] })],
        [],
        [],
      ),
    ).toBeNull();
  });

  it("a catalog-only match offers connect (no connectorId)", () => {
    const hit = suggestConnector("add it to notion please", [entry()], [], []);
    expect(hit).toMatchObject({ slug: "notion", connectorId: null });
  });
});

describe("the catalog merge", () => {
  it("a connected slug renders once - the roster row, never a duplicate catalog card", () => {
    const mine = row({ catalog_slug: "notion", slug: "notion", name: "Notion" });
    const cards = mergeCatalog([entry()], [mine]);
    expect(cards).toHaveLength(1);
    expect(cards[0].kind).toBe("mine");
  });

  it("a custom-URL connector still earns a card, and offers keep their order", () => {
    const cards = mergeCatalog([entry()], [row()]);
    expect(cards.map((c) => c.kind)).toEqual(["mine", "offered"]);
  });

  it("filter=connected shows only your rows; available only the offers; search names both", () => {
    const cards = mergeCatalog([entry()], [row()]);
    expect(
      filterCards(cards, { filter: "connected", category: null, q: "" }).map((c) => c.kind),
    ).toEqual(["mine"]);
    expect(
      filterCards(cards, { filter: "available", category: null, q: "" }).map((c) => c.kind),
    ).toEqual(["offered"]);
    expect(filterCards(cards, { filter: "all", category: null, q: "gmail" })).toHaveLength(1);
    expect(categoriesOf([entry()])).toEqual(["Productivity"]);
  });
});

describe("the flow settle decision", () => {
  const flow: ConnectFlow = { phase: "waiting", url: "u", opened: true, startedAt: 0 };
  it("an abandoned consent tab settles at the TTL - Waiting never pins forever", () => {
    expect(settleFlow(row({ status: "authorizing" }), flow, 60_000)).toBe("keep");
    expect(settleFlow(row({ status: "authorizing" }), flow, 12 * 60_000)).toBe("expired");
  });
  it("live and failed settle on status; a removed row is gone", () => {
    expect(settleFlow(row({ status: "live" }), flow, 0)).toBe("live");
    expect(settleFlow(row({ status: "needs_reauth" }), flow, 0)).toBe("failed");
    expect(settleFlow(undefined, flow, 0)).toBe("gone");
  });
});

describe("the connector's face", () => {
  it("a known slug draws its brand mark in currentColor only - never a baked color", () => {
    const html = renderToStaticMarkup(<ConnectorIcon slug="github" />);
    expect(html).toContain("<svg");
    expect(html).toContain(BRAND.github.path.slice(0, 24));
    expect(html).not.toContain('fill="#');
  });
  it("an unknown slug falls back to the hue well and the plug - never a crash", () => {
    const html = renderToStaticMarkup(<ConnectorIcon slug="mystery_service" />);
    expect(html).toContain("lucide-plug");
  });
});
