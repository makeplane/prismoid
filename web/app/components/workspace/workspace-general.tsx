/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Workspace → General: name and slug. Owners edit; everyone else reads.

import { Button } from "@makeplane/propel/components/button";
import { InputField } from "@makeplane/propel/components/input-field";
import { useEffect, useState } from "react";
import { useRevalidator } from "react-router";
import { toast } from "@prismoid/blocks/toast";
import { LoadingState, PageHeader, Section } from "~/components/kit";
import { NoWorkspaceState } from "~/components/layout/workspace-switcher";
import { api, toastError, unwrap } from "~/lib/api/client";
import { workspacePerms } from "~/lib/workspaces";
import { useMe } from "./use-workspace";

export function WorkspaceGeneralView({
  name,
  slug,
  canEdit,
  busy,
  onName,
  onSlug,
  onSave,
  dirty,
}: {
  name: string;
  slug: string;
  canEdit: boolean;
  busy: boolean;
  dirty: boolean;
  onName: (v: string) => void;
  onSlug: (v: string) => void;
  onSave: () => void;
}) {
  return (
    <div className="space-y-6">
      <PageHeader as="h2" title="General" description="The workspace's name and address." />
      <Section title="Workspace">
        <div className="space-y-4">
          <InputField
            size="lg"
            orientation="vertical"
            label="Name"
            value={name}
            disabled={!canEdit}
            onChange={(e) => onName(e.currentTarget.value)}
          />
          <InputField
            size="lg"
            orientation="vertical"
            label="Slug"
            value={slug}
            disabled={!canEdit}
            onChange={(e) => onSlug(e.currentTarget.value)}
            hint={canEdit ? undefined : "Only an owner can change these."}
          />
          {canEdit && (
            <Button
              variant="primary"
              size="sm"
              stretch="auto"
              disabled={busy || !dirty}
              onClick={onSave}
              label="Save"
            />
          )}
        </div>
      </Section>
    </div>
  );
}

export function WorkspaceGeneralPanel() {
  const me = useMe();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [busy, setBusy] = useState(false);
  const revalidator = useRevalidator();

  useEffect(() => {
    if (me?.workspace) {
      setName(me.workspace.name);
      setSlug(me.workspace.slug);
    }
  }, [me]);
  if (!me) return <LoadingState />;
  const workspace = me.workspace;
  if (!workspace) return <NoWorkspaceState />;

  const dirty =
    name.trim() !== "" &&
    slug.trim() !== "" &&
    (name !== workspace.name || slug !== workspace.slug);

  async function save() {
    if (!workspace) return;
    setBusy(true);
    try {
      await unwrap(
        api.PATCH("/v1/workspaces/{workspace_id}", {
          params: { path: { workspace_id: workspace.id } },
          body: { name: name.trim(), slug: slug.trim() },
        }),
      );
      revalidator.revalidate(); // the switcher wears the name
      toast.success("Workspace saved.");
    } catch (e) {
      toastError(e, "Couldn't save the workspace.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <WorkspaceGeneralView
      name={name}
      slug={slug}
      canEdit={workspacePerms(workspace.role).rename}
      busy={busy}
      dirty={dirty}
      onName={setName}
      onSlug={setSlug}
      onSave={() => void save()}
    />
  );
}
