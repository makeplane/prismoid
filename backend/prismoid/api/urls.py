# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every route in the product. One line per route. Read top to bottom = the API.

SCREEN 1 of the three. Complete, and every route it lists is *implemented*
- the named-stub countdown reached zero. A route table that only lists what is finished is not
a screen; it is a changelog - so this always listed the whole surface, stubs and all, until there
were none.

The `fmt: off` below is deliberate and load-bearing. This table is **aligned in columns on
purpose**: the whole value of SCREEN 1 is that you can read the path column, the handler column
and the method column *down* the page and see the shape of the API. A formatter that reflows it
into ragged one-line calls destroys exactly the property the screen exists for.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from prismoid.api import stream
from prismoid.api.endpoints import (
    admin,
    admin_audit,
    admin_bots,
    admin_health,
    admin_projects,
    admin_routines,
    admin_spend,
    admin_tasks,
    admin_users,
    admin_workspaces,
    artifacts,
    auth,
    blobs,
    bot_templates,
    bots,
    capture,
    channels,
    connectors,
    conversations,
    device_grants,
    egress_allowances,
    gates,
    health,
    inbox,
    machines,
    memories,
    models,
    plugins,
    presence,
    project_invites,
    projects,
    push_devices,
    registry,
    routines,
    screen,
    skills,
    stream_tickets,
    tasks,
    watch,
    workspaces,
)
from prismoid.api.serializers.conversations import ChatStreamOut


# fmt: off
def _mount_auth(r: APIRouter) -> None:
    # auth (unauthenticated besides /healthz + the OAuth callback: signup, login, and the
    # password-reset pair, whose auth is the emailed single-use `cr_` code)
    r.add_api_route("/auth/config",            auth.auth_config,       methods=["GET"])
    r.add_api_route("/auth/signup",            auth.signup,            methods=["POST"], status_code=201)
    r.add_api_route("/auth/login",             auth.login,             methods=["POST"])
    r.add_api_route("/auth/forgot-password",   auth.forgot_password,   methods=["POST"], status_code=202)
    r.add_api_route("/auth/reset-password",    auth.reset_password,    methods=["POST"])
    r.add_api_route("/auth/logout",            auth.logout,            methods=["POST"])
    r.add_api_route("/auth/me",                auth.me,                methods=["GET"])
    r.add_api_route("/auth/me",                auth.update_me,         methods=["PATCH"])
    r.add_api_route("/auth/me/verify-email",   auth.verify_email,      methods=["POST"])
    r.add_api_route("/auth/me/verification",   auth.resend_verification, methods=["POST"], status_code=202)
    r.add_api_route("/auth/session/workspace",       auth.switch_session_workspace, methods=["PUT"])
    r.add_api_route("/auth/api-keys",          auth.create_api_key,    methods=["POST"], status_code=201)
    r.add_api_route("/auth/api-keys",          auth.list_api_keys,     methods=["GET"])
    r.add_api_route("/auth/api-keys/{key_id}", auth.revoke_api_key,    methods=["DELETE"])
    # push devices - a phone registers for the inbox as push notifications (ADR-100); a login session only
    r.add_api_route("/push/devices",             push_devices.register_device, methods=["POST"], status_code=201)
    r.add_api_route("/push/devices/{device_id}", push_devices.delete_device,   methods=["DELETE"], status_code=204)


