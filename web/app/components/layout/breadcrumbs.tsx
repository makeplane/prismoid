/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Fragment } from "react";
import { Link, useLocation, useMatches } from "react-router";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@makeplane/propel/components/breadcrumb";

// Static path segments → their display label. Anything else (an id) reads as "Detail".
const LABELS: Record<string, string> = {
  tasks: "Tasks",
  new: "New task",
  projects: "Projects",
  captures: "Captures",
  inbox: "Inbox",
  artifacts: "Artifact",
  connectors: "Connectors",
  "allowed-sites": "Allowed sites",
};

// Sections with no index route: an artifact is only ever reached THROUGH a task, so /artifacts
// does not exist - the crumb may name the section but must never link into a 404.
const UNLINKABLE = new Set(["artifacts"]);

function looksLikeId(seg: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(seg) || /^\d+$/.test(seg) || seg.length > 24;
}

function labelFor(seg: string): string {
  return LABELS[seg] ?? (looksLikeId(seg) ? "Detail" : seg.charAt(0).toUpperCase() + seg.slice(1));
}

/** A route names its own crumb by exporting `handle = { crumb: (loaderData) => string }` -
 *  how "Detail" becomes the task's title. Display truncates; the full name rides `title`.
 *  A route may instead name its WHOLE trail (everything after Home) with `trail` - how the
 *  artifact page crumbs back to its owning task instead of to a path segment with no route.
 *  Entries without `to` render as plain text. */
type CrumbHandle = {
  crumb?: (data: unknown) => string | null | undefined;
  trail?: (
    data: unknown,
    params: Record<string, string | undefined>,
  ) => { label: string; to?: string }[] | null | undefined;
};

const CRUMB_MAX = 44;

function crumbText(label: string): string {
  return label.length > CRUMB_MAX ? `${label.slice(0, CRUMB_MAX).trimEnd()}…` : label;
}

/** Breadcrumbs derived from the current path - Section / Detail, last crumb is the page.
 *  No Home crumb: the trail starts at the section ("Tasks › the task"), and on
 *  the landing there is no trail at all - Home is the sidebar's job. */
export function Breadcrumbs() {
  const { pathname } = useLocation();
  const matches = useMatches();
  const deepest = [...matches].reverse();
  const named = deepest
    .map((m) => (m.handle as CrumbHandle | undefined)?.crumb?.(m.loaderData))
    .find((label) => typeof label === "string" && label.trim().length > 0);
  const trail = deepest
    .map((m) => (m.handle as CrumbHandle | undefined)?.trail?.(m.loaderData, m.params))
    .find((t) => Array.isArray(t) && t.length > 0);
  const segments = pathname.split("/").filter(Boolean);
  const crumbs: { seg: string; label: string; to?: string }[] =
    trail?.map((c) => ({ seg: "", label: c.label, to: c.to })) ??
    segments.map((seg, i) => ({
      seg,
      label: i === segments.length - 1 && looksLikeId(seg) && named ? named : labelFor(seg),
      to: "/" + segments.slice(0, i + 1).join("/"),
    }));

  if (crumbs.length === 0) return null;
  // Propel's trail neither shrinks nor takes a className: at phone width the page's crumb gives way
  // here, before the topbar's actions leave the screen. Only the page's crumb shrinks - when the
  // section crumb shrank too, "Tasks" squeezed to a sliver under the title. On a phone the
  // section crumbs go entirely (the nav button beside the trail reaches them) so the title
  // keeps the room. Propel's page crumb is a flex span, and a flex box never draws an ellipsis
  // (the title was cut mid-letter): as a block it ends in "…", and leading-6 keeps its text on
  // the 24px crumb row's centre, level with "Tasks".
  return (
    <div className="min-w-0 max-md:[&_li:not(:last-child)]:hidden [&_li:last-child]:min-w-0 [&_li:last-child]:overflow-hidden [&_li:last-child>*]:max-w-full [&_li:last-child_span]:block [&_li:last-child_span]:truncate [&_li:last-child_span]:leading-6 [&_li:not(:last-child)]:shrink-0 [&_ol]:min-w-0">
      <Breadcrumb aria-label="Breadcrumb">
        <BreadcrumbList>
          {crumbs.map((crumb, i) => (
            <Fragment key={crumb.to ?? `${i}-${crumb.label}`}>
              {i > 0 && <BreadcrumbSeparator />}
              <BreadcrumbItem>
                {i === crumbs.length - 1 ? (
                  <BreadcrumbPage title={crumb.label} label={crumbText(crumb.label)} />
                ) : !crumb.to || UNLINKABLE.has(crumb.seg) ? (
                  // a named section with no route: a crumb's look, never a link
                  <span
                    title={crumb.label}
                    className="text-body-xs-medium text-tertiary inline-flex h-6 items-center px-1 whitespace-nowrap"
                  >
                    {crumbText(crumb.label)}
                  </span>
                ) : (
                  <BreadcrumbLink
                    render={<Link to={crumb.to} />}
                    title={crumb.label}
                    label={crumbText(crumb.label)}
                  />
                )}
              </BreadcrumbItem>
            </Fragment>
          ))}
        </BreadcrumbList>
      </Breadcrumb>
    </div>
  );
}
