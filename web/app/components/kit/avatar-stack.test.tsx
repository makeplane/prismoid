/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Who is there, at a glance - one stack for the Project header (members) and the task
// header (who is here right now). A dimmed person is watching, not working.
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { AvatarStack } from "./avatar-stack";

describe("AvatarStack", () => {
  it("draws initials, one per person, each titled with the full name", () => {
    const html = renderToStaticMarkup(
      <AvatarStack
        people={[
          { id: "a", name: "Ada Lovelace" },
          { id: "b", name: "Bob" },
        ]}
      />,
    );
    expect(html).toContain("AL");
    expect(html).toContain(">B<");
    expect(html).toContain('title="Ada Lovelace"');
    expect(html).toContain('title="Bob"');
    expect(html).not.toContain("opacity-50");
  });

  it("a dimmed person is at reduced opacity and titled '(watching)'", () => {
    const html = renderToStaticMarkup(
      <AvatarStack people={[{ id: "c", name: "Cy", dim: true }]} />,
    );
    expect(html).toContain("opacity-50");
    expect(html).toContain('title="Cy (watching)"');
    // not only a hover title: a screen reader hears who it is and that they are watching
    expect(html).toMatch(/role="img" aria-label="Cy \(watching\)"/);
  });

  it("every avatar is named for assistive tech, the initials are decoration", () => {
    const html = renderToStaticMarkup(<AvatarStack people={[{ id: "a", name: "Ada Lovelace" }]} />);
    // role="img" makes the initials inside presentational - the name is the face's label
    expect(html).toMatch(/role="img" aria-label="Ada Lovelace"/);
  });

  it("is a labelled group", () => {
    const html = renderToStaticMarkup(<AvatarStack people={[{ id: "a", name: "Ada" }]} />);
    expect(html).toContain('role="group"');
  });

  it("past max it collapses to +N", () => {
    const people = ["Ada", "Bob", "Cy", "Di", "Ed", "Flo"].map((name) => ({
      id: name,
      name,
    }));
    const html = renderToStaticMarkup(<AvatarStack people={people} max={4} />);
    expect(html).toContain("+2");
    expect(html).not.toContain('title="Ed"');
    // one past max is still "+1", never a fifth face
    const five = renderToStaticMarkup(<AvatarStack people={people.slice(0, 5)} max={4} />);
    expect(five).toContain("+1");
    expect(five).not.toContain('title="Ed"');
  });

  it("nobody is nothing - no empty ring on the page", () => {
    expect(renderToStaticMarkup(<AvatarStack people={[]} />)).toBe("");
  });

  it("names the group for assistive tech", () => {
    const html = renderToStaticMarkup(
      <AvatarStack people={[{ id: "a", name: "Ada" }]} label="1 member" />,
    );
    expect(html).toContain('aria-label="1 member"');
  });
});
