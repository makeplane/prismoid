# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""One vocabulary per column (ADR-078): a grant's kind and mode and a task's driver are enums at the
door and on the row, and stay VARCHAR in Postgres, so a new word needs no migration."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import pytest
from sqlalchemy import String, inspect
from sqlalchemy.dialects import postgresql

from prismoid.api.serializers.device_grants import DeviceGrantOut
from prismoid.api.serializers.gates import GateOut
from prismoid.db.base import StrEnumColumn
from prismoid.db.models.device_grant import DeviceGrant
from prismoid.db.models.task import Task
from prismoid.schemas.device import DEVICE_DRIVER
from prismoid.schemas.device_grants import GrantKind, GrantMode
from prismoid.schemas.machines import FrameDriverName


class Colour(StrEnum):
    RED = "red"


COLOUR = StrEnumColumn(Colour, 8)
PG = postgresql.dialect()


def test_a_vocabulary_column_is_a_varchar_of_its_length() -> None:
    impl = COLOUR.load_dialect_impl(PG)
    assert isinstance(impl, String) and impl.length == 8


def test_the_word_is_written_and_read_through_the_enum() -> None:
    assert COLOUR.process_bind_param(Colour.RED, PG) == "red"
    assert COLOUR.process_bind_param("red", PG) == "red"
    assert COLOUR.process_result_value("red", PG) is Colour.RED
    assert COLOUR.process_bind_param(None, PG) is None
    assert COLOUR.process_result_value(None, PG) is None


def test_a_word_outside_the_enum_raises_both_ways() -> None:
    with pytest.raises(ValueError, match="blue"):
        COLOUR.process_bind_param("blue", PG)
    with pytest.raises(ValueError, match="blue"):
        COLOUR.process_result_value("blue", PG)


@pytest.mark.parametrize(
    ("column", "enum", "length"),
    [
        (inspect(DeviceGrant).columns.kind, GrantKind, 10),
        (inspect(DeviceGrant).columns.mode, GrantMode, 2),
        (inspect(Task).columns.machine_driver, FrameDriverName, 20),
    ],
)
def test_the_columns_carry_their_enums(column: Any, enum: type[StrEnum], length: int) -> None:
    assert isinstance(column.type, StrEnumColumn) and column.type.enum is enum
    assert column.type.length == length


def test_the_wire_says_the_vocabulary() -> None:
    fields = DeviceGrantOut.model_json_schema()
    spelled = {f"{k}": v.get("enum") for k, v in fields["$defs"].items()}
    assert spelled["GrantKind"] == ["folder", "docker"] and spelled["GrantMode"] == ["ro", "rw"]
    assert "GrantKind" in str(fields["properties"]["kind"]) and "GrantMode" in str(
        fields["properties"]["mode"]
    )
    gate = GateOut.model_json_schema()
    assert "FrameDriverName" in str(gate["properties"]["machine_driver"])


def test_one_spelling_of_the_device_driver() -> None:
    assert DEVICE_DRIVER is FrameDriverName.DEVICE and DEVICE_DRIVER == "device"
