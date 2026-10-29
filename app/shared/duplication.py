"""The `duplicated` condition's algebra. Ticket 21 decided it; ticket 32 built it.

**Pure.** No I/O, no client, no config. It takes a description of the dataflow graph plus
typed column sets and returns twin-sets, so the whole rule is testable off fixtures and
the granite gate is a test rather than a run.

The rule, in one sentence: two **output datasets** are **twins** when their producing
dataflows **differ**, the producers' **input dataset-id sets** are identical, the producers'
**engine** is identical, and the outputs' own **typed column sets** are identical.

Four things about that sentence are load-bearing, and each is a false positive somebody
already found on granite:

* **No names are read.** Not the dataflow's, not the dataset's. Names are ~28% ambiguous on
  granite and, once input-set identity is required, they add nothing — the typed-column axis
  reproduces a name-based key exactly (ticket 21's census).
* **The producing dataflows must DIFFER.** Without this, a dataflow's own sibling outputs
  pair with each other: `ReadySignal_Next 12_Calc` and `_Next 18_Calc` share producer 622,
  one input and one schema, and differ only by a filter. Three `NS Help Desk` NULL-check
  outputs share producer 483. A deliberate multi-output split is not a duplicate.
* **Input-set identity alone is too weak, and the shape axis is what fixes it.** Sharing
  your *only* input is common and innocent — dropping the typed-column axis false-positives
  `Ticket Snapshot` against `Created vs. Resolved Issues`. Five of six extras were
  single-input coincidences.
* **The input set must be NON-EMPTY**, which is the same ruling that put connector-import
  duplication out of scope: the input dataset-id set is what turns "same shape" into "same
  job", and an object with no inputs has no job identity to match on. Free on granite (the
  numbers are identical either way) and a guard everywhere else.

**Absence is never identity** (ADR-0003). An output with no column schema and a producer with
an absent engine are `unknown` — never "matches another unknown".

**Unknown and excluded are kept apart.** A missing schema is "we could not tell"; a producer
with no observed inputs is "the rule says no", the same structural shape as the population
filter. Folding them together would report 18 uncertainties on granite where there are 8, and
an admin reading a coverage note needs to know which half is a data problem they might fix.

**Either way it propagates to the dataflow grain**, because the test there is "*every* output
is twinned": a dataflow with any output the rule did not settle is set aside whole rather than
judged on the outputs that happen to be readable. Granite has no MIXED dataflow today — all
seven with schemaless outputs have *only* schemaless outputs — which is exactly the shape that
would have shipped a silent false positive on the first instance where it differed.

**The group owns the saving, and the aggregate is a floor.** Cost is metered per output
dataset, so one redundancy shows up once per member per grain: on granite's three pairs,
1,320 credits/month displayed across 14 candidates against 330 realisable, 4.0x over.
`realisable_recurring` therefore keeps the most expensive member of each twin-set and retires
the rest, which is the *least* a human can realise by acting on the group. See
`Duplication.aggregate`.
"""

from __future__ import annotations

import hashlib
import json
from typing import Callable, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

GRAIN_OUTPUT = "dataset"
GRAIN_DATAFLOW = "dataflow"

#: Why one object could not be tested. Recorded per object, never collapsed to a count —
#: an admin needs to know WHICH axis was missing to know whether it matters.
UNKNOWN_NO_SCHEMA = "output has no column schema"
UNKNOWN_NO_ENGINE = "producer's engine is absent"
UNKNOWN_NO_INPUTS = "producer has no observed input datasets"


class DataflowShape:
    """One producer, reduced to the three axes the rule reads.

    Deliberately not the pillar's `Asset`: this module is pure, and taking a narrow record
    is what lets the granite gate run off a fixture with no client in the room.
    """

    __slots__ = ("dataflow_id", "inputs", "engine", "outputs")

    def __init__(
        self,
        dataflow_id: str,
        inputs: Sequence[str],
        engine: Optional[str],
        outputs: Sequence[str],
    ) -> None:
        self.dataflow_id = dataflow_id
        self.inputs: FrozenSet[str] = frozenset(inputs)
        self.engine = engine
        self.outputs: FrozenSet[str] = frozenset(outputs)


