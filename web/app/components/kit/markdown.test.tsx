/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The answer renderer's face, pinned as static markup (the house idiom - no DOM driver).
// The lazy code card renders its Suspense fallback here (React.lazy suspends in static
// render), and that IS a pin: a fence degrades to the CSS-styled <pre>, never raw
// backticks, never a crash. Blocks are eager components and pin fully.
import { MarkdownActionsProvider } from "./special-blocks";
import { AnswerCodeBlock } from "./answer-code-block";
import { Markdown } from "./markdown";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

const EMAIL = "```email\nTo: alice@example.com\nSubject: The plan\n\nHi **Alice**.\n```";

describe("markdown fences", () => {
  it("a code fence degrades to a styled <pre> in static render - never raw backticks", () => {
    const html = renderToStaticMarkup(<Markdown>{"```ts\nconst a = 1\n```"}</Markdown>);
    expect(html).toContain("<pre");
    expect(html).toContain("const a = 1");
    expect(html).not.toContain("```");
  });

  it("an indented/bare block keeps the plain pre path", () => {
    const html = renderToStaticMarkup(<Markdown>{"    plain block"}</Markdown>);
    expect(html).toContain("<pre");
    expect(html).toContain("plain block");
  });
});

describe("line breaks", () => {
  it("a chat answer keeps one row per line instead of running a poem together", () => {
    const html = renderToStaticMarkup(
      <Markdown breaks>{"Roses are red\nViolets are blue"}</Markdown>,
    );
    expect(html).toContain("Roses are red<br/>\nViolets are blue");
  });

  it("a streaming chat tail breaks lines the same way", () => {
    const html = renderToStaticMarkup(
      <Markdown live breaks>
        {"first row\nsecond row"}
      </Markdown>,
    );
    expect(html).toContain("first row<br/>\nsecond row");
  });

  it("a document keeps standard GFM: a hard-wrapped paragraph reflows, no <br>", () => {
    const html = renderToStaticMarkup(<Markdown>{"wrapped at eighty\ncolumns by hand"}</Markdown>);
    expect(html).toContain("wrapped at eighty\ncolumns by hand");
    expect(html).not.toContain("<br");
  });
});

describe("links and images", () => {
  it("external links leave the SPA safely; relative and mailto stay native", () => {
    const html = renderToStaticMarkup(
      <Markdown>{"[out](https://example.com) [in](/tasks) [mail](mailto:a@b.c)"}</Markdown>,
    );
    expect(html).toContain('href="https://example.com" target="_blank" rel="noopener noreferrer"');
    expect(html).toContain('href="/tasks"');
    expect(html).not.toContain('href="/tasks" target');
    expect(html).not.toContain('href="mailto:a@b.c" target');
  });

  it("a model-authored external image never auto-loads - it renders as a click-through chip", () => {
    const html = renderToStaticMarkup(
      <Markdown>{"![the shot](https://elsewhere.example/shot.png?t=1)"}</Markdown>,
    );
    expect(html).not.toContain("<img");
    expect(html).toContain("the shot");
    expect(html).toContain('target="_blank"');
  });

  it("relative images render (they are ours)", () => {
    const html = renderToStaticMarkup(<Markdown>{"![p](/media/p.png)"}</Markdown>);
    expect(html).toContain('<img src="/media/p.png"');
    expect(html).toContain('loading="lazy"');
  });

  it("a BARE relative src resolves against the base and renders - it never threw its way to a chip", () => {
    const html = renderToStaticMarkup(<Markdown>{"![c](images/x.png)"}</Markdown>);
    expect(html).toContain('<img src="images/x.png"');
  });

  it("protocol-relative and foreign-origin srcs stay external chips", () => {
    const evil = renderToStaticMarkup(<Markdown>{"![e](//evil.com/x.png)"}</Markdown>);
    expect(evil).not.toContain("<img");
    expect(evil).toContain('target="_blank"');
    const other = renderToStaticMarkup(<Markdown>{"![o](https://other.com/x.png)"}</Markdown>);
    expect(other).not.toContain("<img");
    expect(other).toContain('target="_blank"');
  });
});

describe("reference-style links while live", () => {
  it("a ref in one block resolves against its definition in another - and the definition stays invisible", () => {
    const src = "See [the spec][1] here.\n\nMore prose.\n\n[1]: https://example.com/spec";
    const html = renderToStaticMarkup(<Markdown live>{src}</Markdown>);
    expect(html).toContain('href="https://example.com/spec"');
    expect(html).toContain("the spec");
    expect(html).not.toContain("[1]:");
  });
});

describe("gfm surfaces", () => {
  it("task lists arrive with the classes the CSS keys on, and a real disabled checkbox", () => {
    const html = renderToStaticMarkup(<Markdown>{"- [x] done\n- [ ] open"}</Markdown>);
    expect(html).toContain("contains-task-list");
    expect(html).toContain('type="checkbox"');
    expect(html).toContain("disabled");
    expect(html).toContain("checked");
  });

  it("strikethrough and h4 render as their elements", () => {
    const html = renderToStaticMarkup(<Markdown>{"#### deep heading\n\n~~gone~~"}</Markdown>);
    expect(html).toContain("<h4>deep heading</h4>");
    expect(html).toContain("<del>gone</del>");
  });
});

