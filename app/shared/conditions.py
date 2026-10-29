"""The measuring rules instance-cleanup's conditions and instance-documentation's signals share.

Moved here from `collector/pillars/data.py` when instance-documentation became their second
consumer (D2 ticket 09). **Rules only — no reads, no candidates, no evidence.** Each caller
supplies its own facts and decides what a result means for it: cleanup turns a True into a
candidate, documentation into a judged signal state. One rule, so "stale" in a cleanup finding
and "stale" in an asset doc can never drift apart (the documentation glossary's **Weak point**).

What stays in the pillar is everything cleanup-specific about *which facts* feed a rule —
the enumeration pre-filter, lineage adjudication, publication liveness and the ledger's
retained candidates all decide a dataset's *direct* consumption state there, and are handed
to `unconsumed_states` below as a callable.

Defaults: the windows below are the values instance-cleanup ships in
`projects/instance-cleanup/config/defaults.yaml`, where their provenance is recorded. Either
project may override them per instance under `conditions:` in `instances/<key>/config.yaml`;
`test_shared_condition_defaults_match_cleanup` keeps the two default sets equal.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

#: `conditions.stale.dataset_days` / `dataflow_days` — one quarter with no refresh.
STALE_DATASET_DAYS = 90
STALE_DATAFLOW_DAYS = 90
#: `conditions.stale.never_run_is_stale` — a never-run object has an undefined age.
NEVER_RUN_IS_STALE = False
#: `conditions.inactive.last_login_days` — the login line an owner is judged against.
LAST_LOGIN_DAYS = 180

#: Node-key prefixes for the consumer graph; the same spellings as cleanup's asset kinds.
DATASET = "dataset"
DATAFLOW = "dataflow"


def node_key(kind: str, asset_id: str) -> str:
    return f"{kind}:{asset_id}"


# ---------------------------------------------------------------------------------------
# stale
# ---------------------------------------------------------------------------------------


def days_since(stamp: _dt.datetime, as_of: _dt.date) -> int:
    """Age in whole days, measured between two DATES rather than from a moving clock.

    Ticket 26. The timestamp form `(now - stamp).days` reads a wall clock, so an asset
    whose last run sits near the window boundary crosses it *while the collector is
    running*: two granite runs two minutes apart moved `days_stale` for 6 of 2,475
    candidates and 4 ranks; over a 3.5-hour gap it moved 720 of 2,475, which is simply
    3.5/24 of a day. ADR-0001 claims deterministic findings, and that made it false.

    The crossing is a cliff, not a trickle, which is why the small symptom mattered: 170
    granite candidates share a `last_run` inside a TWO-MINUTE window
    (2026-05-18T21:44:31Z-21:44:33Z), so under the timestamp form all 170 crossed 90 days
    within those same two minutes, and two runs that afternoon straddling 21:44 would have
    disagreed by 170 candidates -- reported by the diff as newly appeared.

    The ledger settled the same question in the same direction for cycles (ticket 07): a
    cycle is an absolute date, not a counter, precisely so that three runs in one afternoon
    are idempotent. Age is the same quantity and now reads the same way.
    """
    return (as_of - stamp.date()).days


def stale_age(
    last_attempt: Optional[_dt.datetime],
    never_ran: bool,
    as_of: _dt.date,
    never_run_is_stale: bool = NEVER_RUN_IS_STALE,
) -> Optional[int]:
    """The age `stale` is judged on, or None when the object is outside the test.

    **Stale presupposes a refresh history.** A never-run object has an *undefined* age, not
    an infinite one, so unless `never_run_is_stale` is configured it gets no age at all; and
    an object with no recorded attempt has nothing to measure from. Measured on the run
    ATTEMPT, never the landing (cleanup glossary, **Run attempt vs. data landing**).
    """
    if never_ran and not never_run_is_stale:
        return None
    if last_attempt is None:
        return None
    return days_since(last_attempt, as_of)


def is_stale(age: Optional[int], window_days: int) -> bool:
    """Older than the window. An undefined age is never stale."""
    return age is not None and age > window_days


# ---------------------------------------------------------------------------------------
# failing
# ---------------------------------------------------------------------------------------


def dataset_failing(status: Optional[str]) -> bool:
    """A dataset's last load ATTEMPT errored (DomoStats `Status` = ERROR).

    Deliberately not gated on a completed run — 18 granite datasets report ERROR with no load
    date at all, nine of them netsuite-jdbc connections that have never once succeeded."""
    return (status or "").upper() == "ERROR"


def dataflow_failing(last_execution_failed: Optional[str]) -> bool:
    """A dataflow's last execution failed (`Dataflow Details`.`Last Execution Failed`).

    Absent means no attempt was ever made, which is not a failure — that is the never-run
    population, and `unconsumed` has it."""
    return last_execution_failed == "True"


# ---------------------------------------------------------------------------------------
# unconsumed
# ---------------------------------------------------------------------------------------


#: A fact source the caller's configuration does not declare (distinct from declared-but-unread,
#: which is None and contagious).
NOT_DECLARED = object()

#: `shared.consumption` adjudication verdicts, by value, so this module imports no reader.
_VERDICT_UNCONSUMED = "unconsumed"
_VERDICT_UNKNOWN = "unknown"


def direct_consumption_state(
    card_count: Optional[int],
    publication_live: Any = NOT_DECLARED,
    flagged: Any = NOT_DECLARED,
    verdict: Optional[str] = None,
    verdict_reason: Optional[str] = None,
    has_downstream_dataflows: bool = False,
) -> Optional[bool]:
    """Whether a dataset has no *terminal* consumer: True, False, or None (unknown).

    Moved from the data pillar (D2 ticket 10) so instance-documentation's consumption signal
    decides a dataset's direct state with cleanup's rule, not a second one. The facts, in the
    order they are asked:

    * `card_count` — `Cards and Datasets` linkage; None when the report was unavailable.
    * `publication_live` — a publication with a live subscriber is a terminal consumer
      (ticket 65), asked BEFORE the enumeration because the enumeration cannot see it.
      `NOT_DECLARED` skips it; None (declared, unreadable or no summary) is contagious.
    * `flagged` — the enumeration pre-filter (`ConsumptionCounts.flagged`); `NOT_DECLARED`
      means no pre-filter is configured and an uncarded dataset is directly unconsumed.
    * `verdict` / `verdict_reason` — lineage adjudication of a flagged dataset; None when no
      adjudication ran or it gave none.
    * `has_downstream_dataflows` — whether the governance graph carries the downstream edge.

    Dataflow edges intentionally do not decide this value. They are graph edges, not a reason
    to stop the question: a staging layer is consumed only when some path through it reaches a
    real consumer. Cards, views and alerts are terminal consumers. ``None`` is contagious rather
    than optimistic, preserving the no-false-positive rule.
    """
    if card_count is None:
        return None
    if card_count > 0:
        return False
    if publication_live is not NOT_DECLARED:
        if publication_live:
            return False
        if publication_live is None:
            return None
    if flagged is NOT_DECLARED:
        return True
    if flagged is None:
        return None
    if not flagged:
        return True
    if verdict is None or verdict == _VERDICT_UNKNOWN:
        return None
    if verdict == _VERDICT_UNCONSUMED:
        return True
    if verdict_reason == "downstream-dataflow" and has_downstream_dataflows:
        # Product lineage confirmed the same kind of edge the governance graph carries; the
        # closure owns its meaning. If the edge is absent from that graph, it cannot safely be
        # followed and stays unknown.
        return True
    return False


def combine_successors(states: List[Optional[bool]]) -> Optional[bool]:
    """All paths must be dead: a real consumer wins, uncertainty abstains second."""
    if any(state is False for state in states):
        return False
    if any(state is None for state in states):
        return None
    return True


def unconsumed_states(
    dataset_ids: Iterable[str],
    dataflow_outputs: Mapping[str, Sequence[str]],
    direct_state: Callable[[str], Optional[bool]],
    downstream_dataflows: Callable[[str], Iterable[str]],
    pinned_consumed: Iterable[str] = (),
) -> Dict[str, Optional[bool]]:
    """Greatest fixed point of the dataset -> dataflow -> dataset consumer graph.

    Returns `node_key(kind, id) -> True` (unconsumed) / `False` (consumed) / `None` (unknown)
    for every dataset in `dataset_ids`, every output of every dataflow in `dataflow_outputs`,
    and every dataflow.

    `direct_state(dataset_id)` says whether a dataset has no *terminal* consumer — a card, a
    view, an alert, a live publication — as True, False or None. Dataflow edges intentionally
    do not decide it: a staging layer is consumed only when some path through it reaches a
    real consumer, and `None` is contagious rather than optimistic. `pinned_consumed` holds
    node keys forced to consumed (cleanup's ledger-retained candidates).

    Starting every decidable node at ``True`` makes a closed component with no terminal
    consumer unconsumed. Repeatedly propagating terminal consumers and unknown edges then
    reaches the deterministic fixed point for chains, branches and cycles alike. A dataset
    feeding a dataflow outside `dataflow_outputs` is unknown: the graph cannot follow it.
    """
    pinned = set(pinned_consumed)
    all_datasets = set(dataset_ids)
    for outputs in dataflow_outputs.values():
        all_datasets.update(outputs)

    states: Dict[str, Optional[bool]] = {}
    successors: Dict[str, List[str]] = {}
    for dataset_id in all_datasets:
        key = node_key(DATASET, dataset_id)
        direct = direct_state(dataset_id)
        if key in pinned:
            states[key] = False
            continue
        if direct is not True:
            states[key] = direct
            continue
        dataflows = downstream_dataflows(dataset_id)
        if not dataflows:
            states[key] = True
            continue
        successors[key] = []
        for dataflow_id in dataflows:
            if dataflow_id not in dataflow_outputs:
                states[key] = None
                successors.pop(key)
                break
            successors[key].append(node_key(DATAFLOW, dataflow_id))

    for dataflow_id, outputs in dataflow_outputs.items():
        key = node_key(DATAFLOW, dataflow_id)
        if key in pinned:
            states[key] = False
            continue
        if not outputs:
            states[key] = True
            continue
        successors[key] = [node_key(DATASET, dataset_id) for dataset_id in outputs]

    for key in successors:
        states.setdefault(key, True)
    changed = True
    while changed:
        changed = False
        for key, targets in successors.items():
            state = combine_successors([states.get(target) for target in targets])
            if states.get(key) != state:
                states[key] = state
                changed = True
    return states
