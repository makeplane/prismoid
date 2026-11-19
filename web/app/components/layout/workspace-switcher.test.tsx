/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The workspace switcher - house idiom (static markup for the faces, plain calls for the verbs). */
import { Dialog } from "@makeplane/propel/components/dialog";
import { Menu } from "@makeplane/propel/components/menu";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "~/lib/api/client";
import { joinWithCode, switchWorkspace } from "~/lib/workspaces";
import {
  CREATE_LABEL,
  JOIN_LABEL,
  JoinCodeView,
  NO_WORKSPACE_LABEL,
  NoWorkspaceState,
  WorkspaceList,
  WorkspaceSwitcherFace,
} from "./workspace-switcher";

const workspace = (over = {}) => ({
  id: "o1",
  name: "Acme",
  slug: "acme",
  kind: "team",
  role: "owner",
  ...over,
});

describe("the switcher's face", () => {
  it("wears the active workspace's name and the caller's role", () => {
    const html = renderToStaticMarkup(<WorkspaceSwitcherFace workspace={workspace()} />);
    expect(html).toContain("Acme");
    expect(html).toContain("owner");
  });

  it("says so when every membership is gone (`me.workspace` is null), and points at the way back in", () => {
    const html = renderToStaticMarkup(<WorkspaceSwitcherFace workspace={null} />);
    expect(html).toContain(NO_WORKSPACE_LABEL);
    expect(html).toContain("Create or join one");
  });
});

describe("a workspace pane with no workspace", () => {
  it("names the switcher's two doors back in instead of asking about a workspace that is not there", () => {
    const html = renderToStaticMarkup(<NoWorkspaceState />);
    expect(html).toContain(CREATE_LABEL);
    expect(html).toContain(JOIN_LABEL);
  });
});

describe("the membership list", () => {
  it("each row wears the workspace's name and your role; the active one is checked", () => {
    // The rows render under an open Menu root without its portal, so static markup sees them.
    const row = (o: ReturnType<typeof workspace>, activeId: string) =>
      renderToStaticMarkup(
        <Menu open>
          <WorkspaceList
            workspaces={[o]}
            activeId={activeId}
            canCreate
            onSwitch={() => {}}
            onCreate={() => {}}
            onJoin={() => {}}
          />
        </Menu>,
      );
    const active = row(workspace(), "o1");
    const other = row(workspace({ id: "o2", name: "Beta", role: "guest" }), "o1");
    expect(active).toContain("Acme");
    expect(active).toContain('aria-checked="true"');
    expect(other).toContain("Beta");
    expect(other).toContain("Guest"); // the role, capitalized in the label
    expect(other).toContain('aria-checked="false"');
  });

  it("the menu offers Create workspace only when the caller may found one", () => {
    const menu = (canCreate: boolean) =>
      renderToStaticMarkup(
        <Menu open>
          <WorkspaceList
            workspaces={[workspace()]}
            activeId="o1"
            canCreate={canCreate}
            onSwitch={() => {}}
            onCreate={() => {}}
            onJoin={() => {}}
          />
        </Menu>,
      );
    expect(menu(true)).toContain(CREATE_LABEL);
    expect(menu(false)).not.toContain(CREATE_LABEL);
    expect(menu(false)).toContain(JOIN_LABEL);
  });
});

describe("switching", () => {
  it("PUTs the session's workspace, then reloads the whole page", async () => {
    const calls: string[] = [];
    const put = vi.fn(async (id: string) => void calls.push(`put:${id}`));
    const reload = vi.fn(() => void calls.push("reload"));
    await switchWorkspace("o2", { put, reload });
    expect(calls).toEqual(["put:o2", "reload"]);
  });

  it("does not reload when the switch is refused", async () => {
    const reload = vi.fn();
    await expect(
      switchWorkspace("o2", { put: async () => Promise.reject(new Error("403")), reload }),
    ).rejects.toThrow("403");
    expect(reload).not.toHaveBeenCalled();
  });
});

describe("joining with a code (the invite email's second door)", () => {
  it("the menu offers Join with a code", () => {
    expect(JOIN_LABEL).toBe("Join with a code");
  });

  it("the form takes the pasted code and shows the server's refusal in words", () => {
    const view = (error: string | null) =>
      renderToStaticMarkup(
        <Dialog open>
          <JoinCodeView
            code="ci_abc"
            error={error}
            busy={false}
            onCode={() => {}}
            onJoin={() => {}}
          />
        </Dialog>,
      );
    expect(view(null)).toContain('aria-label="Invite code"');
    expect(view(null)).toContain('value="ci_abc"');
    expect(view("This invite is for another email.")).toContain(
      "This invite is for another email.",
    );
  });

  it("accepts the trimmed code, then reloads into the workspace", async () => {
    const calls: string[] = [];
    const out = await joinWithCode("  ci_abc ", {
      accept: async (code) => void calls.push(`accept:${code}`),
      reload: () => void calls.push("reload"),
    });
    expect(out).toBeNull();
    expect(calls).toEqual(["accept:ci_abc", "reload"]);
  });

  it("a refusal is the server's words, and nothing reloads", async () => {
    const reload = vi.fn();
    const out = await joinWithCode("ci_abc", {
      accept: async () =>
        Promise.reject(new ApiError(403, "forbidden", "This invite is for another email.")),
      reload,
    });
    expect(out).toBe("This invite is for another email.");
    expect(reload).not.toHaveBeenCalled();
  });

  it("an empty paste never calls the server", async () => {
    const accept = vi.fn();
    expect(await joinWithCode("   ", { accept, reload: vi.fn() })).toContain("Paste the code");
    expect(accept).not.toHaveBeenCalled();
  });
});