class TwinSet:
    """Two or more objects the rule found indistinguishable, at one grain."""

    __slots__ = ("grain", "group_id", "members", "producers", "key_digest")

    def __init__(
        self, grain: str, group_id: str, members: Sequence[str], producers: Dict[str, str]
    ) -> None:
        self.grain = grain
        self.group_id = group_id
        #: Sorted asset ids — output dataset ids at the output grain, dataflow ids at the
        #: dataflow grain.
        self.members: List[str] = sorted(members)
        #: member asset id -> producing dataflow id. Empty at the dataflow grain, where the
        #: member IS the producer.
        self.producers = dict(producers)

    def twins_of(self, member: str) -> List[str]:
        return [m for m in self.members if m != member]

    def as_dict(self) -> dict:
        out = {
            "group_id": self.group_id,
            "grain": self.grain,
            "members": list(self.members),
        }
        if self.producers:
            out["producers"] = {m: self.producers[m] for m in self.members if m in self.producers}
        return out


def _digest(grain: str, *parts) -> str:
    """A stable group id.

    Hashed over a canonical serialization of the key rather than assigned by iteration
    order, so two runs over identical data label the same group identically (ADR-0001) —
    and so do two *instances*, which is what makes a group id quotable in a review. It
    rides in `evidence` and never in `candidate_id`, so the ledger's fold key is untouched
    even if a key's serialization ever changes.
    """
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=sorted)
    prefix = "out" if grain == GRAIN_OUTPUT else "flow"
    return f"dup-{prefix}-{hashlib.sha1(payload.encode('utf-8')).hexdigest()[:12]}"


class Duplication:
    """The twin-sets of one instance, at both grains, plus what could not be tested."""

    def __init__(self) -> None:
        self.output_sets: List[TwinSet] = []
        self.dataflow_sets: List[TwinSet] = []
        #: asset id -> the TwinSet it belongs to, per grain.
        self._by_output: Dict[str, TwinSet] = {}
        self._by_dataflow: Dict[str, TwinSet] = {}
        #: The dataflow grain is ASYMMETRIC where one producer's job is a strict subset of
        #: another's, so the partner that twins a candidate is recorded explicitly rather
        #: than inferred from group membership.
        self.dataflow_partners: Dict[str, List[str]] = {}
        #: Objects the rule COULD NOT judge because data was missing — no column schema, an
        #: absent producer engine. Genuinely unknown, in ADR-0003's sense.
        self.unknown_outputs: List[dict] = []
        self.unknown_dataflows: List[dict] = []
        #: Objects the rule DECLINED to judge by a structural rule: the producer has no
        #: observed inputs, so it has no job identity to match on. Kept apart from the
        #: unknowns deliberately — one is "we could not tell", the other is "the rule says
        #: no", and folding them together would report 18 uncertainties on granite where
        #: there are 8. Same distinction the population filter draws.
        self.excluded_outputs: List[dict] = []
        self.excluded_dataflows: List[dict] = []

    # -- lookups ---------------------------------------------------------------------------

    def output_set(self, dataset_id: str) -> Optional[TwinSet]:
        return self._by_output.get(dataset_id)

    def dataflow_set(self, dataflow_id: str) -> Optional[TwinSet]:
        return self._by_dataflow.get(dataflow_id)

    @property
    def output_candidates(self) -> List[str]:
        return sorted(self._by_output)

    @property
    def dataflow_candidates(self) -> List[str]:
        return sorted(self._by_dataflow)

    # -- the aggregate ---------------------------------------------------------------------

    def survivors(self, term_of_output: Callable[[str], Optional[float]]) -> Optional[float]:
        """The cost of the member each twin-set KEEPS — what an entity-once sum must give
        back so the group is counted once (ticket 34).

        `aggregate()` reports what a group *recovers* (`sum - max`); this reports what it
        *retains* (`max`). Both read the same "keep the most expensive, retire the rest"
        rule off the same `output_sets`, which is why they live together rather than the
        rule being restated wherever a group needs counting once.

        **On granite the two are numerically identical — 330.0 either way — because every
        duplicate group is an equal-cost PAIR.** A group of [100, 50] separates them: it
        retains 100 and recovers 50. So a fixture built from granite's own shape cannot tell
        a correct reduction from an inverted one, and every test for this must carry an
        unequal-cost group.

        OUTPUT grain only, exactly as `realisable_recurring` is: a dataflow's figure is the
        sum of its own outputs' and would retain the same credits twice.

        Returns None rather than 0.0 if any member is unmeasurable — a group whose survivor
        is unknown must not be silently reduced by nothing.
        """
        total = 0.0
        for group in self.output_sets:
            values = [term_of_output(m) for m in group.members]
            if any(v is None for v in values):
                return None
            total += max(values)
        return round(total, 3)

    def aggregate(
        self,
        recurring_of_output: Callable[[str], Optional[float]],
        recurring_of_dataflow: Callable[[str], Optional[float]],
    ) -> dict:
        """Displayed against realisable, counted **once per output-grain twin-set**.

        Cost is metered per output dataset, so a redundancy is displayed once per member per
        grain while only one member's spend is recoverable. Two figures, both true, and the
        gap named rather than left for a reader to sum wrongly:

        * `displayed_recurring` — what adding up the `duplicated` candidate rows gives, at
          **both** grains, because both grains are rows in the file. Every one of those
          per-candidate figures is a true meter reading of what that entity bills, which is
          why none of them is suppressed or hidden.
        * `realisable_recurring` — one twin-set at a time, keeping the **most expensive**
          member and retiring the rest. A FLOOR, deliberately: nothing in the data picks
          which twin survives, so the honest aggregate is the least a human can realise by
          acting on the group. Only the OUTPUT grain contributes, because a dataflow's
          figure is the sum of its own outputs' and would count the same credits twice.

        On granite's three pairs that is 1,320.0 against 330.0 credits/month — **4.0x** —
        which is the whole reason this method exists.

        Returns nulls rather than zeros where no meter was readable: `basis: none` must not
        read as "measured, and it is free".
        """
        displayed: Optional[float] = 0.0
        realisable: Optional[float] = 0.0

        for group in self.output_sets:
            values = [recurring_of_output(m) for m in group.members]
            if any(v is None for v in values):
                displayed = realisable = None
                break
            displayed += sum(values)
            realisable += sum(values) - max(values)

        if displayed is not None:
            for dataflow_id in sorted(self._by_dataflow):
                value = recurring_of_dataflow(dataflow_id)
                if value is None:
                    displayed = realisable = None
                    break
                displayed += value

        return {
            "groups": {
                GRAIN_OUTPUT: len(self.output_sets),
                GRAIN_DATAFLOW: len(self.dataflow_sets),
            },
            "candidates": {
                GRAIN_OUTPUT: len(self._by_output),
                GRAIN_DATAFLOW: len(self._by_dataflow),
            },
            "counted_once_at": GRAIN_OUTPUT,
            "displayed_recurring": None if displayed is None else round(displayed, 3),
            "realisable_recurring": None if realisable is None else round(realisable, 3),
            "unknown_outputs": len(self.unknown_outputs),
            "unknown_dataflows": len(self.unknown_dataflows),
            "excluded_outputs": len(self.excluded_outputs),
            "excluded_dataflows": len(self.excluded_dataflows),
        }


