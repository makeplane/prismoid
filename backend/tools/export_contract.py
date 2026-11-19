# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Freeze the frontend contract: the OpenAPI spec and the TaskEvent union JSON Schema.

These two files are what a frontend generates its types from on day one — `openapi-typescript` over the
spec for every request/response, and the event schema for the SSE stream union. Exporting them from the
*running* app (not hand-maintained) means they cannot drift from the code: re-run `just export-contract`
and diff. The `X-Prismoid-Protocol: 2` header the middleware already sets is the third leg — it tells the
frontend which contract version it is talking to.
"""

from __future__ import annotations

import json
import pathlib

from pydantic import TypeAdapter

from prismoid.api.app import create_app
from prismoid.schemas.collection import Collection
from prismoid.schemas.events import TaskEvent

OUT = pathlib.Path("../docs/api")  # the root docs/ — the exporter runs from backend/


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    app = create_app()
    spec = app.openapi()
    (OUT / "openapi.json").write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")

    # The wire union as one `oneOf` on `type` — the flat discriminated union the frontend switches on.
    event_schema = TypeAdapter(TaskEvent).json_schema()
    (OUT / "events.schema.json").write_text(json.dumps(event_schema, indent=2, sort_keys=True) + "\n")

    # The `collection` artifact's CONTENT schema. Not an API shape — the bytes ride
    # inside an artifact — but the web's zod façade and the organize skill's reference both pin
    # against this file, so it is exported from the one pydantic definition like everything else.
    collection_schema = Collection.model_json_schema()
    (OUT / "collection.schema.json").write_text(
        json.dumps(collection_schema, indent=2, sort_keys=True) + "\n"
    )

    print(f"  openapi {spec['openapi']} · info.version {spec['info']['version']} → {OUT / 'openapi.json'}")
    variants = len(event_schema.get("oneOf", event_schema.get("anyOf", [])))
    print(f"  TaskEvent union · {variants} variants → {OUT / 'events.schema.json'}")
    print(f"  Collection content schema (v1) → {OUT / 'collection.schema.json'}")


if __name__ == "__main__":
    main()