describe("the email block", () => {
  it("renders the letter card: labeled fields, markdown body, copy - and no Use-draft without a host handler", () => {
    const html = renderToStaticMarkup(<Markdown>{EMAIL}</Markdown>);
    expect(html).toContain('data-block="email"');
    expect(html).toContain("alice@example.com");
    expect(html).toContain("The plan");
    expect(html).toContain("<strong>Alice</strong>");
    expect(html).toContain('aria-label="Copy email"');
    expect(html).not.toContain("Use this draft");
    expect(html).not.toContain("Draft 1 of");
  });

  it("offers Use-this-draft exactly when a host provides the composer handle", () => {
    const html = renderToStaticMarkup(
      <MarkdownActionsProvider value={{ onUseDraft: () => {} }}>
        <Markdown>{EMAIL}</Markdown>
      </MarkdownActionsProvider>,
    );
    expect(html).toContain("Use this draft");
  });

  it("numbers variants across one answer: Draft 1 of 2, Draft 2 of 2", () => {
    const two = `${EMAIL}\n\nor, warmer:\n\n\`\`\`email\nTo: alice@example.com\nSubject: The plan, gently\n\nHi Alice!\n\`\`\``;
    const html = renderToStaticMarkup(<Markdown>{two}</Markdown>);
    expect(html).toContain("Draft 1 of 2");
    expect(html).toContain("Draft 2 of 2");
  });

  it("a fence open at the live tail renders progressively with its actions hidden", () => {
    const html = renderToStaticMarkup(
      <Markdown live>{"```email\nTo: alice@example.com\n\nHi th"}</Markdown>,
    );
    expect(html).toContain('data-block="email"');
    expect(html).toContain("alice@example.com");
    expect(html).toContain("Hi th");
    expect(html).not.toContain("Copy email");
  });

  it("the same source SETTLED shows its actions - live is the only gate", () => {
    const html = renderToStaticMarkup(
      <Markdown>{"```email\nTo: alice@example.com\n\nHi there\n```"}</Markdown>,
    );
    expect(html).toContain('aria-label="Copy email"');
  });

  it("a fence the scan cannot track (blockquote-nested) is presumed OPEN while live - no actions", () => {
    const html = renderToStaticMarkup(
      <Markdown live>{"> ```email\n> To: alice@example.com\n>\n> Hi th"}</Markdown>,
    );
    expect(html).toContain('data-block="email"');
    expect(html).toContain("alice@example.com");
    expect(html).not.toContain("Copy email");
  });

  it("an email body quoting a fence stays a plain code block - blocks never nest", () => {
    const nested = "````email\nTo: a@b.c\n\nRun this:\n\n```sh\nls\n```\n````";
    const html = renderToStaticMarkup(<Markdown>{nested}</Markdown>);
    const cards = html.match(/data-block="email"/g) ?? [];
    expect(cards).toHaveLength(1);
    expect(html).toContain("ls");
  });
});

describe("the message, csv, and mermaid blocks", () => {
  it("message drafts wear their destination and bubble body", () => {
    const html = renderToStaticMarkup(
      <Markdown>{"```message\nChannel: #launch\n\nShip it :rocket:\n```"}</Markdown>,
    );
    expect(html).toContain('data-block="message"');
    expect(html).toContain("Message draft · #launch");
    expect(html).toContain("Ship it");
  });

  it("a present-but-empty Channel falls through to To for the destination", () => {
    const html = renderToStaticMarkup(
      <Markdown>{"```message\nChannel:\nTo: bob@example.com\n\nhi\n```"}</Markdown>,
    );
    expect(html).toContain("Message draft · bob@example.com");
  });

  it("csv renders a real table with a header row and cell values", () => {
    const html = renderToStaticMarkup(
      <Markdown>{"```csv\nname,stage\nAtlas,live\nHelios,draft\n```"}</Markdown>,
    );
    expect(html).toContain('data-block="csv"');
    expect(html).toContain("<th>name</th>");
    expect(html).toContain("<td>Helios</td>");
    expect(html).toContain("2 rows");
  });

  it("mermaid's static render is the source placeholder - the diagram is browser work", () => {
    const html = renderToStaticMarkup(<Markdown>{"```mermaid\ngraph TD; A-->B\n```"}</Markdown>);
    expect(html).toContain('data-block="mermaid"');
    expect(html).toContain("graph TD");
    expect(html).not.toContain("data-mermaid-svg");
  });
});

describe("the code card (direct - the lazy chunk's own face)", () => {
  it("settled: header wears the language and a copy button; body shows the code", () => {
    const html = renderToStaticMarkup(<AnswerCodeBlock code="print('hi')" language="python" />);
    expect(html).toContain('data-language="python"');
    expect(html).toContain(">python<");
    expect(html).toContain("print(&#x27;hi&#x27;)");
    expect(html).toContain("lucide-copy");
  });

  it("an unknown language renders instead of crashing the highlighter", () => {
    const html = renderToStaticMarkup(<AnswerCodeBlock code="???" language="not-a-language" />);
    expect(html).toContain(">not-a-language<");
    expect(html).toContain("???");
  });

  it("an Object.prototype key is not a grammar: 'constructor' resolves to text", () => {
    const html = renderToStaticMarkup(<AnswerCodeBlock code="x" language="constructor" />);
    expect(html).toContain('data-language="text"');
    expect(html).toContain(">constructor<"); // the label still wears the author's tag
  });

  it("streaming: frame + plain body, no copy (half a snippet is not a snippet)", () => {
    const html = renderToStaticMarkup(<AnswerCodeBlock code="const x" language="ts" streaming />);
    expect(html).toContain(">ts<");
    expect(html).toContain("const x");
    expect(html).not.toContain("lucide-copy");
  });
});
