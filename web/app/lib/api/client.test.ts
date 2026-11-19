/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// unwrap: an openapi-fetch result to its data, or an ApiError. A 204 is the API's own "done,
// nothing to say" (revoking an invite link, removing a workspace member), so it is success with no
// data - not an error named "No Content" that the caller then reports to the person as a failure.
import { describe, expect, it } from "vitest";
import { ApiError, unwrap } from "./client";

const result = (status: number, data?: unknown, error?: unknown) =>
  Promise.resolve({
    data,
    error,
    response: new Response(null, { status }),
  });

describe("unwrap", () => {
  it("returns the data of a 200", async () => {
    expect(await unwrap(result(200, { id: "x" }))).toEqual({ id: "x" });
  });

  it("treats a 204 as success with nothing to return", async () => {
    await expect(unwrap(result(204))).resolves.toBeUndefined();
  });

  it("still refuses a 200 that carried no body at all", async () => {
    await expect(unwrap(result(200))).rejects.toBeInstanceOf(ApiError);
  });

  it("raises the server's own words for a failure", async () => {
    const failed = unwrap(
      result(404, undefined, {
        error: { code: "not_found", message: "this invite is invalid or has expired" },
      }),
    );
    await expect(failed).rejects.toMatchObject({
      status: 404,
      code: "not_found",
      message: "this invite is invalid or has expired",
    });
  });

  it("a field error shows the reason, without pydantic's frame", async () => {
    // Regression: the login form printed "value is not a valid email address: The part after
    // the @-sign is a special-use or reserved name…" under the email field.
    const failed = unwrap(
      result(422, undefined, {
        error: {
          code: "validation_failed",
          message: "the request body is not valid",
          fields: {
            email:
              "value is not a valid email address: The part after the @-sign is a special-use or reserved name that cannot be used with email.",
            name: "Value error, a name is required",
            password: "String should have at least 12 characters",
          },
        },
      }),
    );
    await expect(failed).rejects.toMatchObject({
      fields: {
        email:
          "The part after the @-sign is a special-use or reserved name that cannot be used with email.",
        name: "a name is required",
        password: "String should have at least 12 characters",
      },
    });
  });

  it("carries what a stale steer's author missed (ADR-064)", async () => {
    const missed = [
      { kind: "steer", author: "Bob", text: "use euros", epoch: 3, state: "accepted" },
      { kind: "turn", author: "Ada", epoch: 4 },
    ];
    const stale = unwrap(
      result(409, undefined, {
        error: {
          code: "stale_steer",
          message: "Bob steered while you were typing: 'use euros'",
          request_id: "r1",
          missed,
        },
      }),
    );
    await expect(stale).rejects.toMatchObject({ status: 409, code: "stale_steer", missed });
  });

  it("leaves missed unset on every other error", async () => {
    const failed = unwrap(result(409, undefined, { error: { code: "conflict", message: "no" } }));
    await expect(failed).rejects.toMatchObject({ code: "conflict", missed: undefined });
  });
});
