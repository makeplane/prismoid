/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The operator's own receipts: every admin action, append-only, newest first. Reading is
// the whole page - audit rows are written by the backend as verbs happen, never from here.
// The rendering is the shared AuditRows (components/admin/audit-rows), unscoped here; the
// user detail page's History tab is the same rows filtered to one subject.

import { AuditRows } from "~/components/admin/audit-rows";

export function meta() {
  return [{ title: "Audit · Admin · Prismoid" }];
}

export default function AdminAudit() {
  return <AuditRows />;
}

export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
