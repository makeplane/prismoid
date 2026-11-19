/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** Connectors on the web (ADR-020) - the pure vocabulary and the faces, house idiom
 *  (renderToStaticMarkup + string assertions; effects never run, so the panel's mount face is
 *  its loading face, and the row - extracted precisely for this - is tested with real props). */

import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import {
  ConnectorsControl,
  attachable,
  type ConnectorOut,
} from "~/components/composer/connectors-control";
import {
  CONNECTOR_TONE,
  connectorStatusLabel,
  connectorVerbs,
  type ConnectFlow,
} from "~/components/connectors/vocabulary";
import { ConnectorRowItem, ConnectorsPanel } from "./settings-panels";

type ConnectorStatus = ConnectorOut["status"];

const connector = (over: Partial<ConnectorOut> = {}): ConnectorOut => ({
  id: "11111111-1111-4111-8111-111111111111",
  name: "Notion",
  slug: "notion",
  server_url: "https://mcp.notion.com/mcp",
  status: "live",
  auth_kind: "oauth",
  tools: [
    {
      name: "search",
      description: "",
      read_only: true,
      destructive: null,
      policy: "allow",
      policy_source: "default",
    },
  ],
  instructions: null,
  last_error: null,
  auto_attach: true,
  catalog_slug: "notion",
  suggest_words: ["notion"],
  created_at: "2026-08-08T00:00:00Z",
  ...over,
});

// The generated union IS the vocabulary now (the serializer ships the enum): this list
// typechecks against it, and CONNECTOR_TONE being a Record over the union means a new
// backend status fails `tsc` in both places before it can drift silently.
const STATUSES: ConnectorStatus[] = [
  "live",
  "authorizing",
  "pending_auth",
  "needs_reauth",
  "error",
  "disabled",
];

describe("the status vocabulary", () => {
  it("every status wears a tone and speaks a human label - and nothing extra lingers", () => {
    expect(Object.keys(CONNECTOR_TONE).sort()).toEqual([...STATUSES].sort());
    for (const status of STATUSES) {
      expect(connectorStatusLabel(status)).not.toBe(status);
    }
  });

  it("only a live connector is attachable - the door 422s the rest", () => {
    const rows = STATUSES.map((status) => connector({ status, id: status.padEnd(36, "0") }));
    expect(attachable(rows).map((c) => c.status)).toEqual(["live"]);
  });
});

describe("the verb law", () => {
  it("Connect belongs to OAuth alone; Retry is the probe for the rest; live and disabled offer neither", () => {
    for (const status of ["pending_auth", "needs_reauth", "error", "authorizing"] as const) {
      expect(connectorVerbs({ status, auth_kind: "oauth" })).toEqual({
        connect: true,
        retry: false,
        disable: true,
        enable: false,
      });
    }
    // A no-auth or bearer row has no dance to run - the probe is its recovery verb.
    expect(connectorVerbs({ status: "error", auth_kind: "none" })).toEqual({
      connect: false,
      retry: true,
      disable: true,
      enable: false,
    });
    expect(connectorVerbs({ status: "needs_reauth", auth_kind: "bearer" })).toEqual({
      connect: false,
      retry: true,
      disable: true,
      enable: false,
    });
    // Live and disabled offer neither dance nor probe; each offers ONLY its own off/on
    // switch.
    for (const auth_kind of ["oauth", "none", "bearer"] as const) {
      expect(connectorVerbs({ status: "live", auth_kind })).toEqual({
        connect: false,
        retry: false,
        disable: true,
        enable: false,
      });
      expect(connectorVerbs({ status: "disabled", auth_kind })).toEqual({
        connect: false,
        retry: false,
        disable: false,
        enable: true,
      });
    }
  });
});