def _mount_admin(r: APIRouter) -> None:
    # admin - the operator surface: users, the task console, spend, health, the audit trail.
    # Every route AdminDep; every mutation writes an admin_actions row (operations/admin_ops).
    r.add_api_route("/admin/users",                        admin_users.list_users,          methods=["GET"])
    r.add_api_route("/admin/users/{id}",                   admin_users.get_user,            methods=["GET"])
    r.add_api_route("/admin/users/{id}/suspend",           admin_users.suspend_user,        methods=["POST"])
    r.add_api_route("/admin/users/{id}/unsuspend",         admin_users.unsuspend_user,      methods=["POST"])
    r.add_api_route("/admin/users/{id}/grant-admin",       admin_users.grant_admin,         methods=["POST"])
    r.add_api_route("/admin/users/{id}/revoke-admin",      admin_users.revoke_admin,        methods=["POST"])
    r.add_api_route("/admin/users/{id}/revoke-sessions",   admin_users.revoke_user_sessions, methods=["POST"])
    r.add_api_route("/admin/users/{id}/spend",             admin_spend.user_spend_daily,    methods=["GET"])
    r.add_api_route("/admin/users/{id}/sessions",          admin_users.list_user_sessions,  methods=["GET"])
    r.add_api_route("/admin/users/{id}/keys",              admin_users.list_user_keys,      methods=["GET"])
    r.add_api_route("/admin/users/{id}/sessions/{session_id}/revoke", admin_users.revoke_user_session, methods=["POST"])
    r.add_api_route("/admin/users/{id}/keys/{key_id}/revoke",         admin_users.revoke_user_key,     methods=["POST"])
    r.add_api_route("/admin/workspaces",                         admin_workspaces.list_workspaces,            methods=["GET"])
    r.add_api_route("/admin/workspaces/{workspace_id}",                admin_workspaces.get_workspace,              methods=["GET"])
    r.add_api_route("/admin/tasks",                        admin_tasks.list_tasks,          methods=["GET"])
    r.add_api_route("/admin/tasks/{task_id}",              admin_tasks.get_task,            methods=["GET"])
    r.add_api_route("/admin/tasks/{task_id}/events",       admin_tasks.get_task_events,     methods=["GET"])
    r.add_api_route("/admin/tasks/{task_id}/cancel",       admin_tasks.cancel_task,         methods=["POST"])
    r.add_api_route("/admin/spend/summary",                admin_spend.spend_summary,       methods=["GET"])
    r.add_api_route("/admin/spend/daily",                  admin_spend.spend_daily,         methods=["GET"])
    r.add_api_route("/admin/health",                       admin_health.get_health,         methods=["GET"])
    r.add_api_route("/admin/alerts",                       admin_health.get_alerts,         methods=["GET"])
    r.add_api_route("/admin/models/doctor",                admin_health.get_doctor,         methods=["GET"])
    r.add_api_route("/admin/audit",                        admin_audit.list_audit,          methods=["GET"])
    r.add_api_route("/admin/projects",                       admin_projects.list_projects,        methods=["GET"])
    r.add_api_route("/admin/projects/{project_id}",            admin_projects.get_project,          methods=["GET"])
    r.add_api_route("/admin/bots",                         admin_bots.list_bots,            methods=["GET"])
    r.add_api_route("/admin/bots/{bot_id}",                admin_bots.get_bot,              methods=["GET"])
    r.add_api_route("/admin/templates/{template_id}/unlist", bot_templates.admin_unlist_template, methods=["POST"])
    # standing work, observed + the audited stop
    r.add_api_route("/admin/routines",                     admin_routines.list_routines,    methods=["GET"])
    r.add_api_route("/admin/routines/{routine_id}/pause",  admin_routines.pause_routine,    methods=["POST"])


def _mount_inbox_gates(r: APIRouter) -> None:
    # inbox - what the system told a person; an item reads when they read it, read all, or open
    # its task. gates - the questions waiting on a person, and the three-action resolve.
    r.add_api_route("/inbox",                  inbox.list_inbox,       methods=["GET"])
    r.add_api_route("/inbox/{item_id}/read",   inbox.mark_read,        methods=["POST"])
    r.add_api_route("/inbox/read-all",         inbox.mark_all_read,    methods=["POST"])
    r.add_api_route("/inbox/tasks/{task_id}/read", inbox.mark_task_read, methods=["POST"])
    r.add_api_route("/gates",                  gates.list_gates,       methods=["GET"])
    r.add_api_route("/gates/{gate_id}",        gates.get_gate,         methods=["GET"])
    r.add_api_route("/gates/{gate_id}/resolve",gates.resolve_gate,     methods=["POST"])


