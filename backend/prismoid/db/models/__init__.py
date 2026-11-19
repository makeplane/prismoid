# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every table in the product, in one import. Rows live here and nowhere else.

Alembic autogenerates against `Base.metadata`; a model nobody imports is invisible to it, and
the failure mode is not an error - it is a migration that silently omits the table you just
wrote. So this module imports them all, and `ALL_TABLES` is the roster, asserted against the
live database by `tests/integration/test_schema.py`.
"""

from __future__ import annotations

from prismoid.db.models import tenant as _tenant
from prismoid.db.models.actor import Actor, ActorKind, ActorStatus, Scope
from prismoid.db.models.admin_action import AdminAction
from prismoid.db.models.api_key import ApiKey
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.auth_throttle import AuthThrottle
from prismoid.db.models.beat import Beat
from prismoid.db.models.bot import BotOrigin, BotRecord, BotStatus
from prismoid.db.models.bot_channel import BotChannel
from prismoid.db.models.bot_template import BotTemplate
from prismoid.db.models.bot_template_install import BotTemplateInstall
from prismoid.db.models.call_log import CallLogEntry
from prismoid.db.models.capture import Capture, CaptureStatus
from prismoid.db.models.connector import Connector
from prismoid.db.models.conversation import Conversation
from prismoid.db.models.credential import Credential
from prismoid.db.models.device_grant import DeviceGrant
from prismoid.db.models.egress_allowance import EgressAllowance
from prismoid.db.models.email_verification import EmailVerification
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.feature import Feature
from prismoid.db.models.gate import Gate
from prismoid.db.models.inbox import InboxItem, InboxKind
from prismoid.db.models.machine import Machine
from prismoid.db.models.memory import (
    ACTIVE_CAP,
    Memory,
    MemoryKind,
    MemoryScope,
    MemoryStatus,
    MemoryUse,
)
from prismoid.db.models.message import Message
from prismoid.db.models.model_catalog import ModelCatalogEntry
from prismoid.db.models.password_reset import PasswordReset
from prismoid.db.models.plan import MAX_ATTEMPTS, PlanEdge, PlanEdit, PlanStep
from prismoid.db.models.plugin_install import PluginInstall
from prismoid.db.models.price_book import PriceBookEntry
from prismoid.db.models.project import Project, Role, at_least
from prismoid.db.models.project_invite import ProjectInvite
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.push_device import PushDevice
from prismoid.db.models.routine import Routine, RoutineEventCursor, RoutineRun
from prismoid.db.models.routing import RoutingTable
from prismoid.db.models.session import AuthSession
from prismoid.db.models.skill import EvalStatus, SkillOrigin, SkillRecord, SkillStatus
from prismoid.db.models.steer import Steer
from prismoid.db.models.stream_ticket import StreamTicket
from prismoid.db.models.task import TERMINAL, Task
from prismoid.db.models.usage import ProjectUsageDaily, UsageEvent
from prismoid.db.models.user import User
from prismoid.db.models.verification import Verification
from prismoid.db.models.workspace import (
    Workspace,
    WorkspaceKind,
    WorkspaceMember,
    WorkspaceRole,
    WorkspaceStatus,
)
from prismoid.db.models.workspace_invite import WorkspaceInvite

_tenant.install_stamping()

# The roster, in order. `tests/integration/test_schema.py` compares this list against the tables that
# actually exist in the migrated database - so a table in code but not in a migration (or the
# reverse) is a failing test, not a surprise in production.
ALL_TABLES: tuple[str, ...] = (
    # workspaces - the tenant, its people, its invites
    "workspaces",
    "workspace_members",
    "workspace_invites",
    # identity - M0.4a
    "actors",
    "users",
    "credentials",
    "auth_sessions",
    "api_keys",
    # the phones a login session registered for inbox push notifications (ADR-100)
    "push_devices",
    # projects, tasks, plans, events, gates - M0.4b
    "projects",
    "project_members",
    "project_invites",
    "tasks",
    "machines",
    # what a person granted a task on the device: folders and Docker
    "device_grants",
    "plan_steps",
    "plan_edges",
    "plan_edits",
    # the call log - a step attempt's logged model and tool calls, replayed instead of re-bought
    "call_log",
    "steers",
    "task_events",
    "gates",
    # deliverables and the human surfaces - M0.4c
    "artifacts",
    "verifications",
    "captures",
    "inbox_items",
    # the ledger, the catalog, the registry, the flags - M0.4d
    "usage_events",
    "price_book",
    "project_usage_daily",
    "model_catalog",
    "skills_registry",
    # bots - user-authored agents, the roster One routes across
    "bots_registry",
    "bot_templates",
    "bot_template_installs",
    "bot_channels",
    # conversations - talk, never work: the thread and its lines; a channel is a
    # transport for one of these (channel_messages retired, replaced by messages)
    "conversations",
    "messages",
    # plugin installs - the receipt of what an Agent Plugins bundle became
    "plugin_installs",
    "routing_table",
    "features",
    # observability - the worker's pulse (a stopped worker must be sayable)
    "system_beats",
    # memory & context - the ledger and its spend record
    "memories",
    "memory_uses",
    # connectors - external MCP servers a user's tasks can reach
    "connectors",
    # egress allowances - the owner's standing always-allow hosts for the browser
    "egress_allowances",
    # forgotten passwords - emailed single-use reset codes, hashed at rest
    "password_resets",
    # address verification - the emailed single-use code that proves an address before it invites
    "email_verifications",
    # the auth doors' burst buckets - login, signup and the reset pair, counted fleet-wide
    "auth_throttles",
    # stream tickets - a browser's single-use, seconds-long admission to one event stream
    "stream_tickets",
    # the operator's own flight recorder - every admin mutation, append-only (admin surface)
    "admin_actions",
    # routines - standing work: the object, its append-only fire ledger, and the event
    # matcher's watermark
    "routines",
    "routine_runs",
    "routine_event_cursor",
)

__all__ = [
    "ACTIVE_CAP",
    "ALL_TABLES",
    "MAX_ATTEMPTS",
    "TERMINAL",
    "Actor",
    "ActorKind",
    "ActorStatus",
    "AdminAction",
    "ApiKey",
    "Artifact",
    "AuthSession",
    "AuthThrottle",
    "Beat",
    "BotChannel",
    "BotOrigin",
    "BotRecord",
    "BotStatus",
    "BotTemplate",
    "BotTemplateInstall",
    "CallLogEntry",
    "Capture",
    "CaptureStatus",
    "Connector",
    "Conversation",
    "Credential",
    "DeviceGrant",
    "EgressAllowance",
    "EmailVerification",
    "EvalStatus",
    "Feature",
    "Gate",
    "InboxItem",
    "InboxKind",
    "Machine",
    "Memory",
    "MemoryKind",
    "MemoryScope",
    "MemoryStatus",
    "MemoryUse",
    "Message",
    "ModelCatalogEntry",
    "PasswordReset",
    "PlanEdge",
    "PlanEdit",
    "PlanStep",
    "PluginInstall",
    "PriceBookEntry",
    "Project",
    "ProjectInvite",
    "ProjectMember",
    "ProjectUsageDaily",
    "PushDevice",
    "Role",
    "Routine",
    "RoutineEventCursor",
    "RoutineRun",
    "RoutingTable",
    "Scope",
    "SkillOrigin",
    "SkillRecord",
    "SkillStatus",
    "Steer",
    "StreamTicket",
    "Task",
    "TaskEvent",
    "UsageEvent",
    "User",
    "Verification",
    "Workspace",
    "WorkspaceInvite",
    "WorkspaceKind",
    "WorkspaceMember",
    "WorkspaceRole",
    "WorkspaceStatus",
    "at_least",
]
