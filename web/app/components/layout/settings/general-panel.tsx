/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Button } from "@makeplane/propel/components/button";
import { InputField } from "@makeplane/propel/components/input-field";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectList,
  SelectTrigger,
} from "@makeplane/propel/components/select";
import { Monitor, Moon, Sun } from "lucide-react";
import { useTheme } from "next-themes";
import { useEffect, useState } from "react";
import { useRevalidator } from "react-router";
import { toast } from "@prismoid/blocks/toast";
import { LoadingState, PageHeader, Section, ToggleChip } from "~/components/kit";
import { Card, CardContent } from "@prismoid/blocks/card";
import { toastError, api, unwrap } from "~/lib/api/client";

// ── General: profile, appearance, timezone, language - the one account tab ────────────────────

const fetchMe = () => unwrap(api.GET("/v1/auth/me"));

/** "First Last" ⇄ the one canonical name the API stores. The split is presentational: the
 *  first word is the first name, everything after it is the last name. */
function splitName(name: string): { first: string; last: string } {
  const [first = "", ...rest] = name.trim().split(/\s+/);
  return { first, last: rest.join(" ") };
}

/** IANA zones from the runtime, with the stored value kept present even if this browser
 *  doesn't list it - a select whose current value is missing renders as empty. */
function timezoneOptions(current: string): string[] {
  const zones: string[] =
    typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];
  return zones.includes(current) ? zones : [current, ...zones];
}

export function GeneralPanel() {
  const [me, setMe] = useState<Awaited<ReturnType<typeof fetchMe>> | null>(null);
  const [first, setFirst] = useState("");
  const [last, setLast] = useState("");
  const [busy, setBusy] = useState(false);
  const revalidator = useRevalidator();

  useEffect(() => {
    fetchMe()
      .then((u) => {
        setMe(u);
        const parts = splitName(u.name);
        setFirst(parts.first);
        setLast(parts.last);
      })
      .catch(() => toast.error("Couldn't load your profile."));
  }, []);

  const name = [first.trim(), last.trim()].filter(Boolean).join(" ");
  const dirty = me !== null && name !== me.name;

  async function saveName() {
    if (!name) return;
    setBusy(true);
    try {
      const updated = await unwrap(api.PATCH("/v1/auth/me", { body: { name } }));
      // PATCH answers UserOut (no is_admin - that's MeOut's, /auth/me GET only); merge so
      // the loaded profile keeps its admin standing.
      setMe((cur) => (cur ? { ...cur, ...updated } : cur));
      revalidator.revalidate(); // the sidebar's account menu shows this name
      toast.success("Profile saved.");
    } catch (e) {
      toastError(e, "Couldn't save your profile.");
    } finally {
      setBusy(false);
    }
  }

  async function saveTimezone(timezone: string) {
    try {
      const updated = await unwrap(api.PATCH("/v1/auth/me", { body: { timezone } }));
      setMe((cur) => (cur ? { ...cur, ...updated } : cur));
      toast.success("Timezone saved.");
    } catch (e) {
      toastError(e, "Couldn't save the timezone.");
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        as="h2"
        title="General"
        description="Who you are, and how the product reads and looks."
      />
      {me === null ? (
        <LoadingState />
      ) : (
        <>
          <Section title="Profile">
            <Card size="sm">
              <CardContent className="space-y-4">
                <div className="grid gap-3 sm:grid-cols-2">
                  <InputField
                    size="lg"
                    orientation="vertical"
                    label="First name"
                    value={first}
                    onChange={(e) => setFirst(e.currentTarget.value)}
                    placeholder="Ada"
                  />
                  <InputField
                    size="lg"
                    orientation="vertical"
                    label="Last name"
                    value={last}
                    onChange={(e) => setLast(e.currentTarget.value)}
                    placeholder="Lovelace"
                  />
                </div>
                <InputField
                  size="lg"
                  orientation="vertical"
                  label="Email"
                  value={me.email}
                  disabled
                  readOnly
                  hint="Your email is how you sign in; it can't be changed here."
                />
                <div className="flex justify-end">
                  <Button
                    variant="primary"
                    size="sm"
                    stretch="auto"
                    onClick={() => void saveName()}
                    disabled={busy || !name || !dirty}
                    label="Save"
                  />
                </div>
              </CardContent>
            </Card>
          </Section>

          <Section title="Appearance">
            <ThemePicker />
          </Section>

          <Section title="Timezone">
            <Select
              modal={false}
              value={me.timezone}
              onValueChange={(v) => {
                if (v && v !== me.timezone) void saveTimezone(v);
              }}
            >
              <SelectTrigger size="lg" variant="secondary" width="auto" aria-label="Timezone" />
              <SelectContent>
                <SelectList>
                  {timezoneOptions(me.timezone).map((tz) => (
                    <SelectItem key={tz} value={tz} label={tz} />
                  ))}
                </SelectList>
              </SelectContent>
            </Select>
          </Section>

          <Section title="Language">
            <Select modal={false} value="en" items={{ en: "English" }} disabled>
              <SelectTrigger size="lg" variant="secondary" width="auto" aria-label="Language" />
              <SelectContent>
                <SelectList>
                  <SelectItem value="en" label="English" />
                </SelectList>
              </SelectContent>
            </Select>
            <p className="text-muted-foreground mt-1.5 text-caption-md-regular">
              English is the only interface language for now.
            </p>
          </Section>
        </>
      )}
    </div>
  );
}

function ThemePicker() {
  const { theme, setTheme } = useTheme();
  const options = [
    { value: "light", label: "Light", icon: Sun },
    { value: "dark", label: "Dark", icon: Moon },
    { value: "system", label: "System", icon: Monitor },
  ];
  return (
    <div className="flex gap-2">
      {options.map((o) => (
        <ToggleChip key={o.value} active={theme === o.value} onClick={() => setTheme(o.value)}>
          <o.icon className="size-3.5" />
          {o.label}
        </ToggleChip>
      ))}
    </div>
  );
}