def _mount_connectors(r: APIRouter) -> None:
    # connectors - external MCP servers a user's tasks can reach. The oauth callback is
    # the fourth unauthenticated route: the single-use hashed state token IS its auth.
    r.add_api_route("/connectors",                        connectors.create_connector,    methods=["POST"], status_code=201)
    r.add_api_route("/connectors",                        connectors.list_connectors,     methods=["GET"])
    r.add_api_route("/connectors/catalog",                connectors.get_catalog,         methods=["GET"])  # before {connector_id}
    r.add_api_route("/connectors/connect",                connectors.connect_connector,   methods=["POST"], status_code=201)  # before {connector_id}
    r.add_api_route("/connectors/oauth/callback",         connectors.oauth_callback,      methods=["GET"])  # before {connector_id}
    r.add_api_route("/connectors/{connector_id}",         connectors.get_connector,       methods=["GET"])
    r.add_api_route("/connectors/{connector_id}",         connectors.rename_connector,    methods=["PATCH"])
    r.add_api_route("/connectors/{connector_id}",         connectors.delete_connector,    methods=["DELETE"])
    r.add_api_route("/connectors/{connector_id}/authorize", connectors.authorize_connector, methods=["POST"])
    r.add_api_route("/connectors/{connector_id}/oauth/finish", connectors.finish_app_sign_in, methods=["POST"])
    r.add_api_route("/connectors/{connector_id}/probe",   connectors.probe_connector,     methods=["POST"])
    r.add_api_route("/connectors/{connector_id}/policies", connectors.set_policies,       methods=["PATCH"])
    r.add_api_route("/connectors/{connector_id}/disable", connectors.disable_connector,   methods=["POST"])
    r.add_api_route("/connectors/{connector_id}/enable",  connectors.enable_connector,    methods=["POST"])


def _mount_stream_tickets(app: FastAPI) -> None:
    # The single-use `et_` tickets a browser opens an event stream with (its EventSource cannot
    # set the Authorization header every other client sends), then the conversation stream.
    app.add_api_route("/v1/tasks/{task_id}/events/ticket", stream_tickets.mint_task_stream_ticket, methods=["POST"])
    app.add_api_route("/v1/conversations/{conversation_id}/events/ticket", stream_tickets.mint_conversation_stream_ticket, methods=["POST"])
    # The conversation stream: `message` rows (id: seq) + the reply's chat.* deltas.
    # `responses` carries the frames' shapes into the frozen contract; the body is text/event-stream.
    app.add_api_route("/v1/conversations/{conversation_id}/events", stream.conversation_sse, methods=["GET"],
                      responses={200: {"model": ChatStreamOut, "content": {"text/event-stream": {}}}})


