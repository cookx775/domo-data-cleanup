"""Input fingerprints: a compact, non-inventory record of what `unconsumed` was decided from.

Ticket 42. The change report says *that* candidates are new; it cannot say whether their
condition changed because an asset changed, a report changed content, or a coverage source
changed shape. On 2026-09-01 granite moved by 195 candidates between two runs five hours
apart with nothing in either artifact a reviewer could use to tell those apart.

Four inputs can change an `unconsumed` verdict, and each gets ONE fingerprint:

    dataflow_edges            the dataflow -> dataset / dataset -> dataflow edge graph,
                              from `Dataflow Details`
    card_linkage              the per-dataset card aggregate, from `Cards and Datasets`
    enumeration_consumption   the per-dataset union-signal counts, from the dataset
                              enumeration (`datasources/search`)
    publications              the per-dataset publication membership and each publication's
                              subscriber count, from the publish API (ticket 65)

A fingerprint is a SHA-256 over the input's canonical rows plus a handful of counts. It is
deliberately not the rows: the findings file already carries one unbounded artifact (the
candidate list) and a second one would be a second thing to diff, store and leak. Hashes
answer the only question the reviewer has — "did this input change between the two runs?" —
and the counts say roughly how.

**Absent is not changed is not zero** (ADR-0003, one level up). Each fingerprint carries a
`state`: `present` means the input was read and hashed, and a hash over ZERO rows is still
`present` — an edge table that loaded and was empty is a fact about the instance.
`unavailable` means the input was never read — report missing, enumeration incomplete, or
never offered to the run — and then `sha256` is null and `why` says which. An unavailable
input already shows up as a coverage note and takes its condition off the run; it must not
ALSO read as "the input changed", which is why `compare` reports `became-unavailable` and
never `changed` for it.

The scoring code is NOT fingerprinted here. On granite the 008 -> 009 shift coincided with
identical inputs and a scoring change (the chain closure of ticket 22) under an unmoved
`collector_version`; `run.collector_version` and `run.config_version` are compared in the
note for that reason, and their limits are recorded in the ticket rather than papered over.
"""

from __future__ import annotations

import hashlib
from typing import Dict, Iterable, Optional

from shared.consumption import UNION_FIELDS

INPUTS = ("dataflow_edges", "card_linkage", "enumeration_consumption", "publications")

STATE_PRESENT = "present"
STATE_UNAVAILABLE = "unavailable"

#: What `compare` can say about one input between two runs.
UNCHANGED = "unchanged"
CHANGED = "changed"
UNAVAILABLE_IN_BOTH = "unavailable-in-both"
BECAME_UNAVAILABLE = "became-unavailable"
BECAME_AVAILABLE = "became-available"
NOT_RECORDED_IN_PRIOR = "not-recorded-in-prior"

PRIOR_PRESENT = "present"
PRIOR_ABSENT = "absent"


def _digest(lines: Iterable[str]) -> str:
    """SHA-256 over the SORTED lines. Sorted so the fingerprint is a property of the input's
    content and not of the order a paged read happened to return it in."""
    joined = "\n".join(sorted(lines))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _present(sha256: str, counts: Dict[str, int]) -> dict:
    return {"state": STATE_PRESENT, "sha256": sha256, "counts": counts, "why": None}


def _unavailable(why: str) -> dict:
    return {"state": STATE_UNAVAILABLE, "sha256": None, "counts": None, "why": why}


def dataflow_edges(lineage) -> dict:
    """Both edge families from `Dataflow Details`, one line per edge.

    `Dataflow Details` is a floor report, so in a live run this is always `present`. The
    `unavailable` branch exists for the shape a partially constructed graph has — a test, or
    a future caller that loaded one family and not the other — because a graph with no edges
    loaded and a graph whose instance has no dataflows must never hash the same.
    """
    loaded = getattr(lineage, "edge_tables_loaded", set())
    if not {"outputs", "inputs"} <= set(loaded):
        missing = sorted({"outputs", "inputs"} - set(loaded))
        return _unavailable(
            f"the {' and '.join(missing)} edge table(s) were never loaded into the graph"
        )
    lines = [
        f"out\t{dataflow}\t{dataset}"
        for dataflow, datasets in lineage.outputs.items()
        for dataset in datasets
    ] + [
        f"in\t{dataflow}\t{dataset}"
        for dataflow, datasets in lineage.inputs.items()
        for dataset in datasets
    ]
    return _present(
        _digest(lines),
        {
            "dataflows_with_outputs": len(lineage.outputs),
            "dataflows_with_inputs": len(lineage.inputs),
            "output_edges": sum(len(v) for v in lineage.outputs.values()),
            "input_edges": sum(len(v) for v in lineage.inputs.values()),
        },
    )


