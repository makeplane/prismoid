# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every attachment door (`create_task`, a follow-up, a chat line) goes through `store_inputs`; a
native program is refused there by its first bytes, whatever its name or claimed media type."""

from __future__ import annotations

import pytest

from prismoid.errors import PolicyDenied
from prismoid.operations import tasks as task_ops
from prismoid.schemas.inputs import RawInput

_PE = bytearray(b"MZ" + b"\x00" * 126)
_PE[0x3C:0x40] = (0x40).to_bytes(4, "little")
_PE[0x40:0x44] = b"PE\x00\x00"

EXECUTABLES = {
    "tool.exe": bytes(_PE),  # Windows PE
    "tool": b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 56,  # Linux ELF
    "tool.bin": b"\xcf\xfa\xed\xfe\x07\x00\x00\x01" + b"\x00" * 56,  # Mach-O 64-bit
    "renamed.txt": bytes(_PE),  # the name is not the test
}


@pytest.fixture(autouse=True)
def no_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(task_ops.storage, "put", lambda data: "0" * 64)


@pytest.mark.parametrize("name", sorted(EXECUTABLES))
async def test_a_native_executable_is_refused_at_intake(name: str) -> None:
    with pytest.raises(PolicyDenied, match="programs can't be attached"):
        await task_ops.store_inputs([RawInput(filename=name, data=EXECUTABLES[name])])


@pytest.mark.parametrize(
    ("name", "data"),
    [("brands.csv", b"MZ Wallace,bags\n" * 8), ("notes.md", b"# notes\n"), ("dump.bin", b"\x00\x01" * 40)],
)
async def test_documents_and_data_still_attach(name: str, data: bytes) -> None:
    assert len(await task_ops.store_inputs([RawInput(filename=name, data=data)])) == 1
