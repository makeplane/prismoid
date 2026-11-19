/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The answer's code card (ADR-053): the vendored shiki CodeBlock kit, composed for rendered
// markdown. Header wears the language left and copy right (2s check latch, the consensus
// anatomy); the body scrolls, never wraps; no line numbers. This file is the ONLY kit door
// to `@prismoid/blocks/code-block` and is loaded via React.lazy - an eager import would ship shiki's
// ~170KB core to every route through the kit chunk (measured against the Aug-23 build).
import {
  CodeBlock,
  CodeBlockActions,
  CodeBlockContainer,
  CodeBlockCopyButton,
  CodeBlockHeader,
  CodeBlockTitle,
} from "@prismoid/blocks/code-block";
import { bundledLanguages, type BundledLanguage } from "shiki";

/** The shiki grammar to load: unknown tags fall to "text" BEFORE createHighlighter sees
 *  them - it rejects unknown languages outright (found by the plan's pressure test).
 *  hasOwn, not `in`: a fence tagged "constructor" would otherwise pass on an
 *  Object.prototype key and reach the highlighter as a grammar. */
function grammarFor(lang: string): BundledLanguage {
  return lang && Object.hasOwn(bundledLanguages, lang)
    ? (lang as BundledLanguage)
    : ("text" as BundledLanguage);
}

export function AnswerCodeBlock({
  code,
  language,
  streaming = false,
}: {
  code: string;
  language: string;
  streaming?: boolean;
}) {
  const label = language || "text";
  if (streaming) {
    // The open-fence variant: frame + plain body, no per-delta tokenize (the whole-code
    // token cache would miss on every frame), no copy (half a snippet is not a snippet).
    // Metrics match CodeBlockBody (p-4 text-body-sm-regular) so the settle upgrade never reflows.
    return (
      <CodeBlockContainer language={label} className="my-3 rounded-lg">
        <CodeBlockHeader>
          <CodeBlockTitle>{label}</CodeBlockTitle>
        </CodeBlockHeader>
        <pre className="m-0 overflow-x-auto p-4 font-mono text-body-sm-regular">{code}</pre>
      </CodeBlockContainer>
    );
  }
  return (
    <CodeBlock code={code} language={grammarFor(language)} className="my-3 rounded-lg">
      <CodeBlockHeader>
        <CodeBlockTitle>{label}</CodeBlockTitle>
        <CodeBlockActions>
          <CodeBlockCopyButton className="size-7" aria-label="Copy code" />
        </CodeBlockActions>
      </CodeBlockHeader>
    </CodeBlock>
  );
}

export default AnswerCodeBlock;
