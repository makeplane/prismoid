/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Section } from "~/components/kit";
import { DownloadCard } from "~/components/artifact/download-card";
import CollectionRenderer from "~/components/artifact/renderers/collection/collection-view";
import { FIXTURE_TEXT } from "~/components/artifact/renderers/collection/collection.fixtures";
import { type ArtifactOut as ArtifactOutShape } from "~/components/artifact/renderers/registry";
import { Specimen } from "./shared";

// The collection fixture, wired exactly as the renderer receives it in production.
const COLLECTION_ARTIFACT = {
  id: "ds-collection",
  type: "collection",
  title: "Q3 workstreams",
  filename: "q3.collection.json",
  render: "collection",
  sha256: "0".repeat(64),
  bytes: 2048,
  provenance: {},
  verdict: "pass",
  created_at: "2026-07-31T00:00:00Z",
} as ArtifactOutShape;
const COLLECTION_DOWNLOAD = {
  url: "#",
  filename: "q3.collection.json",
  sha256: "0".repeat(64),
  expires_in: 3600,
};

export function RenderersSection() {
  return (
    <section id="renderers">
      <Section title="Artifact renderers - dispatched on the wire's render hint">
        <div className="space-y-4">
          <Specimen label="collection" file="renderers/collection/collection-view.tsx">
            <div className="w-full">
              <CollectionRenderer
                artifact={COLLECTION_ARTIFACT}
                download={COLLECTION_DOWNLOAD}
                text={FIXTURE_TEXT}
              />
            </div>
          </Specimen>
          <Specimen label="invalid content" file="renderers/collection/collection-view.tsx">
            <div className="w-full">
              <CollectionRenderer
                artifact={COLLECTION_ARTIFACT}
                download={COLLECTION_DOWNLOAD}
                text="{broken"
              />
            </div>
          </Specimen>
          <Specimen label="download floor" file="artifact/download-card.tsx">
            <div className="w-full max-w-md">
              <DownloadCard
                artifact={{
                  ...COLLECTION_ARTIFACT,
                  type: "spreadsheet",
                  filename: "model.xlsx",
                  render: "none",
                }}
                download={{ ...COLLECTION_DOWNLOAD, filename: "model.xlsx" }}
              />
            </div>
          </Specimen>
        </div>
      </Section>
    </section>
  );
}
