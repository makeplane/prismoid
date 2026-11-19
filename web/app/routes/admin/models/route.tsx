/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Model administration as a full route (the settings-dialog ModelsPanel's logic, given a
// page): the doctor's findings first (a sick catalog is the headline, not a footnote), the
// catalog with its enable/disable switches, the price book with a per-ref reprice dialog
// (copy-forward: the PUT names ONLY the edited ref; the server carries the rest into the
// new version), and the routing table - read-only v1: the wire's PUT replaces whole chains,
// and a faithful chain editor is its own piece of work, so no control is faked here.

import { HeartPulse, Plus, X } from "lucide-react";
import { useState } from "react";
import { useRevalidator, useLoaderData } from "react-router";
import { toast } from "@prismoid/blocks/toast";
import { EmptyState, MetaChip, Rows, Section, ToggleChip } from "~/components/kit";
import { Badge } from "@makeplane/propel/components/badge";
import { Button } from "@makeplane/propel/components/button";
import {
  Dialog,
  DialogActions,
  DialogBody,
  DialogClose,
  DialogCloseGroup,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogHeading,
  DialogMain,
  DialogTitle,
  DialogTrigger,
} from "@makeplane/propel/components/dialog";
import { Fieldset } from "@makeplane/propel/components/fieldset";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { InputField } from "@makeplane/propel/components/input-field";
import { Switch } from "@makeplane/propel/components/switch";
import { api, toastError, unwrap } from "~/lib/api/client";
import { cn } from "~/lib/utils";

export function meta() {
  return [{ title: "Models · Admin · Prismoid" }];
}

export async function clientLoader() {
  const [models, routing, doctor, prices] = await Promise.all([
    unwrap(api.GET("/v1/admin/models")),
    // Best-effort, like the rest - but note what the fallback CANNOT invent: the class
    // vocabulary. An empty list means the chips and the routing table render empty rather
    // than render a stale guess, and the section says so.
    unwrap(api.GET("/v1/admin/routing-table")).catch(() => ({
      chains: {} as Record<string, string[]>,
      classes: [] as string[],
      version: 0,
    })),
    unwrap(api.GET("/v1/admin/models/doctor")).catch(() => []),
    unwrap(api.GET("/v1/admin/price-book")).catch(() => null),
  ]);
  return { models, routing, doctor, prices };
}
clientLoader.hydrate = true as const;

// Prices a NEW model needs on day one, by what it is. The book takes any unit
// (`schemas/usage.Unit`); these are the ones whose absence is immediately a doctor finding -
// an enabled chat seat with no token prices, or an image lane metering figures at $0. Anything
// else is a reprice away, in the Price book section below.
const CHAT_UNITS = ["input_token", "output_token"] as const;
const IMAGE_UNITS = ["image"] as const;
const unitsFor = (classes: string[]): readonly string[] =>
  classes.includes("image-gen") ? IMAGE_UNITS : CHAT_UNITS;

export default function AdminModels() {
  const loaderData = useLoaderData<typeof clientLoader>();
  const { models, routing, doctor, prices } = loaderData;
  // The class vocabulary comes off the wire (`RoutingOut.classes` ← `CapabilityClass`), never
  // a copy kept here: the copy that used to live in this file was missing `image-gen`, so a
  // live lane with three catalog models behind it could be neither tagged nor seen routed.
  const classes = routing.classes;
  const revalidator = useRevalidator();
  const reload = () => revalidator.revalidate();

  async function toggle(ref: string, enabled: boolean) {
    try {
      await unwrap(
        api.PATCH("/v1/admin/models/{id}", {
          params: { path: { id: ref } },
          body: { enabled },
        }),
      );
      reload();
    } catch (e) {
      toastError(e, "Couldn't update the model.");
    }
  }

  return (
    <div className="space-y-6">
      <Section title="Doctor">
        {doctor.length === 0 ? (
          <EmptyState
            compact
            icon={HeartPulse}
            title="Catalog healthy"
            description="Every enabled model has a price, a class, and a route."
          />
        ) : (
          <Rows as="ul">
            {doctor.map((f, i) => (
              <li key={`${f.check}-${f.ref}-${i}`} className="flex items-center gap-3 px-4 py-3">
                <Badge variant="neutral" size="xs" label={f.check} />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-body-sm-regular font-medium">{f.ref}</span>
                  <span className="text-muted-foreground block text-caption-md-regular">
                    {f.detail}
                  </span>
                </span>
              </li>
            ))}
          </Rows>
        )}
      </Section>

      <Section title="Catalog" actions={<AddModel classes={classes} onAdded={reload} />}>
        {models.length === 0 ? (
          <EmptyState icon={Plus} title="No models yet" />
        ) : (
          <Rows as="ul">
            {models.map((m) => (
              <li key={m.ref} className="flex items-center gap-3 px-4 py-3">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span
                      className={cn(
                        "text-body-sm-regular font-medium",
                        !m.enabled && "text-muted-foreground",
                      )}
                    >
                      {m.ref}
                    </span>
                    <Badge variant="neutral" size="xs" label={m.family} />
                  </div>
                  <div className="mt-1 flex flex-wrap gap-x-2 gap-y-0.5">
                    {m.classes.map((c) => (
                      <span key={c} className="text-muted-foreground text-caption-md-regular">
                        {c}
                      </span>
                    ))}
                  </div>
                </div>
                <span className="text-muted-foreground shrink-0 text-caption-md-regular tabular-nums">
                  {(m.context_window / 1000).toFixed(0)}K ctx
                </span>
                <Switch
                  size="md"
                  checked={m.enabled}
                  onCheckedChange={(v) => void toggle(m.ref, v === true)}
                  aria-label={`${m.enabled ? "Disable" : "Enable"} ${m.ref}`}
                />
              </li>
            ))}
          </Rows>
        )}
      </Section>

      <Section title={prices ? `Price book · v${prices.version}` : "Price book"}>
        {!prices || Object.keys(prices.prices).length === 0 ? (
          <EmptyState
            compact
            icon={Plus}
            title="No prices yet"
            description="Every provider call needs a price row before its cost can land in the ledger."
          />
        ) : (
          <Rows as="ul">
            {Object.entries(prices.prices).map(([ref, units]) => (
              <li key={ref} className="flex items-center gap-3 px-4 py-3">
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-body-sm-regular font-medium">{ref}</span>
                  <span className="mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5">
                    {Object.entries(units).map(([unit, price]) => (
                      <MetaChip key={unit} className="tabular-nums">
                        {unit} = {price}
                      </MetaChip>
                    ))}
                  </span>
                </span>
                <EditPrices modelRef={ref} units={units} onSaved={reload} />
              </li>
            ))}
          </Rows>
        )}
      </Section>

      {/* EVERY class, routed or not - the section is the vocabulary, not just the configured
          half. An unrouted class is a real, working state (routing.py: it falls back to
          alphabetical), and hiding it was how a whole lane went missing from this screen. */}
      <Section title={`Routing table · v${routing.version}`}>
        <Rows as="ul">
          {classes.map((cls) => {
            const chain = routing.chains[cls] ?? [];
            return (
              <li key={cls} className="flex items-center gap-3 px-4 py-3">
                <span className="w-40 shrink-0 text-body-sm-regular font-medium">{cls}</span>
                {chain.length > 0 ? (
                  <span className="text-muted-foreground min-w-0 flex-1 truncate text-body-sm-regular">
                    {chain.join(" → ")}
                  </span>
                ) : (
                  <span className="text-muted-foreground min-w-0 flex-1 truncate text-body-sm-regular italic">
                    unrouted - falls back to alphabetical order
                  </span>
                )}
              </li>
            );
          })}
        </Rows>
      </Section>
    </div>
  );
}

/** Reprice ONE ref: a dialog of per-unit inputs. The PUT is copy-forward - it names only
 *  this ref; every other ref rides into the new version untouched. Prices stay STRINGS end
 *  to end (JSON must never round a cent). */
function EditPrices({
  modelRef,
  units,
  onSaved,
}: {
  modelRef: string;
  units: Record<string, string>;
  onSaved: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<Record<string, string>>(units);
  const [busy, setBusy] = useState(false);
  const dirty = Object.keys(units).some((u) => draft[u] !== units[u]);

  async function save() {
    setBusy(true);
    try {
      const out = await unwrap(
        api.PUT("/v1/admin/price-book", {
          body: { prices: { [modelRef]: draft } },
        }),
      );
      toast.success(`Price book v${out.version} - ${modelRef} repriced.`);
      setOpen(false);
      onSaved();
    } catch (e) {
      toastError(e, "Couldn't save the prices.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (o) setDraft(units); // a reopened dialog starts from the book, not a stale draft
      }}
    >
      <DialogTrigger
        render={<Button variant="secondary" size="sm" stretch="auto" label="Edit prices" />}
      />
      <DialogContent size="xs">
        <DialogCloseGroup>
          <IconButton
            variant="ghost"
            size="xs"
            aria-label="Close"
            icon={<Icon icon={X} />}
            render={<DialogClose />}
          />
        </DialogCloseGroup>
        <DialogMain>
          <DialogHeader>
            <DialogHeading>
              <DialogTitle>Reprice {modelRef}</DialogTitle>
              <DialogDescription>
                USD per native unit. Saving mints a new price-book version - only this model
                changes; everything else carries forward.
              </DialogDescription>
            </DialogHeading>
          </DialogHeader>
          <DialogBody tabIndex={0}>
            <div className="space-y-3 tabular-nums">
              {Object.keys(units).map((unit) => (
                <InputField
                  key={unit}
                  size="lg"
                  orientation="vertical"
                  label={unit}
                  inputMode="decimal"
                  value={draft[unit] ?? ""}
                  onChange={(e) => setDraft((d) => ({ ...d, [unit]: e.currentTarget.value }))}
                />
              ))}
            </div>
          </DialogBody>
        </DialogMain>
        <DialogActions>
          <Button
            variant="primary"
            size="md"
            stretch="auto"
            label="Save new version"
            disabled={busy || !dirty}
            onClick={() => void save()}
          />
        </DialogActions>
      </DialogContent>
    </Dialog>
  );
}

/** Adding a model is data, never a deploy (the panel's dialog, unchanged in spirit).
 *
 *  It collects PRICES, because the copy always said it did and the body always had the field:
 *  a model added without them is enabled-able and would meter every call at $0 until somebody
 *  noticed the doctor finding. Which units are asked for follows the classes chosen; anything
 *  further is a reprice in the Price book, and a blank field is simply not sent. */
function AddModel({ classes: vocabulary, onAdded }: { classes: string[]; onAdded: () => void }) {
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({
    provider: "",
    model_id: "",
    family: "",
    protocol: "anthropic",
    context_window: "200000",
  });
  const [classes, setClasses] = useState<string[]>([]);
  const [prices, setPrices] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const units = unitsFor(classes);

  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [k]: e.currentTarget.value }));

  async function add() {
    if (!form.provider || !form.model_id || !form.family || classes.length === 0) {
      toast.error("Provider, model id, family, and at least one class are required.");
      return;
    }
    // Strings end to end - a price that becomes a float stops reconciling.
    const priced = Object.fromEntries(
      units.map((u) => [u, (prices[u] ?? "").trim()]).filter(([, v]) => v),
    );
    setBusy(true);
    try {
      await unwrap(
        api.POST("/v1/admin/models", {
          body: {
            provider: form.provider,
            model_id: form.model_id,
            family: form.family,
            protocol: form.protocol,
            context_window: Number(form.context_window) || 0,
            classes,
            prices: priced,
          },
        }),
      );
      setOpen(false);
      onAdded();
      toast.success(
        Object.keys(priced).length > 0
          ? "Model added, disabled, and priced. Enable it when you're ready."
          : "Model added and disabled - price it before you enable it, or it meters at $0.",
      );
    } catch (e) {
      toastError(e, "Couldn't add the model.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger
        render={
          <Button
            variant="primary"
            size="sm"
            stretch="auto"
            icon={<Icon icon={Plus} />}
            label="Add model"
          />
        }
      />
      <DialogContent size="xs">
        <DialogCloseGroup>
          <IconButton
            variant="ghost"
            size="xs"
            aria-label="Close"
            icon={<Icon icon={X} />}
            render={<DialogClose />}
          />
        </DialogCloseGroup>
        <DialogMain>
          <DialogHeader>
            <DialogHeading>
              <DialogTitle>Add a model</DialogTitle>
              <DialogDescription>
                A catalog row + prices, never a deploy. It routes once a class chain names it.
              </DialogDescription>
            </DialogHeading>
          </DialogHeader>
          <DialogBody tabIndex={0}>
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <InputField
                  size="lg"
                  orientation="vertical"
                  label="Provider"
                  value={form.provider}
                  onChange={set("provider")}
                  placeholder="anthropic"
                  autoFocus
                />
                <InputField
                  size="lg"
                  orientation="vertical"
                  label="Model id"
                  value={form.model_id}
                  onChange={set("model_id")}
                  placeholder="claude-…"
                />
                <InputField
                  size="lg"
                  orientation="vertical"
                  label="Family"
                  value={form.family}
                  onChange={set("family")}
                  placeholder="claude"
                />
                <InputField
                  size="lg"
                  orientation="vertical"
                  label="Context window"
                  inputMode="numeric"
                  value={form.context_window}
                  onChange={set("context_window")}
                />
              </div>
              <Fieldset legend="Classes" legendSize="lg" bordered={false}>
                <div className="flex flex-wrap gap-2">
                  {vocabulary.map((c) => (
                    <ToggleChip
                      key={c}
                      active={classes.includes(c)}
                      onClick={() =>
                        setClasses((cur) =>
                          cur.includes(c) ? cur.filter((x) => x !== c) : [...cur, c],
                        )
                      }
                    >
                      {c}
                    </ToggleChip>
                  ))}
                </div>
              </Fieldset>
              <Fieldset legend="Prices - USD per native unit" legendSize="lg" bordered={false}>
                <div className="grid grid-cols-2 gap-3 tabular-nums">
                  {units.map((unit) => (
                    <InputField
                      key={unit}
                      size="lg"
                      orientation="vertical"
                      label={unit}
                      inputMode="decimal"
                      value={prices[unit] ?? ""}
                      onChange={(e) =>
                        setPrices((p) => ({
                          ...p,
                          [unit]: e.currentTarget.value,
                        }))
                      }
                      placeholder="0.000003"
                    />
                  ))}
                </div>
                <p className="text-muted-foreground text-caption-md-regular">
                  Every provider call needs a price row or its cost lands in the ledger as $0. Other
                  units are a reprice away in the price book.
                </p>
              </Fieldset>
            </div>
          </DialogBody>
        </DialogMain>
        <DialogActions>
          <Button
            variant="primary"
            size="md"
            stretch="auto"
            label="Add model"
            onClick={() => void add()}
            disabled={busy}
          />
        </DialogActions>
      </DialogContent>
    </Dialog>
  );
}

export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