def find(
    flows: Sequence[DataflowShape],
    typed_columns: Callable[[str], Optional[FrozenSet[str]]],
) -> Duplication:
    """Twin-sets at both grains.

    `flows` is the SCORED population only — the caller applies the population filter first,
    because a non-executing object has no job to duplicate and would otherwise pair with a
    real one.

    `typed_columns` returns a frozenset of `name:TYPE` strings, or **None** for a dataset
    whose schema the instance did not report. None is not an empty set: two datasets with
    unknown schemas are not twins.
    """
    result = Duplication()

    # -- output grain ----------------------------------------------------------------------
    # key = (producer's input-set, producer's engine, the output's own typed column set)
    keys: Dict[Tuple[str, str], tuple] = {}
    producer_of: Dict[str, str] = {}

    for flow in flows:
        unjudgeable: Optional[str] = None
        if not flow.engine:
            unjudgeable = UNKNOWN_NO_ENGINE
        elif not flow.inputs:
            # No inputs => no job identity. Same ruling that put connector-import
            # duplication out of scope: shape without inputs is a connector, not a copy.
            unjudgeable = UNKNOWN_NO_INPUTS

        for output in sorted(flow.outputs):
            producer_of[output] = flow.dataflow_id
            reason = unjudgeable
            columns = None
            if reason is None:
                columns = typed_columns(output)
                if columns is None:
                    reason = UNKNOWN_NO_SCHEMA
            if reason is not None:
                record = {
                    "dataset_id": output,
                    "dataflow_id": flow.dataflow_id,
                    "why": reason,
                }
                if reason == UNKNOWN_NO_INPUTS:
                    result.excluded_outputs.append(record)
                else:
                    result.unknown_outputs.append(record)
                continue
            keys[(flow.dataflow_id, output)] = (
                tuple(sorted(flow.inputs)),
                flow.engine,
                tuple(sorted(columns)),
            )

    grouped: Dict[tuple, List[Tuple[str, str]]] = {}
    for pair, key in keys.items():
        grouped.setdefault(key, []).append(pair)

    for key, pairs in sorted(grouped.items(), key=lambda kv: sorted(kv[1])):
        producers = {producer for producer, _ in pairs}
        if len(producers) < 2:
            # Sibling outputs of a single dataflow. A deliberate multi-output split, not a
            # duplicate — this is the structural rule, and without it granite's producers
            # 622 and 483 false-positive against themselves.
            continue
        members = [output for _, output in pairs]
        group = TwinSet(
            grain=GRAIN_OUTPUT,
            group_id=_digest(GRAIN_OUTPUT, *key),
            members=members,
            producers={output: producer for producer, output in pairs},
        )
        result.output_sets.append(group)
        for member in group.members:
            result._by_output[member] = group

    # -- dataflow grain --------------------------------------------------------------------
    # A dataflow is a candidate when it has outputs, EVERY one of them is judgeable, and
    # ONE other dataflow supplies a twin for every one of them. "Every output" is why an
    # unjudgeable output disqualifies the whole producer rather than being skipped: a
    # dataflow judged on two of its three outputs would be reported as fully duplicated on
    # evidence that does not cover it.
    unjudgeable_outputs: Dict[str, List[str]] = {}
    for record in result.unknown_outputs + result.excluded_outputs:
        unjudgeable_outputs.setdefault(record["dataflow_id"], []).append(record["why"])

    output_keys: Dict[str, Set[tuple]] = {}
    for flow in flows:
        if not flow.outputs:
            continue
        if flow.dataflow_id in unjudgeable_outputs:
            reasons = sorted(set(unjudgeable_outputs[flow.dataflow_id]))
            structural = reasons == [UNKNOWN_NO_INPUTS]
            record = {
                "dataflow_id": flow.dataflow_id,
                "why": (
                    f"the producer has no observed input datasets, so it has no job "
                    f"identity to match on — excluded by rule, not by missing data"
                    if structural
                    else f"{len(unjudgeable_outputs[flow.dataflow_id])} of "
                         f"{len(flow.outputs)} output(s) could not be tested "
                         f"({reasons[0]}), so 'every output is twinned' cannot be "
                         f"established either way"
                ),
            }
            if structural:
                result.excluded_dataflows.append(record)
            else:
                result.unknown_dataflows.append(record)
            continue
        output_keys[flow.dataflow_id] = {
            keys[(flow.dataflow_id, output)] for output in flow.outputs
        }

    shapes = {flow.dataflow_id: flow for flow in flows}
    job_key: Dict[str, tuple] = {}
    for dataflow_id, own in sorted(output_keys.items()):
        partners = sorted(
            other
            for other, theirs in output_keys.items()
            if other != dataflow_id and own <= theirs
        )
        if not partners:
            continue
        result.dataflow_partners[dataflow_id] = partners
        flow = shapes[dataflow_id]
        job_key[dataflow_id] = (
            tuple(sorted(flow.inputs)),
            flow.engine,
            tuple(sorted(own)),
        )

    jobs: Dict[tuple, List[str]] = {}
    for dataflow_id, key in job_key.items():
        jobs.setdefault(key, []).append(dataflow_id)

    for key, members in sorted(jobs.items(), key=lambda kv: sorted(kv[1])):
        # A group of ONE is legitimate here and is the asymmetric case: this dataflow's
        # whole job is a strict subset of a bigger dataflow's, so retiring THIS one is the
        # action and the bigger one is not itself redundant. The partner is named in
        # `dataflow_partners` either way, so the finding never depends on group size.
        group = TwinSet(
            grain=GRAIN_DATAFLOW,
            group_id=_digest(GRAIN_DATAFLOW, *key),
            members=members,
            producers={},
        )
        result.dataflow_sets.append(group)
        for member in group.members:
            result._by_dataflow[member] = group

    result.output_sets.sort(key=lambda g: g.members)
    result.dataflow_sets.sort(key=lambda g: g.members)
    for records in (result.unknown_outputs, result.excluded_outputs):
        records.sort(key=lambda r: (r["dataflow_id"], r["dataset_id"]))
    for records in (result.unknown_dataflows, result.excluded_dataflows):
        records.sort(key=lambda r: r["dataflow_id"])
    return result
