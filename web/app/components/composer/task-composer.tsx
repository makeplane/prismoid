/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The task-minting box with every dial - ONE component, two homes: the landing (its own Project
// picker, remembering the last Project used) and a Project's page (a task
// starts from the Project itself, scoped to it). Promoted from routes/home on its second consumer;
// the submit path (create a task, or save a routine when the When dial is a schedule) is the
// landing's, unchanged - a Project page only fixes `project_id` and lands the new task on its
// canonical URL.

import { useEffect, useMemo, useState, type ComponentProps } from "react";
import { useNavigate } from "react-router";
import { toast } from "@prismoid/blocks/toast";
import { Composer } from "~/components/composer/composer";
import { toInputs, type Attachment } from "~/components/composer/capture";
import type { BotOut } from "~/components/composer/bots-control";
import type { ConnectorOut } from "~/components/composer/connectors-control";
import { fetchCatalog, type CatalogEntry } from "~/components/connectors/catalog";
import { useDebouncedValue } from "~/lib/paging";
import { suggestConnector } from "~/components/connectors/suggest";
import { ConnectorSuggestionChip } from "~/components/connectors/suggestion-chip";
import { useConnectFlows } from "~/components/connectors/use-connect-flows";
import {
  SINGLE_BREATH_BLOCKED,
  splitDeliverableChoice,
  type DeliverableChoice,
  type ModelOut,
  type TaskScale,
  fleetEligible,
} from "~/components/composer/dials";
import { OptionsMenu, type OptionsMenuProps } from "~/components/composer/options-menu";
import { SetOptions, type Reveal } from "~/components/composer/set-options";
import { MAX_SKILLS, type Clarify } from "~/components/composer/task-dials";
import { toastError, api, ApiError, unwrap } from "~/lib/api/client";
import { formatDateTime } from "~/lib/format";
import { inputsProblem } from "~/lib/input-limits";
import { taskHref } from "~/lib/links";
import { HOLD_PAGE } from "~/lib/pending-route";
import { onConnectorsChanged } from "~/lib/settings-bus";
import { ProjectPicker, type ProjectRef } from "./project-picker";
import { WHEN_NOW, whenLabel, type WhenValue } from "./when-control";
import type { components } from "~/lib/api/types.gen";
import { STORAGE } from "~/lib/storage";
import { uuidV4 } from "~/lib/uuid";

type TaskCreateIn = components["schemas"]["TaskCreateIn"];
type SkillOut = components["schemas"]["SkillOut"];

const LAST_PROJECT_KEY = STORAGE.lastProject;

/** What the box needs from the API before it can draw: the dials' rosters. Best-effort all the
 *  way down - a missing roster is a missing dial, never a blocked composer. */
export type ComposerOptions = {
  skills: SkillOut[];
  fleetModels: ModelOut[];
  connectors: ConnectorOut[];
  catalog: CatalogEntry[] | null;
  bots: BotOut[];
};

/** Fetch the dials' rosters (a route loader's job: the box paints complete on first render). */
export async function loadComposerOptions(): Promise<ComposerOptions> {
  const [skills, models, connectorsPage, catalog, botsPage] = await Promise.all([
    unwrap(api.GET("/v1/skills")).catch(() => [] as SkillOut[]),
    unwrap(api.GET("/v1/models")).catch(() => [] as ModelOut[]),
    // Best-effort like the rest: no connectors simply means no dial, never a blocked composer.
    unwrap(api.GET("/v1/connectors", { params: { query: { limit: 200 } } })).catch(() => null),
    // The catalog feeds the suggestion chip's grammar - same best-effort posture.
    fetchCatalog(),
    // Same posture for bots: the roster is auto-included server-side either way;
    // this only feeds the narrowing control.
    unwrap(api.GET("/v1/bots", { params: { query: { limit: 200 } } })).catch(() => null),
  ]);
  const latest = new Map<string, SkillOut>();
  for (const s of skills) {
    const prev = latest.get(s.name);
    if (!prev || s.version > prev.version) latest.set(s.name, s);
  }
  return {
    skills: [...latest.values()].sort((a, b) => a.name.localeCompare(b.name)),
    fleetModels: fleetEligible(models),
    // The whole roster, not just the live half - the control shows switches for live ones
    // and an honest "n to authorize" for the rest.
    connectors: connectorsPage?.items ?? [],
    catalog,
    bots: botsPage?.items ?? [],
  };
}

/** The server's own reason for a 422 on start, else undefined: a problem with the attached files
 *  in words about those files, else the one under `goal`, else `body`. */
function unprocessableReason(e: unknown, attached: Attachment[]): string | undefined {
  if (!(e instanceof ApiError) || e.status !== 422) return undefined;
  const sent = toInputs(attached).map((i) => i.filename);
  return inputsProblem(e.fields, sent) ?? e.fields?.goal ?? e.fields?.body;
}

const noStartHook = () => {};

export function TaskComposer({
  options,
  initialProject = null,
  fixedProject,
  className,
  tall,
  placeholder = "Describe what you need done. Attach anything that helps.",
  onStarting = noStartHook,
  ...box
}: Pick<ComponentProps<typeof Composer>, "autoFocus" | "apiRef"> & {
  options: ComposerOptions;
  /** The landing's pre-filled Project (its last-used memory); the picker can change it. */
  initialProject?: ProjectRef | null;
  /** A Project page's own Project: the task is created in it and there is no picker. */
  fixedProject?: ProjectRef;
  className?: string;
  /** Landing size: rest at ~3 lines instead of one. */
  tall?: boolean;
  placeholder?: string;
  /** A task is being created from this goal (the request is out), or null when that failed:
   *  the page shows the ask and a loader at once, and puts the composer back on a failure. */
  onStarting?: (goal: string | null) => void;
}) {
  const { skills, fleetModels, catalog, bots } = options;
  const navigate = useNavigate();

  const [mode, setMode] = useState<"standard" | "fleet">("standard");
  // The size dial: auto = a number in the goal decides, otherwise standard - the
  // system never picks bigger than the ask (the no-push law lives server-side; this is its UI).
  const [scale, setScale] = useState<TaskScale>("auto");
  const [clarify, setClarify] = useState<Clarify>("auto");
  // The chosen artifact kind. "auto" = the planner reads the goal; anything else
  // is a CONSTRAINT the server enforces end to end - the dropdown's promise is deterministic.
  const [deliverable, setDeliverable] = useState<DeliverableChoice>("auto");
  // Budget is a normalized decimal string ("2.00") or "" for no cap (task-dials normalizes).
  const [budgetUsd, setBudgetUsd] = useState("");
  const handOff = onStarting === noStartHook ? undefined : HOLD_PAGE;
  const [reveal, setReveal] = useState<Reveal>(null);
  const [revealed, setRevealed] = useState({ when: 0, budget: 0 });
  function revealPanel(dial: NonNullable<Reveal>) {
    setReveal(dial);
    setRevealed((n) => ({ ...n, [dial]: n[dial] + 1 }));
  }
  // Where the task will live. The landing picks it (and remembers the last pick); a Project
  // page hands its own Project down as `fixedProject` - scoped, nothing to wander off with.
  const [picked, setPicked] = useState<ProjectRef | null>(initialProject);
  const project = fixedProject ?? picked;
  // The WHEN dial: now = today's behavior; a schedule/event SAVES this payload as
  // a routine instead of sending it. The organic lane - /routines has its own creation dialog.
  const [when, setWhen] = useState<WhenValue>(WHEN_NOW);
  function setProject(next: ProjectRef | null) {
    setPicked(next);
    if (next) localStorage.setItem(LAST_PROJECT_KEY, JSON.stringify(next));
    else localStorage.removeItem(LAST_PROJECT_KEY);
  }
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  // Connectors are explicit per task: nothing attaches unless chosen here.
  const [connectorIds, setConnectorIds] = useState<string[]>([]);
  // Settings announces every roster mutation on the bus; the roster refreshes in place - the
  // settings dialog opens OVER the page, so no navigation would do it. (The loader's roster
  // is only the first paint: a host that revalidates hands a fresh one down, and it wins.)
  const [connectors, setConnectors] = useState(options.connectors);
  useEffect(() => {
    setConnectors(options.connectors);
  }, [options.connectors]);
  useEffect(
    () =>
      onConnectorsChanged(() => {
        void unwrap(api.GET("/v1/connectors", { params: { query: { limit: 200 } } })).then(
          (page) => setConnectors(page.items),
          () => {}, // best-effort, like the first load: a stale roster beats a broken box
        );
      }),
    [],
  );
  // The Bot dial: null = Auto (One routes); a name = a DIRECT task - that bot
  // answers every turn, exactly like its own chat.
  const [botRef, setBotRef] = useState<string | null>(null);
  // The suggestion chip: the draft's words against the catalog + roster, the same
  // word-boundary grammar the server's auto-attach door speaks. Debounced; dismissals live
  // per draft (clearing the box forgives them); absent when nothing matches.
  const [draft, setDraft] = useState("");
  const debouncedDraft = useDebouncedValue(draft, 250);
  const [dismissedSlugs, setDismissedSlugs] = useState<string[]>([]);
  useEffect(() => {
    if (debouncedDraft.trim() === "") setDismissedSlugs([]);
  }, [debouncedDraft]);
  const suggestion = useMemo(() => {
    const hit = suggestConnector(debouncedDraft, catalog, connectors, connectorIds);
    return hit && !dismissedSlugs.includes(hit.slug) ? hit : null;
  }, [debouncedDraft, catalog, connectors, connectorIds, dismissedSlugs]);
  const suggestFlows = useConnectFlows();
  // And prune selections the fresh roster no longer honors, so a removed connector can't
  // ride into a 422 at the door.
  useEffect(() => {
    setConnectorIds((cur) =>
      cur.filter((id) => connectors.some((c) => c.id === id && c.status === "live")),
    );
  }, [connectors]);
  // And prune an oversize bucket when the mode flips to fleet - the server 422s the pair
  // (the single-breath law), so the composer must never submit it.
  useEffect(() => {
    if (mode === "fleet" && SINGLE_BREATH_BLOCKED.has(scale)) setScale("auto");
  }, [mode, scale]);
  // Every model flies by default; the picker narrows. Kept as refs so the payload is the wire value.
  const [modelRefs, setModelRefs] = useState<string[]>(() => fleetModels.map((m) => m.ref));
  const [busy, setBusy] = useState(false);

  function toggleSkill(name: string) {
    setSelectedSkills((cur) =>
      cur.includes(name)
        ? cur.filter((s) => s !== name)
        : cur.length >= MAX_SKILLS
          ? (toast.error(`Up to ${MAX_SKILLS} skills.`), cur)
          : [...cur, name],
    );
  }

  async function start({ text, attachments }: { text: string; attachments: Attachment[] }) {
    const goal = text.trim();
    if (!goal) {
      toast.error("Describe the task to start.");
      return;
    }
    setBusy(true);
    // A standing When branches here: the SAME payload posts as a routine (the constitution -
    // the composer, saved instead of sent). Fleet seats and attachments have no standing
    // shape yet, so the refusal is honest and immediate rather than a 422 later.
    if (when.mode !== "now") {
      if (mode === "fleet") {
        setBusy(false);
        toast.error("Routines run standard tasks. Switch the mode dial off fleet first.");
        return;
      }
      if (attachments.length) {
        setBusy(false);
        toast.error("Routines can't carry attachments yet - each run starts fresh.");
        return;
      }
      try {
        const saved = await unwrap(
          api.POST("/v1/routines", {
            body: {
              name: goal.length > 60 ? `${goal.slice(0, 60)}…` : goal,
              instruction: goal,
              ...(when.mode === "schedule"
                ? { schedule: { cron: when.cron ?? null, every: when.every ?? null } }
                : { on_events: when.kinds }),
              ...(project ? { project_id: project.id } : {}),
              ...(budgetUsd.trim() ? { budget_usd: Number(budgetUsd) } : {}),
              // the size dial rides the saved payload too - "the SAME payload, saved instead
              // of sent" (it was once silently dropped on this lane)
              ...(scale !== "auto" ? { scale } : {}),
              ...(botRef !== null ? { bot: botRef } : {}),
              ...(selectedSkills.length ? { skills: selectedSkills } : {}),
              ...(connectorIds.length ? { connectors: connectorIds } : {}),
              ...(mode === "standard" ? splitDeliverableChoice(deliverable) : {}),
            },
          }),
        );
        toast.success(
          `Routine saved - ${whenLabel(when)}${saved.upcoming[0] ? `, first fire ${formatDateTime(saved.upcoming[0])}` : ""}.`,
        );
        navigate(`/routines?routine=${saved.id}`);
      } catch (e) {
        setBusy(false);
        toastError(e, "Couldn't save the routine.");
        throw e;
      }
      return;
    }
    const body: TaskCreateIn = {
      goal,
      mode,
      clarify,
      // The chosen bucket, or auto (a literal number in the goal picks the bucket;
      // otherwise standard - never bigger). The resolved size comes back on the task row.
      scale,
      // one token per SEND GESTURE - a replayed request returns the same task (guardrail)
      client_token: uuidV4(),
      ...(project ? { project_id: project.id } : {}),
      ...(budgetUsd.trim() ? { budget: { usd: budgetUsd.trim() } } : {}),
      ...(selectedSkills.length ? { skills: selectedSkills } : {}),
      ...(connectorIds.length ? { connectors: connectorIds } : {}),
      direct: botRef !== null,
      ...(botRef !== null ? { bots: [botRef] } : {}),
      // A task on the device works in a fresh folder from the web:
      // folders and Docker are granted from the CLI only in v1.
      docker: false,
      // Only standard mode takes a deliverable (a fleet delivers the chair's table - the
      // server refuses the pair with a 422, so don't offer it).
      // ONE dial carries kind + document format; split back to the wire's
      // two fields - "auto" sends neither.
      ...(mode === "standard" ? splitDeliverableChoice(deliverable) : {}),
      ...(attachments.length ? { inputs: toInputs(attachments) } : {}),
      // Send models only when the picker narrowed them - the full fleet is the server default.
      ...(mode === "fleet" && modelRefs.length < fleetModels.length ? { models: modelRefs } : {}),
    };
    onStarting(goal);
    try {
      const accepted = await unwrap(api.POST("/v1/tasks", { body }));
      // A host showing its own wait (Home's ask and loader) keeps the screen until the task
      // page is ready; any other host gets the shell's page loader.
      navigate(taskHref({ id: accepted.task_id, project_id: project?.id }), { state: handOff });
    } catch (e) {
      setBusy(false);
      onStarting(null);
      // A model-level 422 (the fleet single-breath pair, a bad enum) lands under fields.body
      // with a generic top-line - name the real reason either way.
      const named = unprocessableReason(e, attachments);
      if (named) toast.error(named);
      else toastError(e, "Couldn't start the task.");
      // Reject so the composer keeps the typed goal + attachments for a retry - it clears
      // only on a resolved send (composer.tsx).
      throw e;
    }
  }

  // One set of values, two faces: the Options menu (every setting) and the row's pills (the
  // changed ones) read the same props.
  const opts: OptionsMenuProps = {
    when,
    onWhen: setWhen,
    onCustomWhen: () => revealPanel("when"),
    mode,
    onMode: setMode,
    scale,
    onScale: setScale,
    deliverable,
    onDeliverable: setDeliverable,
    fleetModels,
    modelRefs,
    onModelRefs: setModelRefs,
    clarify,
    onClarify: setClarify,
    budgetUsd,
    onBudget: setBudgetUsd,
    onCustomBudget: () => revealPanel("budget"),
    skills,
    selectedSkills,
    onToggleSkill: toggleSkill,
    connectors,
    connectorIds,
    onConnectorIds: setConnectorIds,
    bots,
    botRef,
    onBot: setBotRef,
  };

  return (
    <Composer
      onTextChange={setDraft}
      className={className}
      tall={tall}
      {...box}
      onSubmit={start}
      status={busy ? "submitting" : "idle"}
      placeholder={placeholder}
      settings={
        <>
          {!fixedProject && <ProjectPicker value={project} onChange={setProject} />}
          <OptionsMenu {...opts} />
          <SetOptions
            {...opts}
            reveal={reveal}
            revealed={revealed}
            onRevealDone={() => setReveal(null)}
            onClearSkills={() => setSelectedSkills([])}
          />
          {suggestion && (
            <ConnectorSuggestionChip
              suggestion={suggestion}
              flow={suggestFlows.flows[suggestion.slug] ?? null}
              onAttach={(id) => setConnectorIds((cur) => [...cur, id])}
              onConnect={(slug) => {
                void suggestFlows.connectFromCatalog(slug).then((row) => {
                  // Live right away (an open server): attach it now. An OAuth flow lands
                  // through the bus - the roster revalidates and the user flips the switch.
                  if (row?.status === "live") setConnectorIds((cur) => [...cur, row.id]);
                });
              }}
              onDismiss={(slug) => setDismissedSlugs((cur) => [...cur, slug])}
            />
          )}
        </>
      }
    />
  );
}
