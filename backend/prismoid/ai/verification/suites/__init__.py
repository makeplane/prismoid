# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The suites themselves - **each one runs inside a frame, not in this process.**

These modules are copied into a sandbox and executed there. They therefore import the standard
library and the frame-pinned parsers, nothing else: no `prismoid.*`, no database, no network. Do not import them from
application code and do not call their functions directly - `pipeline.run_suite()` ships them into
a frame and reads their JSON back.

The rule looks fussy for a document (it is only text) and is non-negotiable for the rest: the
spreadsheet suite opens a workbook the model wrote, and the code suite *builds and runs a program*
the model wrote. Those do not happen in the process that holds the database connection.
"""