def card_linkage(lineage) -> dict:
    """The per-dataset card aggregate — the ONE count the condition reads, `cards`.

    Deliberately not the placement evidence. `CardLinkage` also carries
    `cards_with_no_page_recorded` (a floor on unplaced cards, ticket 39) and `pages`; both are
    evidence about where a card sits, carried on the candidate for a reviewer, and neither can
    move an `unconsumed` verdict — the condition asks only whether `cards == 0`. A fingerprint
    that hashed them would report `changed` when a card was moved between pages and nothing
    that decides the condition moved, which is a false coincidence: the exact thing the note
    exists not to manufacture. So the hash is over what the verdict was decided from.
    """
    if not lineage.card_linkage_available:
        return _unavailable(
            "`Cards and Datasets` was not read this run, so no card aggregate exists to hash"
        )
    lines = [f"{dataset}\t{link.cards}" for dataset, link in lineage.cards.items()]
    return _present(
        _digest(lines),
        {
            "datasets_with_rows": len(lineage.cards),
            "cards_total": sum(link.cards for link in lineage.cards.values()),
        },
    )


def enumeration_consumption(counts, signal: str) -> dict:
    """The union-signal counts per dataset, from the enumeration.

    `signal` is the pillar's three-state `consumption_signal`. Both non-`present` states are
    `unavailable` here — there is nothing to hash either way — but `why` keeps them apart:
    an enumeration that could not be read completely is a coverage exception on the
    instance, while one that was never offered is a fact about the caller.
    """
    if counts is None or signal != "present":
        why = (
            "the dataset enumeration could not be read completely, so the consumption "
            "signal is a coverage exception this run and not a fingerprint"
            if signal == "unavailable"
            else "the dataset enumeration was never offered to the run"
        )
        return _unavailable(why)
    lines = []
    for dataset_id in counts:
        row = counts.counts(dataset_id) or {}
        lines.append(
            dataset_id + "\t" + ",".join(f"{field}={row.get(field, 0)}" for field in UNION_FIELDS)
        )
    stats = counts.stats()
    return _present(
        _digest(lines),
        {
            "datasets_seen": stats["datasets_seen"],
            "datasets_with_counts": stats["datasets_with_counts"],
            "datasets_flagged": stats["datasets_flagged"],
        },
    )


def publications(pubs, signal: str) -> dict:
    """Per published dataset, the publications carrying it and each one's subscriber count
    (ticket 65). A subscriber count that moves from 1 to 0 changes an `unconsumed` verdict
    with no dataset, edge or enumeration row moving, which is exactly the movement this
    block exists to name. An unknown count is spelled `?`, never `0`."""
    if pubs is None or signal != "present":
        why = (
            "the publish API could not be read, so publication is a coverage exception this "
            "run and not a fingerprint"
            if signal == "unavailable"
            else "the publish API was never offered to the run"
        )
        return _unavailable(why)
    lines = []
    for dataset_id in pubs:
        members = sorted(
            f"{p.id}={'?' if p.total_subscribers is None else p.total_subscribers}"
            for p in pubs.publications_of(dataset_id)
        )
        lines.append(dataset_id + "\t" + ",".join(members))
    stats = pubs.stats()
    return _present(
        _digest(lines),
        {
            "publications": stats["publications"],
            "publications_with_subscribers": stats["publications_with_subscribers"],
            "published_datasets": stats["published_datasets"],
            "published_datasets_with_live_subscriber": stats[
                "published_datasets_with_live_subscriber"
            ],
        },
    )


def capture(pillar) -> Dict[str, dict]:
    """All four fingerprints off a scored pillar. Keyed by `INPUTS`, always all four."""
    return {
        "dataflow_edges": dataflow_edges(pillar.lineage),
        "card_linkage": card_linkage(pillar.lineage),
        "enumeration_consumption": enumeration_consumption(
            pillar.consumption_counts, pillar.stats()["consumption_signal"]
        ),
        "publications": publications(pillar.publications, pillar.stats()["publications_signal"]),
    }


def _movement(current: dict, prior: Optional[dict]) -> str:
    if prior is None:
        return NOT_RECORDED_IN_PRIOR
    now_present = current.get("state") == STATE_PRESENT
    was_present = prior.get("state") == STATE_PRESENT
    if now_present and was_present:
        return UNCHANGED if current.get("sha256") == prior.get("sha256") else CHANGED
    if not now_present and not was_present:
        return UNAVAILABLE_IN_BOTH
    return BECAME_UNAVAILABLE if was_present else BECAME_AVAILABLE


def compare(current: Dict[str, dict], prior_document: Optional[dict]) -> dict:
    """Movement per input against the prior findings file.

    A prior run written before fingerprints existed — every granite run up to and including
    `granite-2026-09-01-010` — has none, and that is reported as `prior_fingerprints: absent`
    with every input `not-recorded-in-prior`. It is never reported as `unchanged`: a
    comparison with nothing on the other side has no result, and "unchanged" is a result.
    """
    prior_block = ((prior_document or {}).get("coverage") or {}).get("input_fingerprints")
    if not isinstance(prior_block, dict):
        return {
            "prior_fingerprints": PRIOR_ABSENT,
            "inputs": {name: NOT_RECORDED_IN_PRIOR for name in INPUTS},
        }
    return {
        "prior_fingerprints": PRIOR_PRESENT,
        "inputs": {
            name: _movement(current.get(name) or _unavailable("not captured"), prior_block.get(name))
            for name in INPUTS
        },
    }