describe("the Connectors control's face", () => {
  // A closed popover renders only its trigger - the trigger IS the resting face.
  it("is PRESENT with zero connectors - the control is how connectors are discovered", () => {
    const html = renderToStaticMarkup(
      <ConnectorsControl connectors={[]} ids={[]} onChange={() => {}} />,
    );
    expect(html).toContain('aria-label="Connectors"');
    expect(html).toContain("Connectors");
  });

  it("counts nothing at rest and wears no persistent fill", () => {
    const html = renderToStaticMarkup(
      <ConnectorsControl connectors={[connector()]} ids={[]} onChange={() => {}} />,
    );
    expect(html).not.toContain("tabular-nums");
    // At rest the trigger is the bar's ghost; only a set state wears the tertiary fill.
    expect(html).not.toContain("bg-layer-3 ");
  });

  it("a set state counts itself - in the fill AND in the accessible name", () => {
    const one = connector();
    const html = renderToStaticMarkup(
      <ConnectorsControl connectors={[one]} ids={[one.id]} onChange={() => {}} />,
    );
    expect(html).toContain("tabular-nums");
    expect(html).toContain("bg-layer-3 "); // attached credentials, at a glance
    expect(html).toContain("Connectors · 1 attached"); // …and audible, not just visible
  });
});

describe("the row's faces", () => {
  const noop = () => {};
  const face = (row: ConnectorOut, flow: ConnectFlow | null = null, probing = false) =>
    renderToStaticMarkup(
      <ConnectorRowItem
        row={row}
        flow={flow}
        probing={probing}
        onConnect={noop}
        onCancel={noop}
        onRetry={noop}
        onRemove={noop}
      />,
    );

  it("a live row offers only Remove - never Connect, never Retry", () => {
    const html = face(connector());
    expect(html).toContain("Connected");
    expect(html).not.toContain(">Connect<");
    expect(html).not.toContain("Retry");
  });

  it("an error row substitutes last_error for the server URL and offers its honest verb", () => {
    const oauthErr = face(connector({ status: "error", last_error: "boom at the socket" }));
    expect(oauthErr).toContain("boom at the socket");
    expect(oauthErr).not.toContain("mcp.notion.com");
    expect(oauthErr).toContain(">Connect<");
    const bearerErr = face(
      connector({ status: "error", auth_kind: "bearer", last_error: "401 from the server" }),
    );
    expect(bearerErr).toContain("Retry");
    expect(bearerErr).not.toContain(">Connect<");
  });

  it("a waiting flow shows the watch and its exits - Cancel always, the consent link only when the popup was blocked", () => {
    const flow: ConnectFlow = {
      phase: "waiting",
      url: "https://as.example/authorize?x=1",
      opened: false,
      startedAt: 0,
    };
    const blocked = face(connector({ status: "authorizing" }), flow);
    expect(blocked).toContain("Waiting for the provider…");
    expect(blocked).toContain("Cancel");
    expect(blocked).toContain('href="https://as.example/authorize?x=1"');
    const opened = face(connector({ status: "authorizing" }), { ...flow, opened: true });
    expect(opened).toContain("Cancel");
    expect(opened).not.toContain("href="); // the tab is already open - no second door
  });

  it("a starting flow is a status, not a button - a double-click has nothing to press", () => {
    const html = face(connector({ status: "pending_auth" }), {
      phase: "starting",
      url: null,
      opened: true,
      startedAt: 0,
    });
    expect(html).toContain("Starting…");
    expect(html).not.toContain(">Connect<");
  });
});

describe("the panel's mount face", () => {
  it("opens as its anatomy: header, section, loading - never a bare paragraph", () => {
    const html = renderToStaticMarkup(
      // The panel carries a real <Link> to /connectors now - links need a router to render.
      <MemoryRouter>
        <ConnectorsPanel />
      </MemoryRouter>,
    );
    expect(html).toContain("Connectors");
    expect(html).toContain("Open connectors"); // the pointer: policies live on the page
    expect(html).not.toContain("Add connector"); // adding moved to /connectors with the catalog
    expect(html).toContain("Loading");
  });
});
