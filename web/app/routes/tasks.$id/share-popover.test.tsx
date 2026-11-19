/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The Share popover's body, as a pure view over what the popover fetched on open. A Project task
// says who can see it (and lets the owner mint a link); a personal task says it is private and
// offers the move that makes it shared.
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { moveTargets, ShareBody } from "./share-popover";

const noop = () => {};

const ada = { name: "Ada", email: "ada@x.co" };
const members = [
  { actor_id: "b", name: "Bob", email: "bob@x.co", role: "editor" },
  { actor_id: "c", name: "", email: "cy@x.co", role: "viewer" },
];

describe("ShareBody - a Project task", () => {
  const body = (over: Record<string, unknown> = {}) =>
    renderToStaticMarkup(
      <ShareBody
        project={{ name: "Launch", isOwner: false, owner: ada }}
        members={members}
        inviteUrl={null}
        inviting={false}
        onInvite={noop}
        projects={null}
        pending={null}
        onMove={noop}
        onConfirmMove={noop}
        onCancelMove={noop}
        {...over}
      />,
    );

  it("says who can see it, then lists the members with their roles", () => {
    const html = body();
    expect(html).toContain("Everyone in Launch can see this");
    expect(html).toContain("Bob");
    expect(html).toContain("editor");
    // a member with no display name falls back to the email, like the Project page
    expect(html).toContain("cy@x.co");
    expect(html).toContain("viewer");
  });

  it("names the owner to every member, not just to the owner", () => {
    // a member (not the owner) reads who owns the Project - by name, never "You"
    const html = body();
    expect(html).toContain("Ada");
    expect(html).toContain("owner");
    expect(html).not.toContain("You");
    // the owner's own view names them too, and no special-case "You"
    const own = body({ project: { name: "Launch", isOwner: true, owner: ada } });
    expect(own).toContain("Ada");
    expect(own).not.toContain("You");
  });

  it("a guest's read - names only, no emails - still names everyone", () => {
    const html = body({
      project: {
        name: "Launch",
        isOwner: false,
        owner: { name: "Ada", email: null },
      },
      members: [
        { actor_id: "b", name: "Bob", email: null, role: "editor" },
        { actor_id: "g", name: "Gus", email: null, role: "viewer" },
      ],
    });
    expect(html).toContain("Ada");
    expect(html).toContain("Bob");
    expect(html).toContain("Gus");
    expect(html).not.toContain("null");
  });

  it("an owner with no display name is drawn by their email", () => {
    const html = body({
      project: {
        name: "Launch",
        isOwner: false,
        owner: { name: "", email: "ada@x.co" },
      },
    });
    expect(html).toContain("ada@x.co");
  });

  it("only the Project owner is offered Invite to Project", () => {
    expect(body()).not.toContain("Invite to Project");
    expect(body({ project: { name: "Launch", isOwner: true, owner: ada } })).toContain(
      "Invite to Project",
    );
  });

  it("a made link shows its url with a copy button, and the owner is the only one who sees it", () => {
    const html = body({
      project: { name: "Launch", isOwner: true, owner: ada },
      inviteUrl: "https://app.test/join/si_abc123",
    });
    expect(html).toContain("https://app.test/join/si_abc123");
    expect(html).toContain("Copy");
    expect(html).toContain("One person can join with this link"); // single-use
  });

  it("while the members load it says so rather than showing an empty list", () => {
    expect(body({ members: null })).toContain("Loading");
  });

  it("a Project name is text, never markup", () => {
    const html = body({
      project: { name: "<b>Launch</b>", isOwner: false, owner: ada },
    });
    expect(html).not.toContain("<b>");
    expect(html).toContain("&lt;b&gt;");
  });
});

describe("ShareBody - a personal task", () => {
  const body = (over: Record<string, unknown> = {}) =>
    renderToStaticMarkup(
      <ShareBody
        project={null}
        members={null}
        inviteUrl={null}
        inviting={false}
        onInvite={noop}
        projects={[
          { id: "s1", name: "Launch" },
          { id: "s2", name: "Ops" },
        ]}
        pending={null}
        onMove={noop}
        onConfirmMove={noop}
        onCancelMove={noop}
        {...over}
      />,
    );

  it("says it is private and how to change that", () => {
    const html = body();
    expect(html).toContain("Only you can see this. Move it to a Project to work on it together.");
    expect(html).not.toContain("Everyone in");
  });

  it("offers each of your Projects as the place to move it", () => {
    const html = body();
    expect(html).toContain("Launch");
    expect(html).toContain("Ops");
    expect(html).toContain("Move to");
    expect(html).not.toContain("Invite to Project");
  });

  it("picking a Project asks first: it makes the task visible to everyone in it", () => {
    const html = body({ pending: { id: "s1", name: "Launch" } });
    expect(html).toContain("Everyone in Launch will be able to see this task");
    expect(html).toContain("Move it");
    expect(html).toContain("Cancel");
    // the list is not what is asked while a move is pending
    expect(html).not.toContain("Move to Ops");
  });

  it("no Projects yet is an honest empty line, not a blank", () => {
    expect(body({ projects: [] })).toContain("You have no Projects yet");
  });
});

describe("moveTargets - only Projects the caller can file into", () => {
  it("drops the Projects where the caller is a viewer (a move there answers 403)", () => {
    expect(
      moveTargets([
        { id: "a", name: "Launch", role: "owner" },
        { id: "b", name: "Ops", role: "editor" },
        { id: "c", name: "Board", role: "viewer" },
      ]),
    ).toEqual([
      { id: "a", name: "Launch" },
      { id: "b", name: "Ops" },
    ]);
  });
});
