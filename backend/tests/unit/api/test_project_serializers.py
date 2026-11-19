# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Project read always names its owner (ADR-062): a serializer that let a caller forget the
owner would answer a blank name that looks like an owner with no profile."""

import inspect

from prismoid.api.serializers.projects import project_out


def test_project_out_cannot_be_called_without_naming_the_owner() -> None:
    owner = inspect.signature(project_out).parameters["owner"]
    assert owner.default is inspect.Parameter.empty, "the owner is required, never defaulted to blank"
