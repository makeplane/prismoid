/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Paperclip, Search } from "lucide-react";
import { toast } from "@prismoid/blocks/toast";
import { Section } from "~/components/kit";
import { Button } from "@makeplane/propel/components/button";
import { Card, CardContent } from "@prismoid/blocks/card";
import { CodeBlock } from "@prismoid/blocks/code-block";
import { Marker, MarkerContent, MarkerIcon } from "@prismoid/blocks/marker";
import {
  PromptInput,
  PromptInputButton,
  PromptInputSubmit,
  PromptInputTextarea,
  PromptInputToolbar,
  PromptInputTools,
} from "@prismoid/blocks/prompt-input";
import { Spinner } from "@prismoid/blocks/spinner";
import { Specimen } from "./shared";

export function BlocksSection() {
  return (
    <section id="blocks">
      <Section title="Blocks - Prismoid's composites (@prismoid/blocks)">
        <div className="space-y-4">
          <Specimen
            label="Card"
            file="@prismoid/blocks/card"
            note="Propel has no card; Plane keeps it in blocks too."
          >
            <Card className="w-56" size="sm">
              <CardContent>
                <p className="text-body-sm-regular font-medium">A quiet card</p>
                <p className="text-muted-foreground text-caption-md-regular">
                  Padding from --card-spacing; never re-pad.
                </p>
              </CardContent>
            </Card>
          </Specimen>
          <Specimen label="Toast" file="@prismoid/blocks/toast">
            <Button
              variant="secondary"
              size="sm"
              stretch="auto"
              label="Fire a toast"
              onClick={() => toast.success("Saved", { description: "Rev 5 minted." })}
            />
          </Specimen>
          <Specimen label="Code block" file="@prismoid/blocks/code-block">
            <div className="w-full max-w-md">
              <CodeBlock
                code={
                  'def verdict(checks):\n    return "pass" if all(c.ok for c in checks) else "fail"'
                }
                language="python"
              />
            </div>
          </Specimen>
          <Specimen label="Markers · Spinner" file="@prismoid/blocks/marker · spinner">
            <div className="w-full max-w-md space-y-2">
              <Marker variant="separator">
                <MarkerContent>Plan</MarkerContent>
              </Marker>
              <Marker>
                <MarkerIcon>
                  <Search />
                </MarkerIcon>
                <MarkerContent>Searching the web…</MarkerContent>
              </Marker>
              <span className="text-muted-foreground flex items-center gap-2 text-caption-md-regular">
                <Spinner className="size-4" /> Working…
              </span>
            </div>
          </Specimen>
          <Specimen label="Composer (prompt-input)" file="@prismoid/blocks/prompt-input">
            <div className="w-full max-w-md">
              <PromptInput onSubmit={() => {}}>
                <PromptInputTextarea placeholder="Describe the task…" />
                <PromptInputToolbar>
                  <PromptInputTools>
                    <PromptInputButton tooltip="Attach">
                      <Paperclip />
                    </PromptInputButton>
                  </PromptInputTools>
                  <PromptInputSubmit status="ready" />
                </PromptInputToolbar>
              </PromptInput>
            </div>
          </Specimen>
        </div>
      </Section>
    </section>
  );
}
