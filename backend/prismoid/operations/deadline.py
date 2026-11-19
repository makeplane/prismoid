# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A model call's hard deadline: the receipt judge and the claim reader both end their wait with it.

`asyncio.wait_for` waits for a cancelled call to acknowledge, and a provider stream that ignores
cancellation once held a read 52 minutes past its budget; here the call is cancelled and abandoned at
the deadline, unmetered work at worst.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any


async def hard_timeout(coro: Coroutine[Any, Any, Any], seconds: float) -> Any:
    """The call's result, or `TimeoutError` at `seconds` with the call cancelled and abandoned."""
    task = asyncio.ensure_future(coro)
    done, _pending = await asyncio.wait({task}, timeout=seconds)
    if task in done:
        return task.result()
    task.cancel()
    # The orphan's eventual exception is retrieved, so the loop never logs it as unretrieved.
    task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
    raise TimeoutError(f"the model call exceeded {seconds:.0f}s - abandoned")
