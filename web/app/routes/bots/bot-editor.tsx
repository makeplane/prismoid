/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The bot's editable definition: the draft form that saves a new version.

import { useState } from "react";
import { toast } from "@prismoid/blocks/toast";
import { Rows, Section } from "~/components/kit";
import { Button } from "@makeplane/propel/components/button";
import { Input, InputGroup } from "@makeplane/propel/components/input";
import { TextAreaField } from "@makeplane/propel/components/text-area-field";
import { api, toastError, unwrap } from "~/lib/api/client";
import type { components } from "~/lib/api/types.gen";

type BotOut = components["schemas"]["BotOut"];

/** Direct editing - the project-charter pattern, twice over: one dirty state, one save that
 *  mints the next draft whole. */
export function Editor({ bot, onSaved }: { bot: BotOut; onSaved: () => void }) {
  // Structured routing when the bot has it OR has nothing yet (a newborn gets
  // the three questions, never a grammar textarea); the raw sentence only for bots whose
  // author typed one verbatim. The server composes the grammar - nobody retypes it here.
  const structured = Boolean(
    bot.job || bot.use_when.length > 0 || bot.not_for.length > 0 || !bot.description,
  );
  const [description, setDescription] = useState(bot.description);
  const [job, setJob] = useState(bot.job);
  const [useWhen, setUseWhen] = useState(bot.use_when.join(", "));
  const [notFor, setNotFor] = useState(bot.not_for.join(", "));
  const [instructions, setInstructions] = useState(bot.instructions);
  const [busy, setBusy] = useState(false);
  const phrases = (s: string) =>
    s
      .split(",")
      .map((p) => p.trim())
      .filter(Boolean);
  const dirty = structured
    ? job.trim() !== bot.job ||
      phrases(useWhen).join("|") !== bot.use_when.join("|") ||
      phrases(notFor).join("|") !== bot.not_for.join("|") ||
      instructions.trim() !== bot.instructions
    : description.trim() !== bot.description || instructions.trim() !== bot.instructions;

  async function save() {
    setBusy(true);
    try {
      const saved = await unwrap(
        api.PUT("/v1/bots/{name}", {
          params: { path: { name: bot.name } },
          body: {
            // structured mode leaves description to the server's composer; verbatim mode
            // carries the structured fields forward untouched so an edit never drops them
            ...(structured
              ? job.trim() || phrases(useWhen).length > 0 || phrases(notFor).length > 0
                ? {
                    job: job.trim(),
                    use_when: phrases(useWhen),
                    not_for: phrases(notFor),
                  }
                : { description: bot.description || undefined } // all blank: never wipe what exists
              : {
                  description: description.trim(),
                  job: bot.job,
                  use_when: bot.use_when,
                  not_for: bot.not_for,
                }),
            instructions: instructions.trim(),
            capability: bot.capability,
            produces: bot.produces,
            tools: bot.tools,
            skills: bot.skills,
            connectors: bot.connectors,
            can_ask_user: bot.can_ask_user,
            // the AUTHORED cases ride forward - an edit must never silently drop the evals
            cases: bot.cases.map((c) => ({ goal: c.goal, expect: c.expect })),
            // the nightly golden cases ride forward too, for the same never-silently reason
            golden: bot.golden as {
              goal: string;
              must_contain?: string[];
              inputs?: string[];
            }[],
          },
        }),
      );
      toast.success(`Saved as draft v${saved.version}.`);
      onSaved();
    } catch (e) {
      toastError(e, "Couldn't save the draft.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Section title={structured ? "Routing" : "Description"}>
        <Rows as="div">
          {structured ? (
            <div className="space-y-3 px-4 py-3.5">
              <div className="space-y-1.5">
                <p className="text-muted-foreground text-caption-md-regular">
                  What does this bot do?
                </p>
                <InputGroup size="lg">
                  <Input
                    size="lg"
                    value={job}
                    onChange={(e) => setJob(e.currentTarget.value)}
                    aria-label="What this bot does"
                  />
                </InputGroup>
              </div>
              <div className="space-y-1.5">
                <p className="text-muted-foreground text-caption-md-regular">
                  When should it step in? Commas separate triggers.
                </p>
                <InputGroup size="lg">
                  <Input
                    size="lg"
                    value={useWhen}
                    onChange={(e) => setUseWhen(e.currentTarget.value)}
                    aria-label="When it steps in"
                  />
                </InputGroup>
              </div>
              <div className="space-y-1.5">
                <p className="text-muted-foreground text-caption-md-regular">
                  What should it refuse?
                </p>
                <InputGroup size="lg">
                  <Input
                    size="lg"
                    value={notFor}
                    onChange={(e) => setNotFor(e.currentTarget.value)}
                    aria-label="What it refuses"
                  />
                </InputGroup>
              </div>
              {bot.description ? (
                <p className="text-muted-foreground text-caption-md-regular">
                  Routes as: “{bot.description}”
                </p>
              ) : (
                <p className="text-muted-foreground text-caption-md-regular">
                  No routing yet - fill these three before publishing.
                </p>
              )}
            </div>
          ) : (
            <div className="space-y-2 px-4 py-3.5">
              <p className="text-muted-foreground text-caption-md-regular">
                How tasks find this bot - what it is for, and the work it should refuse. Keep the
                “Load when … Not for …” shape.
              </p>
              <TextAreaField
                size="lg"
                autoResize
                value={description}
                onChange={(e) => setDescription(e.currentTarget.value)}
                rows={3}
                maxLength={600}
                aria-label="Bot description"
              />
            </div>
          )}
        </Rows>
      </Section>

      <Section
        title="Instructions"
        actions={
          dirty ? (
            <Button
              variant="primary"
              size="sm"
              stretch="auto"
              label={`Save as draft v${bot.version + 1}`}
              onClick={() => void save()}
              disabled={busy}
            />
          ) : undefined
        }
      >
        <Rows as="div">
          <div className="space-y-2 px-4 py-3.5">
            <p className="text-muted-foreground text-caption-md-regular">
              The judgment this bot brings. Saving mints the next draft - the published version
              keeps serving until the new one tests green and you publish it.
            </p>
            {/* the instructions read as a prompt: mono, inherited by the textarea */}
            <div className="font-mono">
              <TextAreaField
                size="lg"
                autoResize
                value={instructions}
                onChange={(e) => setInstructions(e.currentTarget.value)}
                rows={14}
                aria-label="Bot instructions"
              />
            </div>
          </div>
        </Rows>
      </Section>
    </>
  );
}
