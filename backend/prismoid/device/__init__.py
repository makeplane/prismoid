# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The device helper: the one process that touches the user's computer.

It imports only `prismoid.schemas`, `prismoid.conf` and `prismoid.egress_relay` (the layering contract); the
worker reaches it over a Unix socket and never the other way round.
"""