def mount(app: FastAPI) -> None:
    r = APIRouter(prefix="/v1")
    _mount_auth(r)
    # workspaces - the tenant; the session's active workspace switches at /auth/session/workspace
    r.add_api_route("/workspaces",                   workspaces.list_my_workspaces,      methods=["GET"])
    r.add_api_route("/workspaces",                   workspaces.create_workspace,        methods=["POST"], status_code=201)
    r.add_api_route("/workspaces/invites/accept",    workspaces.accept_workspace_invite, methods=["POST"])  # before {workspace_id}
    r.add_api_route("/workspaces/{workspace_id}",          workspaces.get_workspace,           methods=["GET"])
    r.add_api_route("/workspaces/{workspace_id}",          workspaces.update_workspace,        methods=["PATCH"])
    r.add_api_route("/workspaces/{workspace_id}/members",  workspaces.list_workspace_members,  methods=["GET"])
    r.add_api_route("/workspaces/{workspace_id}/members/me/leave", workspaces.leave_workspace, methods=["POST"], status_code=204)
    r.add_api_route("/workspaces/{workspace_id}/members/{actor_id}", workspaces.update_workspace_member, methods=["PATCH"])
    r.add_api_route("/workspaces/{workspace_id}/members/{actor_id}", workspaces.remove_workspace_member, methods=["DELETE"], status_code=204)
    r.add_api_route("/workspaces/{workspace_id}/transfer", workspaces.transfer_workspace,      methods=["POST"])
    r.add_api_route("/workspaces/{workspace_id}/invites",  workspaces.list_workspace_invites,  methods=["GET"])
    r.add_api_route("/workspaces/{workspace_id}/invites",  workspaces.create_workspace_invite, methods=["POST"], status_code=201)
    r.add_api_route("/workspaces/{workspace_id}/invites/{invite_id}", workspaces.revoke_workspace_invite, methods=["DELETE"], status_code=204)
    # tasks
    r.add_api_route("/tasks",                  tasks.create_task,      methods=["POST"], status_code=202)
    r.add_api_route("/tasks",                  tasks.list_tasks,       methods=["GET"])
    r.add_api_route("/tasks/counts",           tasks.count_tasks,      methods=["GET"])  # before {task_id}
    r.add_api_route("/tasks/{task_id}",        tasks.get_task,         methods=["GET"])
    r.add_api_route("/tasks/{task_id}",        tasks.update_task,      methods=["PATCH"])
    r.add_api_route("/tasks/{task_id}",        tasks.delete_task,      methods=["DELETE"])
    r.add_api_route("/tasks/{task_id}/cancel", tasks.cancel_task,      methods=["POST"])
    r.add_api_route("/tasks/{task_id}/archive",   tasks.archive_task,   methods=["POST"])
    r.add_api_route("/tasks/{task_id}/unarchive", tasks.unarchive_task, methods=["POST"])
    r.add_api_route("/tasks/{task_id}/move",   tasks.move_task,        methods=["POST"])
    r.add_api_route("/tasks/{task_id}/budget", tasks.set_task_budget,  methods=["PUT"])
    r.add_api_route("/tasks/{task_id}/steer",  tasks.steer_task,       methods=["POST"], status_code=202)
    r.add_api_route("/tasks/{task_id}/steers/{command_id}/retract", tasks.retract_steer, methods=["POST"])
    r.add_api_route("/tasks/{task_id}/replan", tasks.replan_task,      methods=["POST"])
    r.add_api_route("/tasks/{task_id}/pause",  tasks.pause_task,       methods=["POST"])
    r.add_api_route("/tasks/{task_id}/resume", tasks.resume_task,      methods=["POST"])
    r.add_api_route("/tasks/{task_id}/follow_up", tasks.follow_up_task, methods=["POST"], status_code=202)
    r.add_api_route("/tasks/{task_id}/retry", tasks.retry_task, methods=["POST"], status_code=202)
    r.add_api_route("/tasks/{task_id}/feedback", tasks.feedback_task, methods=["POST"], status_code=201)
    r.add_api_route("/tasks/{task_id}/fork", tasks.fork_task, methods=["POST"], status_code=202)
    r.add_api_route("/tasks/{task_id}/machine/sleep", machines.sleep_machine, methods=["POST"], status_code=202)
    r.add_api_route("/tasks/{task_id}/machine/reset", machines.reset_machine, methods=["POST"], status_code=202)
    r.add_api_route("/tasks/{task_id}/presence", presence.post_presence, methods=["POST"], status_code=204)
    r.add_api_route("/tasks/{task_id}/screen/control", screen.claim_screen_control, methods=["POST"])
    r.add_api_route("/tasks/{task_id}/screen/control/renew", screen.renew_screen_control, methods=["POST"])
    r.add_api_route("/tasks/{task_id}/screen/control", screen.release_screen_control, methods=["DELETE"], status_code=204)
    r.add_api_route("/tasks/{task_id}/screen/ticket", screen.mint_screen_ticket, methods=["POST"])
    r.add_api_route("/tasks/{task_id}/usage",  tasks.get_task_usage,   methods=["GET"])
    # projects · capture · inbox · gates · artifacts
    r.add_api_route("/projects",                 projects.create_project,    methods=["POST"], status_code=201)
    r.add_api_route("/projects",                 projects.list_projects,     methods=["GET"])
    # project join links - accept before the {project_id} routes
    r.add_api_route("/projects/invites/accept",                   project_invites.accept_project_invite, methods=["POST"])
    r.add_api_route("/projects/{project_id}/invites",               project_invites.list_project_invites,  methods=["GET"])
    r.add_api_route("/projects/{project_id}/invites",               project_invites.create_project_invite, methods=["POST"], status_code=201)
    r.add_api_route("/projects/{project_id}/invites/{project_invite_id}", project_invites.revoke_project_invite, methods=["DELETE"], status_code=204)
    r.add_api_route("/projects/{project_id}",      projects.get_project,       methods=["GET"])
    r.add_api_route("/projects/{project_id}",      projects.update_project,    methods=["PATCH"])
    r.add_api_route("/projects/{project_id}/usage",projects.get_project_usage, methods=["GET"])
    r.add_api_route("/projects/{project_id}/members",            projects.add_member,           methods=["POST"], status_code=201)
    r.add_api_route("/projects/{project_id}/members",            projects.list_members,         methods=["GET"])
    r.add_api_route("/projects/{project_id}/members/{actor_id}", projects.change_role,          methods=["PATCH"])
    r.add_api_route("/projects/{project_id}/members/{actor_id}", projects.remove_member,        methods=["DELETE"])
    r.add_api_route("/projects/{project_id}/artifacts",          projects.list_project_artifacts, methods=["GET"])
    r.add_api_route("/projects/{project_id}/transfer",           projects.transfer_project,       methods=["POST"])
    r.add_api_route("/capture",                capture.create_capture, methods=["POST"], status_code=201)
    r.add_api_route("/capture",                capture.list_captures,  methods=["GET"])
    r.add_api_route("/capture/{capture_id}/promote", capture.promote_capture, methods=["POST"])
    _mount_inbox_gates(r)
    r.add_api_route("/artifacts/{artifact_id}",              artifacts.get_artifact,      methods=["GET"])
    r.add_api_route("/artifacts/{artifact_id}/download",     artifacts.download_artifact, methods=["GET"])
    r.add_api_route("/artifacts/{artifact_id}/verification", artifacts.get_verification,  methods=["GET"])
    r.add_api_route("/artifacts/{artifact_id}/revise",       artifacts.revise_artifact,   methods=["POST"], status_code=201)
    r.add_api_route("/artifacts/{artifact_id}/restore",      artifacts.restore_artifact,  methods=["POST"], status_code=201)
    r.add_api_route("/artifacts/{artifact_id}/label",        artifacts.label_artifact,    methods=["POST"])
    r.add_api_route("/artifacts/{artifact_id}/revisions",    artifacts.list_revisions,    methods=["GET"])
    r.add_api_route("/artifacts/{artifact_id}/preview",      artifacts.get_preview,       methods=["GET"])
    r.add_api_route("/artifacts/{artifact_id}/rendition",    artifacts.get_rendition,     methods=["GET"])
    # blobs - local mode's download door: no Actor, the HMAC `sig` in its query is the auth; 404 off `fs`
    r.add_api_route("/blobs/{digest}",         blobs.get_blob,         methods=["GET"])
    # memory - the context ledger: scoped, attributed, retire-not-delete
    r.add_api_route("/memories",                     memories.create_memory, methods=["POST"], status_code=201)
    r.add_api_route("/memories",                     memories.list_memories, methods=["GET"])
    r.add_api_route("/memories/{memory_id}",         memories.get_memory,    methods=["GET"])
    r.add_api_route("/memories/{memory_id}",         memories.update_memory, methods=["PATCH"])
    r.add_api_route("/memories/{memory_id}/retire",  memories.retire_memory, methods=["POST"])
    _mount_connectors(r)
    # egress allowances - the owner's standing always-allow hosts for the browser
    r.add_api_route("/egress-allowances",              egress_allowances.list_egress_allowances,   methods=["GET"])
    r.add_api_route("/egress-allowances",              egress_allowances.add_egress_allowance,     methods=["POST"], status_code=201)
    r.add_api_route("/egress-allowances/{allowance_id}", egress_allowances.revoke_egress_allowance, methods=["DELETE"])
    # device grants - a task on this computer's folders and Docker, as a person grants them
    r.add_api_route("/device/grants",                  device_grants.list_device_grants,  methods=["GET"])
    r.add_api_route("/device/grants",                  device_grants.add_device_grant,    methods=["POST"], status_code=201)
    r.add_api_route("/device/grants/{grant_id}",       device_grants.revoke_device_grant, methods=["DELETE"])
    # skills - user-editable, draft → test → publish
    r.add_api_route("/skills",                       skills.create_skill,   methods=["POST"], status_code=201)
    r.add_api_route("/skills",                       skills.list_skills,    methods=["GET"])
    r.add_api_route("/skills/{name}",                skills.get_skill,      methods=["GET"])
    r.add_api_route("/skills/{name}",                skills.update_draft,   methods=["PUT"])
    r.add_api_route("/skills/{name}/publish",        skills.publish_skill,  methods=["POST"])
    r.add_api_route("/skills/{name}/versions",       skills.list_versions,  methods=["GET"])
    # bots - user-authored agents; the roster One routes across
    r.add_api_route("/bots",                         bots.create_bot,       methods=["POST"], status_code=201)
    r.add_api_route("/bots",                         bots.list_bots,        methods=["GET"])
    r.add_api_route("/bots/{name}",                  bots.get_bot,          methods=["GET"])
    r.add_api_route("/bots/{name}",                  bots.update_draft,     methods=["PUT"])
    r.add_api_route("/bots/{name}",                  bots.set_face,         methods=["PATCH"])
    r.add_api_route("/bots/{name}/draft",            bots.draft_bot,        methods=["POST"], status_code=202)
    r.add_api_route("/bots/{name}/test",             bots.test_bot,         methods=["POST"], status_code=202)
    r.add_api_route("/bots/{name}/publish",          bots.publish_bot,      methods=["POST"])
    r.add_api_route("/bots/{name}/archive",          bots.archive_bot,      methods=["POST"])
    r.add_api_route("/bots/{name}/versions",         bots.list_versions,    methods=["GET"])
    r.add_api_route("/bots/{name}/pin",              bots.set_pin,          methods=["PUT"])
    r.add_api_route("/bots/{name}/runs",             bots.list_runs,        methods=["GET"])
    r.add_api_route("/bots/{name}/artifacts",        bots.list_bot_artifacts, methods=["GET"])
    r.add_api_route("/bots/{name}/routines",         bots.list_bot_routines, methods=["GET"])
    # bot templates: a bot travels as a link. GET /templates/bots/{slug} is the
    # FIFTH route-level auth exception (the cr_/cn_/rt_ family): the ANONYMOUS share-link
    # preview - read-only, single-purpose, per-IP throttled, 404-on-miss, noindex.
    r.add_api_route("/templates/bots",                  bot_templates.publish_template,   methods=["POST"], status_code=201)
    r.add_api_route("/templates/bots",                  bot_templates.list_my_templates,  methods=["GET"])
    r.add_api_route("/templates/bots/installed",        bot_templates.list_installed,     methods=["GET"])  # before {slug}
    r.add_api_route("/templates/bots/{slug}",           bot_templates.get_template,       methods=["GET"])
    r.add_api_route("/templates/bots/{slug}/install",   bot_templates.install_template,   methods=["POST"], status_code=201)
    r.add_api_route("/templates/bots/{slug}/unpublish", bot_templates.unpublish_template, methods=["POST"])
    r.add_api_route("/bots/{name}/stats",            bots.bot_stats,        methods=["GET"])
    r.add_api_route("/bots/{name}/access",           bots.bot_access,       methods=["GET"])
    # conversations: talk with a bot - executes nothing; `send` answers 202 with the
    # door's verdict, the worker replies on the thread, work mints an ordinary Task. Replaces
    # the /bots/{name}/chat pair (the old DM-as-task lane, removed).
    r.add_api_route("/conversations",                                 conversations.create_conversation, methods=["POST"], status_code=201)
    r.add_api_route("/conversations",                                 conversations.list_conversations,  methods=["GET"])
    r.add_api_route("/conversations/{conversation_id}",               conversations.get_conversation,    methods=["GET"])
    r.add_api_route("/conversations/{conversation_id}",               conversations.update_conversation, methods=["PATCH"])
    r.add_api_route("/conversations/{conversation_id}/messages",      conversations.list_messages,       methods=["GET"])
    r.add_api_route("/conversations/{conversation_id}/messages",      conversations.send_message,        methods=["POST"], status_code=202)
    r.add_api_route("/conversations/{conversation_id}/proposals/{seq}/start",   conversations.start_proposal,   methods=["POST"])
    r.add_api_route("/conversations/{conversation_id}/proposals/{seq}/decline", conversations.decline_proposal, methods=["POST"])
    r.add_api_route("/conversations/{conversation_id}/messages/{seq}/save",     conversations.save_reply,       methods=["POST"], status_code=202)  # a reply → a file, one tap
    # channels: a bot's external doors. The /channels/{id}/messages PAIR is auth
    # exception #6 (the rt_ family): its Bearer is the channel's own bc_ token - it starts and
    # reads THAT channel's turns and nothing else. Everything else is owner CRUD.
    r.add_api_route("/bots/{name}/channels",            channels.create_bot_channel,  methods=["POST"], status_code=201)
    r.add_api_route("/bots/{name}/channels",            channels.list_bot_channels,   methods=["GET"])
    r.add_api_route("/channels/{channel_id}",           channels.update_channel,      methods=["PATCH"])
    r.add_api_route("/channels/{channel_id}",           channels.remove_channel,      methods=["DELETE"])
    r.add_api_route("/channels/{channel_id}/token",     channels.rotate_channel_token, methods=["POST"])
    r.add_api_route("/channels/{channel_id}/disable",   channels.disable_channel,     methods=["POST"])
    r.add_api_route("/channels/{channel_id}/enable",    channels.enable_channel,      methods=["POST"])
    r.add_api_route("/channels/{channel_id}/log",       channels.channel_log,         methods=["GET"])
    r.add_api_route("/channels/{channel_id}/messages",  channels.post_channel_message, methods=["POST"], status_code=202)
    r.add_api_route("/channels/{channel_id}/messages",  channels.get_channel_messages, methods=["GET"])
    # auth exception #7: the provider inbound webhooks - a shared header secret is the whole
    # authentication (operator infrastructure); the address resolves the channel; every
    # refusal is a ledger row wearing its reason.
    r.add_api_route("/channels/inbound/email",          channels.inbound_email,        methods=["POST"], status_code=202)
    # plugin installs - the receipts of Agent Plugins bundles; installing is a CLI act
    r.add_api_route("/plugins",                      plugins.list_plugin_installs, methods=["GET"])
    # routines - standing work: a saved composer payload + a When; every fire mints a Task
    r.add_api_route("/routines",                     routines.create_routine, methods=["POST"], status_code=201)
    r.add_api_route("/routines",                     routines.list_routines,  methods=["GET"])
    r.add_api_route("/routines/{routine_id}",        routines.get_routine,    methods=["GET"])
    r.add_api_route("/routines/{routine_id}",        routines.update_routine, methods=["PATCH"])
    r.add_api_route("/routines/{routine_id}/pause",  routines.pause_routine,  methods=["POST"])
    r.add_api_route("/routines/{routine_id}/resume", routines.resume_routine, methods=["POST"])
    r.add_api_route("/routines/{routine_id}/retire", routines.retire_routine, methods=["POST"])
    # ── the fire door: Bearer may be the routine's own rt_ token (auth exception #4)
    r.add_api_route("/routines/{routine_id}/fire",   routines.fire_routine,   methods=["POST"], status_code=202)
    r.add_api_route("/routines/{routine_id}/token",  routines.mint_fire_token, methods=["POST"], status_code=201)
    r.add_api_route("/routines/{routine_id}/token",  routines.revoke_fire_token, methods=["DELETE"])
    r.add_api_route("/routines/{routine_id}/runs",   routines.list_runs,      methods=["GET"])
    # registry · models · admin
    r.add_api_route("/agents",                 registry.list_agents,   methods=["GET"])
    r.add_api_route("/models",                 models.list_models,     methods=["GET"])
    r.add_api_route("/admin/routing-table",    admin.get_routing,      methods=["GET"])
    r.add_api_route("/admin/routing-table",    admin.put_routing,      methods=["PUT"])
    r.add_api_route("/admin/models",           admin.list_models,      methods=["GET"])
    r.add_api_route("/admin/models",           admin.add_model,        methods=["POST"], status_code=201)
    r.add_api_route("/admin/models/{id}",      admin.patch_model,      methods=["PATCH"])
    r.add_api_route("/admin/price-book",       admin.get_prices,       methods=["GET"])
    r.add_api_route("/admin/price-book",       admin.put_prices,       methods=["PUT"])
    _mount_admin(r)
    app.include_router(r)
    app.add_api_websocket_route("/v1/tasks/{task_id}/stream", stream.task_ws)   # Bearer, or ?ticket=et_…
    app.add_api_websocket_route("/v1/tasks/{task_id}/screen", screen.stream_screen)   # ?ticket=st_…
    app.add_api_route("/v1/tasks/{task_id}/events", stream.task_sse, methods=["GET"])   # SSE co-primary; Bearer, or ?ticket=et_…
    _mount_stream_tickets(app)
    # The browser watcher. The PAGE is inert HTML - no auth needed to fetch it; the token
    # rides in the URL fragment and mints the stream's ticket, never authenticating the page. Not under /v1: it is
    # a human surface, not an API resource.
    app.add_api_route("/watch/{task_id}", watch.watch_page, methods=["GET"], include_in_schema=False)
    app.add_api_route("/healthz", health.healthz, methods=["GET"])          # bare: status only (LBs)
    app.add_api_route("/healthz/full", health.healthz_full, methods=["GET"])  # vitals, behind an Actor
# fmt: on
