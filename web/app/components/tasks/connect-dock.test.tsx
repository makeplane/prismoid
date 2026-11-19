/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The connect block (ADR-054) - the in-conversation "Connect Notion" card. */

import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import type { CatalogEntry } from "~/components/connectors/catalog";
import type { ConnectorRow } from "~/components/connectors/vocabulary";
import type { GateView } from "~/lib/task-view";
import { ConnectDock, connectOffer } from "./connect-dock";

const gate = (over: Partial<GateView> = {}): GateView => ({
  gate_id: "g1",
  kind: "connect",
  title: "Connect Notion to continue?",
  reason: "To create the page there.",
  connector_slug: "notion",
  questions: [],
  ...over,
});

const entry = (over: Partial<CatalogEntry> = {}): CatalogEntry => ({
  slug: "notion",
  name: "Notion",
  vendor: "Notion",
  description: "Pages.",
  category: "Productivity",
  server_url: "https://mcp.notion.com/mcp",
  auth: "oauth",
  icon: "notion",
  docs_url: "",
  auth_note: "",
  keywords: ["notion"],
  ...over,
});

const row = (over: Partial<ConnectorRow> = {}): ConnectorRow => ({
  id: "11111111-1111-4111-8111-111111111111",
  name: "Notion",
  slug: "notion",
  server_url: "https://mcp.notion.com/mcp",
  status: "live",
  auth_kind: "oauth",
  tools: [],
  instructions: null,
  last_error: null,
  auto_attach: true,
  catalog_slug: "notion",
  suggest_words: ["notion"],
  created_at: "2026-08-30T00:00:00Z",
  ...over,
});

describe("the offer decision", () => {
  it("the user's own row outranks the catalog entry, and live means live", () => {
    const offer = connectOffer(gate(), [entry()], [row({ name: "My Notion" })]);
    expect(offer.display).toBe("My Notion");
    expect(offer.live).toBe(true);
  });

  it("catalog-only offers wear the catalog name; a pending row is not live", () => {
    expect(connectOffer(gate(), [entry()], []).display).toBe("Notion");
    expect(connectOffer(gate(), [entry()], [row({ status: "pending_auth" })]).live).toBe(false);
  });

  it("an unmatched service still names itself from the title - never a blank block", () => {
    const offer = connectOffer(
      gate({ connector_slug: "", title: "Connect Teleporter to continue?" }),
      null,
      [],
    );
    expect(offer.slug).toBe("");
    expect(offer.display).toBe("Teleporter");
  });
});

describe("the block's face", () => {
  const render = (g: GateView) =>
    renderToStaticMarkup(
      <MemoryRouter>
        <ConnectDock gate={g} />
      </MemoryRouter>,
    );

  it("is its OWN species: the kicker, the brand well, ONE Connect button - and none of the Q&A grammar", () => {
    const html = render(gate());
    expect(html).toContain("Connection needed"); // the genus marker no Q&A dock wears
    expect(html).toContain(">Notion</h3>"); // the service IS the headline
    expect(html).toContain("To create the page there.");
    expect(html).toContain("Connect Notion"); // the one real button, named
    expect(html).toContain("size-12"); // the directory's lg brand well
    expect(html).toContain("Continue without it"); // a quiet text link, not a Skip chip
    expect(html).not.toContain(">Skip<");
    expect(html).not.toContain(">Yes<");
    expect(html).not.toContain(">No<");
    expect(html).not.toContain("Submit answers");
    expect(html).not.toContain("of 2"); // no stepper - nothing questionnaire-shaped
  });

  it("an unmatched service points at the connectors page instead of a dead button", () => {
    const html = render(gate({ connector_slug: "", title: "Connect Teleporter to continue?" }));
    expect(html).toContain("Open connectors");
    expect(html).toContain('href="/connectors"');
  });
});
