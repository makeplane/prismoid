# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The plan's rules: a proposal is a plan, or the rule it broke; pure, no model in the loop.

The Lead proposes and these rules dispose. A model may name an agent that does not exist, depend on a
step it never declared, describe a cycle, or plan something that delivers nothing; each is caught here by
code, with a reason the Lead reads when `PlanCheck` sends the plan back, and a plan that still breaks
one at the commit lands on the one-step floor instead of failing the task. The rules read the roster as
values the caller flattens (`roster`: each agent's kinds, its outcome, or nothing), so this module never
reaches the registry. A validated step's contract wears the clauses the system promises the person
(`input_clause`, `depth_clause`), never a model's restatement of them; how the file is handed over is
the agent's brief, never a line its work is judged on.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Final

from prismoid.ai.frames.layout import input_paths
from prismoid.schemas import sizing
from prismoid.schemas.contract import Contract, Criteria, SkillPin
from prismoid.schemas.ids import AgentName, StepId
from prismoid.schemas.inputs import TaskInput
from prismoid.schemas.outputs import ArtifactType, OutputRef
from prismoid.schemas.plan import PlanEdge, PlanProposal, ProposedStep, Seat, StepKind, StepSpec
from prismoid.schemas.sizing import SizeBucket

MAX_STEPS = 12  # a plan a human cannot hold in their head is a plan nobody is steering


class Delivers(Enum):
    """What a step delivers when its agent publishes no typed file."""

    OUTCOME = "outcome"


# The roster sentinel for an agent whose step delivers by FINISHING: its report is the deliverable and
# anything it publishes rides beside it (the operator). Distinct from None, which says the agent
# publishes nothing and its narration feeds a later step.
DELIVERS_OUTCOME: Final = Delivers.OUTCOME

# The kinds of file an agent's step can publish (`AgentDef.kinds`), as the roster hands them in.
Makes = tuple[ArtifactType, ...]

# A plan as it is written: its steps in topological order, its edges, its criteria.
Plan = tuple[list[tuple[StepId, StepSpec]], list[PlanEdge], Criteria]


class InvalidPlan(Exception):
    """The proposal is not a plan; the message names the rule it broke, in words the Lead reads back."""


def input_clause(paths: Sequence[str]) -> list[str]:
    """The acceptance lines sources impose: advice without sources, a checkable assertion with them."""
    if not paths:
        return ["every factual claim is traceable to a source or is marked as unsourced"]
    return [
        f"every factual claim, number and quotation comes from one of the provided sources "
        f"({', '.join(paths)}) - nothing is invented, and nothing is cited that is not there",
        "arithmetic derived from the sources is allowed and should be shown; a figure that is "
        "neither quoted nor derived is a fabrication",
    ]


def depth_clause(
    produces: ArtifactType, bucket: SizeBucket | None = None, units: int | None = None
) -> list[str]:
    """The size band the person dialed (none when they sized nothing), and for a collection the substance
    its rows carry. No default ceiling: one typed answer is bounded by its model's output and the budget."""
    lines = sizing.size_clause(produces, bucket, units) if bucket is not None else []
    if produces is ArtifactType.COLLECTION:
        lines = [
            *lines,
            "rows carry substance beyond a title: a stage/status, dates where the ask or sources "
            "provide them, and a short note/description field - sparse cells only where truly unknown",
        ]
    return lines


def plain_filename(name: str) -> str | None:
    """A file name the user asked for, or None: one plain name, never a path or a dotfile. The door
    still judges its extension; this keeps a proposal's name from choosing a directory."""
    name = name.strip()
    if not name or name.startswith(".") or any(c in name for c in "/\\\0"):
        return None
    return name


def validate(
    proposal: PlanProposal,
    *,
    roster: Mapping[str, Makes | Delivers | None],
    inputs: Sequence[TaskInput],
    deliverable: ArtifactType | None = None,
    browses: Collection[str] = (),
    recipes: Mapping[str, Plan] | None = None,
    pins: Mapping[str, SkillPin] | None = None,
    size: tuple[SizeBucket, int | None] | None = None,
) -> Plan:
    """A model's proposal as a plan, or `InvalidPlan` naming the rule it broke: step count, unique labels,
    known agents, declared deps, no cycle, something delivered (the dialed kind last); `recipes` expand,
    and `size` (the turn's own size dial) bands every typed step."""
    steps = proposal.steps
    graph = replace(_graph(proposal, roster, deliverable), shaped=frozenset(recipes or ()))
    for rule in _DELIVERY_RULES:
        rule(graph)

    paths = input_paths(i.filename for i in inputs)
    exits = {label: StepId(uuid.uuid4()) for label in graph.labels}
    entries = {label: [sid] for label, sid in exits.items()}
    built: list[tuple[StepId, StepSpec]] = []
    edges: list[PlanEdge] = []
    for label in graph.order:  # topological: a serial executor running this in order is correct
        step = graph.by_label[label]
        loads = [pins[s] for s in step.skills if pins is not None and s in pins]
        shape = (recipes or {}).get(label)
        if shape is None:
            spec = _built_step(step, graph.kinds.get(label), paths, browses, size)
            built.append((exits[label], _loading(spec, loads)))
            continue
        shaped, inner, _ = shape
        built.extend((sid, _loading(spec, loads)) for sid, spec in shaped)
        edges.extend(inner)
        fed = {edge.to_step for edge in inner}
        exits[label] = shaped[-1][0]
        entries[label] = [sid for sid, _ in shaped if sid not in fed]

    edges += [
        PlanEdge(from_step=exits[dep], to_step=entry, kind="produces_for")
        for step in steps
        for dep in step.depends_on
        for entry in entries[step.label]
    ]
    criteria = Criteria(
        must=proposal.must or [steps[-1].goal], tone=proposal.tone, audience=proposal.audience
    )
    return built, edges, criteria


def breach(proposal: PlanProposal, seats: Sequence[Seat], deliverable: ArtifactType | None) -> str | None:
    """The first rule a plan seated on `seats` breaks, the commit's whole set (a recipe step's shape
    delivering), or None: the Lead's run asks it, so a plan the commit would refuse goes back in-run."""
    roster = {
        s.name: DELIVERS_OUTCOME if s.delivers_outcome else (s.kinds if s.publishes else None) for s in seats
    }
    shaped = frozenset(s.label for s in proposal.steps if s.recipe is not None)
    try:
        graph = replace(_graph(proposal, roster, deliverable), shaped=shaped)
        for rule in _DELIVERY_RULES:
            rule(graph)
    except InvalidPlan as broke:
        return str(broke)
    return None


def _loading(spec: StepSpec, loads: Sequence[SkillPin]) -> StepSpec:
    """A step spec with the skills its proposed step named, after any it already carries."""
    if not loads or spec.kind is not StepKind.STEP:
        return spec
    named = {pin.name for pin in spec.skill_pins}
    return spec.model_copy(
        update={"skill_pins": [*spec.skill_pins, *(p for p in loads if p.name not in named)]}
    )


def _graph(
    proposal: PlanProposal, roster: Mapping[str, Makes | Delivers | None], deliverable: ArtifactType | None
) -> _Proposal:
    """The proposal as the delivery rules read it: the shape rules passed, each step's kind and the execution
    order derived (raises `InvalidPlan` on a cycle)."""
    steps = proposal.steps
    shape = _Proposal(
        steps=tuple(steps),
        roster=roster,
        labels=tuple(s.label for s in steps),
        deliverable=deliverable,
        by_label={s.label: s for s in steps},
    )
    for rule in _SHAPE_RULES:
        rule(shape)
    shape = replace(shape, kinds=_step_kinds(shape))
    return replace(shape, order=tuple(_topological(steps)))


@dataclass(frozen=True, slots=True)
class _Proposal:
    """What every rule reads, derived once: steps, roster, labels in order, steps by label, each typed
    publishing step's kind and the execution order."""

    steps: tuple[ProposedStep, ...]
    roster: Mapping[str, Makes | Delivers | None]
    labels: tuple[str, ...]
    deliverable: ArtifactType | None
    by_label: Mapping[str, ProposedStep]
    kinds: Mapping[str, ArtifactType] = field(default_factory=dict[str, ArtifactType])
    order: tuple[str, ...] = ()
    shaped: frozenset[str] = frozenset()  # the labels a code-made shape replaces, whose last step delivers


def _step_count(p: _Proposal) -> None:
    if not p.steps:
        raise InvalidPlan("the plan has no steps")
    if len(p.steps) > MAX_STEPS:
        raise InvalidPlan(f"{len(p.steps)} steps is more than the {MAX_STEPS} a plan may have")


def _unique_labels(p: _Proposal) -> None:
    labels = list(p.labels)
    if len(set(labels)) != len(labels):
        dupes = sorted({label for label in labels if labels.count(label) > 1})
        raise InvalidPlan(f"two steps share a label: {', '.join(dupes)}")


def _agents_exist(p: _Proposal) -> None:
    names = frozenset(p.roster)
    if unknown := sorted({s.agent for s in p.steps} - names):
        raise InvalidPlan(f"no such agent: {', '.join(unknown)}. The roster is: {', '.join(sorted(names))}")


def _dependencies_declared(p: _Proposal) -> None:
    known = set(p.labels)
    for step in p.steps:
        if dangling := sorted(set(step.depends_on) - known):
            raise InvalidPlan(f"step {step.label} depends on a step that does not exist: {dangling[0]}")
        if step.label in step.depends_on:
            raise InvalidPlan(f"step {step.label} depends on itself")


def _step_kinds(p: _Proposal) -> dict[str, ArtifactType]:
    """Each typed publishing step's kind, by label: its agent's one kind, or of an agent's several the one the
    step names (the chosen deliverable when it names none and its agent makes it)."""
    kinds: dict[str, ArtifactType] = {}
    for step in p.steps:
        made = p.roster[step.agent]
        if not step.publishes or not isinstance(made, tuple) or not made:
            continue
        kind = made[0] if len(made) == 1 else step.produces or p.deliverable
        if kind is None or kind not in made:
            raise InvalidPlan(
                f"step {step.label}: {step.agent} makes {', '.join(k.value for k in made)} - set its "
                "produces to the kind of file this step publishes"
            )
        kinds[step.label] = kind
    return kinds


def _silent_agents_never_publish(p: _Proposal) -> None:
    for step in p.steps:
        if step.publishes and p.roster[step.agent] is None:
            raise InvalidPlan(
                f"step {step.label}: {step.agent} publishes nothing - the change it makes is the "
                "deliverable and its narration flows to the steps that depend on it. Set "
                "publishes=false, and publish from a step that depends on it (the writer, or the "
                "maker for a file format)."
            )


def _something_delivered(p: _Proposal) -> None:
    depended_on = {dep for s in p.steps for dep in s.depends_on}
    ends_on_outcome = any(
        (p.roster[s.agent] is DELIVERS_OUTCOME or s.label in p.shaped) and s.label not in depended_on
        for s in p.steps
    )
    if not ends_on_outcome and not p.kinds:
        raise InvalidPlan(
            "no step publishes anything - the task would run, cost money, and deliver nothing to "
            "the user. At least one step must set publishes=true, or the plan must end on an agent "
            "that publishes an outcome."
        )


def _deliverable_honored(p: _Proposal) -> None:
    """A dialed kind is what the plan delivers: the last typed publishing step in execution order makes it.
    Interim publishes stay legal; an outcome never satisfies a dial."""
    deliverable, kinds, by_label = p.deliverable, p.kinds, p.by_label
    if deliverable is None:
        return
    final = next((label for label in reversed(p.order) if label in kinds), None)
    final_kind = kinds[final] if final is not None else None
    if final_kind is deliverable:
        return
    able = sorted(name for name, made in p.roster.items() if isinstance(made, tuple) and deliverable in made)
    found = (
        f"the final publishing step ({final!r}, agent {by_label[final].agent!r}) publishes {final_kind.value}"
        if final is not None and final_kind is not None
        else "no step publishes it"
    )
    raise InvalidPlan(
        f"the user chose a {deliverable.value} deliverable, but {found}. "
        f"The delivering step must use: {', '.join(able) or 'no agent publishes this kind'}"
    )


# The rules before the cycle check, in order: a dangling dependency is named as one before the cycle
# check could misread it as a cycle.
_SHAPE_RULES: Final[tuple[Callable[[_Proposal], None], ...]] = (
    _step_count,
    _unique_labels,
    _agents_exist,
    _dependencies_declared,
)
# The rules after the cycle check, in order; the last one reads the execution order.
_DELIVERY_RULES: Final[tuple[Callable[[_Proposal], None], ...]] = (
    _silent_agents_never_publish,
    _something_delivered,
    _deliverable_honored,
)


def _built_step(
    step: ProposedStep,
    kind: ArtifactType | None,
    paths: Sequence[str],
    browses: Collection[str],
    size: tuple[SizeBucket, int | None] | None,
) -> StepSpec:
    """One validated proposed step as the real StepSpec; `kind` is what it publishes, None for an untyped step."""
    acceptance = [*step.acceptance]
    if kind is not None:
        acceptance = [*acceptance, *input_clause(paths), *depth_clause(kind, *(size or (None, None)))]
    return StepSpec(
        kind=StepKind.STEP,
        agent=AgentName(step.agent),
        # The step's own need, never its agent's: a connector act or file work takes no browser.
        browser=(step.browser or bool(step.hosts)) and step.agent in browses,
        title=step.title[:120],
        why=step.why[:200],
        contract=Contract(
            goal=step.goal,
            acceptance=acceptance,
            inputs=list(paths),
            output=(
                # Typed by the agent that runs the step: one of its kinds, never one it does not make.
                OutputRef(type=kind, title=step.title[:80], filename=plain_filename(step.filename))
                if kind is not None
                else None  # an outcome step and a research step owe the task a finding, not a typed file
            ),
        ),
    )


def _topological(steps: Sequence[ProposedStep]) -> list[str]:
    """Kahn's algorithm: the labels in an order a serial executor can run, or `InvalidPlan` on a cycle
    (`research -> write -> fact-check -> research` reads as reasonable and never terminates)."""
    pending = {s.label: set(s.depends_on) for s in steps}
    order: list[str] = []
    while pending:
        ready = sorted(label for label, deps in pending.items() if not deps)
        if not ready:
            raise InvalidPlan(
                f"the plan has a cycle among: {', '.join(sorted(pending))} - it would never finish"
            )
        for label in ready:
            order.append(label)
            del pending[label]
        for deps in pending.values():
            deps -= set(ready)
    return order
