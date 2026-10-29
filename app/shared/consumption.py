"""Consumption, from the dataset enumeration and a bounded lineage pass. Ticket 31.

Two readers over two calls, and the split between them is the whole design
([ADR-0004](../docs/adr/0004-bounded-per-asset-reads.md), ticket 28 decision 1):

* **`ConsumptionCounts`** is a second reader over the `datasources/search` enumeration the
  collector already pays 12 calls for (ticket 15, and `schemas.py` for `duplicated`). It is
  a **pre-filter with no false negatives** — sound as a filter, unsound as a verdict, and
  measured that way over all 659 granite dataset candidates.
* **`adjudicate`** spends one product-lineage call per **flagged** candidate and is the
  **authority**. Granite: 31 calls.

Nothing here judges `unconsumed`. `ConsumptionCounts` returns counts and a flag; `adjudicate`
returns a verdict per contested dataset. The pillar owns the condition.

## Why lineage is the authority, and no card-keyed read can corroborate it

Ticket 31 tried to corroborate lineage's DIRECT-card claims against a card-keyed authority
and found that **no such authority exists**. The two granite candidates lineage called
directly consumed — `RXA | Dataset Cleanup | Dependencies` and its `ENT_RXA` twin — report
**zero** cards on every card-keyed surface at once:

| authority | keyed on | says |
|---|---|---|
| `DOMO | Cards and Datasets` (governance) | card -> dataset | 0 |
| full 10,742-card census, `dataSourceIds` + `dataSubscriptions` | the card index | 0 |
| `/api/search/v1/query` filtered on `dataSourceIds` | the card index | 0 |
| product lineage `descendantCounts.CARD` | the impact index | **1** |
| `cardInfo.cardCount`, both surfaces | the impact index | **1** |

Lineage is right. The consumer is card `776064037`, `type: drill_view`, `active: true` —
**"Datasets | Detailed Report | Drill to Views and Cards"** — which reads the candidate
directly and is the drill target of the live card `32545820`. A `drill_view` is **not in the
card index**: filtering the card search on its own `databaseId` returns
`totalResultCount: 0`. So every card-keyed read misses it *structurally*, and quarantining
the dataset would break a live card's drill-through.

This is ticket 30's **silently-ignored filter** wearing a new dress, and the more dangerous
form: not a filter the server ignored, but a **confident zero from an index that excludes the
entity kind**. No typed reader can catch it and no positive control on a *normal* card can
either — the control passes, because normal cards are indexed. The lesson generalises past
this endpoint: *a count is only as complete as its index, and an index's exclusions are not
visible in its responses.*

It also explains why `impactCardCount` is the pre-filter that works. It and lineage read the
**same impact index**, which is the only one that sees a drill view — so the filter cannot
miss what the authority will find. Their agreement is structural, not lucky.
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Optional, Set

# The landing reader (and where the landing timestamp comes from, for the typed value's
# provenance) is shared with instance-documentation (D2 ticket 09).
from shared.landing import LANDING_FIELD, SEARCH_REPORT, landing_raw
from shared.typed import Value

log = logging.getLogger("collector.consumption")

LINEAGE_PATH = "/api/data/v1/lineage/{kind}/{id}"
DATA_SOURCE = "DATA_SOURCE"

TAGS_FIELD = "tagsList"

#: Ticket 28 decision 1's wide union. `cardInfo.cardCount` is deliberately EXCLUDED as a
#: strict subset of `impactCardCount` (18 of 28 on granite, 0 unique).
#:
#: The downstream-DATASET and DATAFLOW signals are not here to catch consumption —
#: `impactCardCount` alone is sufficient on granite — but because a chain reaching no card is
#: precisely a DEAD CHAIN, and `adjudicate` describes the ones it sees (decision 5).
#:
#: **The no-false-negative property is scoped to CONSUMPTION, not to the dead-chain
#: description.** Measured on granite 2026-09-01: all 11 consumed candidates are caught, so no
#: live consumer is ever missed — that is the property that matters and it holds. But of the 6
#: dataset-grain dead chains the census finds, the union flags **1**. The other 5 report
#: lineage DATA_SOURCE descendants of 1-4 while every one of the seven fields above reads 0,
#: so no widening of this union can reach them; only a lineage call over the whole population
#: could, which ADR-0004 forbids. The consequence is bounded and is evidence-only: a dead
#: chain still scores `unconsumed`, which is the correct verdict, it just does not gain the
#: sentence naming its downstream. Ticket 22 takes its population from the census, not from
#: this evidence.
#:
#: The ALERT signals are definitional, not empirical: both fire zero times on granite. A
#: dataset whose only reader is an alert is still being read, and the alternative is
#: recommending quarantine on something that pages a human when it stops (decision 2).
UNION_FIELDS = (
    "impactCardCount",
    "dataSourceCount",
    "impactDataSourceCount",
    "dataFlowCount",
    "impactDataFlowCount",
    "alertCount",
    "impactAlertCount",
)

#: What `adjudicate` can conclude. `UNKNOWN` is not `UNCONSUMED` — the missing-card-report
#: precedent exactly (ticket 28 decision 7).
CONSUMED = "consumed"
UNCONSUMED = "unconsumed"
UNKNOWN = "unknown"


class ConsumptionCounts:
    """Per-dataset consumption counts, read off the dataset enumeration.

    `flagged` returns **None** for a dataset the enumeration never described, which is not
    `False`: a dataset absent from the enumeration cannot be scored `unconsumed` without the
    signal (ticket 28 decision 8). On granite that bounds to 0-1 per run.
    """

    __slots__ = ("_counts", "_landing", "_tags", "datasets_seen")

    def __init__(self) -> None:
        self._counts: Dict[str, Dict[str, int]] = {}
        #: Raw `lastUpdated` per dataset, for datasets whose item CARRIED the key. Resolved
        #: through the typed boundary in `landing`, never read directly.
        self._landing: Dict[str, object] = {}
        #: Domo's dataset tags, read from the same enumeration. A missing key remains
        #: unknown; an empty list is a positive statement that the dataset is untagged.
        self._tags: Dict[str, List[str]] = {}
        self.datasets_seen = 0

    @classmethod
    def from_search(cls, items: Iterable[dict]) -> "ConsumptionCounts":
        self = cls()
        for item in items:
            dataset_id = item.get("id") or ""
            if not dataset_id:
                continue
            self.datasets_seen += 1
            row = {field: _as_count(item.get(field)) for field in UNION_FIELDS}
            # Carried as EVIDENCE only, never in the union: it is the better direct signal
            # inside the search payload (matching the live card census 2,022 times of 2,205
            # against `impactCardCount`'s 1,315, ticket 30), and a strict subset of
            # `impactCardCount` over the candidates.
            row["cardInfo.cardCount"] = _as_count((item.get("cardInfo") or {}).get("cardCount"))
            self._counts[dataset_id] = row
            # Ticket 43: when data last LANDED, a third reader over the same payload. Only a
            # key the item actually carries is recorded — a payload that never mentions it
            # is `unknown` downstream, not `never`.
            if LANDING_FIELD in item:
                self._landing[dataset_id] = item[LANDING_FIELD]
            if TAGS_FIELD in item:
                raw_tags = item[TAGS_FIELD]
                if raw_tags is None:
                    # The live surface returned a list on every sampled item. Treat a null
                    # as unknown rather than turning it into the stronger claim "untagged".
                    continue
                if not isinstance(raw_tags, list) or any(
                    not isinstance(tag, str) for tag in raw_tags
                ):
                    # Tags are evidence, not a condition. An unexpected shape must not
                    # block scoring, and it must not be presented as an empty tag list.
                    continue
                self._tags[dataset_id] = sorted(
                    {tag.strip() for tag in raw_tags if tag.strip()},
                    key=lambda tag: (tag.casefold(), tag),
                )
        return self

    def counts(self, dataset_id: str) -> Optional[Dict[str, int]]:
        return self._counts.get(dataset_id)

    def landing(self, dataset_id: str) -> Optional[Value]:
        """When data last landed on this dataset, as a typed value — or **None** when the
        enumeration did not speak about it (dataset not described, or item without the key).

        Ticket 43. `Last_Run_Date` in DomoStats is a run ATTEMPT; this is the landing, and a
        connector that runs on schedule returning nothing has the first and not the second.

        `0` — the surface's spelling of "never landed" — is resolved into `absent` by the
        shared reader, `shared.landing.landing_raw`, where the measurement behind it lives.
        """
        if dataset_id not in self._landing:
            return None
        return Value(landing_raw(self._landing[dataset_id]), LANDING_FIELD, SEARCH_REPORT)

    def tags(self, dataset_id: str) -> Optional[List[str]]:
        """The dataset's tags, or ``None`` when the enumeration did not describe them.

        A copy is returned so a report helper cannot mutate the evidence shared by every
        candidate for the same asset. Empty means Domo explicitly returned ``tagsList: []``;
        it is not the same as a missing/null/malformed field.
        """
        tags = self._tags.get(dataset_id)
        return list(tags) if tags is not None else None

    def flagged(self, dataset_id: str) -> Optional[bool]:
        """True when ANY union signal is non-zero. None when the enumeration did not
        describe this dataset at all."""
        row = self._counts.get(dataset_id)
        if row is None:
            return None
        return any(row[field] > 0 for field in UNION_FIELDS)

    def signals(self, dataset_id: str) -> List[str]:
        """The non-zero union signals, for the evidence line. A reviewer asked to believe a
        candidate was removed needs to see WHICH count spoke."""
        row = self._counts.get(dataset_id)
        if row is None:
            return []
        return [f"{field}={row[field]}" for field in UNION_FIELDS if row[field] > 0]

    def __len__(self) -> int:
        return len(self._counts)

    def __iter__(self):
        """The dataset ids the enumeration described, in insertion order. The input
        fingerprint (ticket 42) hashes every row through `counts()`; nothing else needs
        to walk the table."""
        return iter(self._counts)

    def stats(self) -> dict:
        flagged = sum(1 for i in self._counts if self.flagged(i))
        return {
            "datasets_seen": self.datasets_seen,
            "datasets_with_counts": len(self._counts),
            "datasets_flagged": flagged,
            # An instance whose payload stops carrying `lastUpdated` shows up here as a
            # count rather than as every dataset quietly reading `landing unknown`.
            "datasets_without_landing_field": len(self._counts) - len(self._landing),
            "datasets_without_tags_field": len(self._counts) - len(self._tags),
        }


def _as_count(value) -> int:
    """A count, or 0. Absence and zero are NOT conflated here and this is the one place that
    deserves saying so: an absent count in the union would be indistinguishable from a zero
    one either way, because the union asks `> 0` and both answers are False. What ADR-0003
    forbids is asking an absent for a number and PRESENTING the answer — which is why
    `flagged` returns None for a dataset the enumeration never described at all, a state
    this function never sees."""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class Adjudication:
    """Verdicts over the contested set, plus the evidence each one rests on."""

    __slots__ = ("verdicts", "evidence", "calls", "unreachable", "warned_above")

    def __init__(self) -> None:
        self.verdicts: Dict[str, str] = {}
        self.evidence: Dict[str, dict] = {}
        self.calls = 0
        self.unreachable: List[str] = []
        self.warned_above = False

    def verdict(self, dataset_id: str) -> Optional[str]:
        return self.verdicts.get(dataset_id)

    def stats(self) -> dict:
        return {
            "adjudicated": len(self.verdicts),
            "consumed": sum(1 for v in self.verdicts.values() if v == CONSUMED),
            "unconsumed": sum(1 for v in self.verdicts.values() if v == UNCONSUMED),
            "unknown": sum(1 for v in self.verdicts.values() if v == UNKNOWN),
            "lineage_calls": self.calls,
            "unreachable": len(self.unreachable),
        }


def adjudicate(client, contested: Iterable[str], warn_above: Optional[int] = None) -> Adjudication:
    """One product-lineage call per contested dataset. **Never capped**.

    A cap's failure mode is downgrading real consumption evidence to `unknown`, which is the
    bug this pass exists to close, so this function never applies `max_calls`
    (ticket 28 decision 6; "a silent cap is the 1,000,000-row truncation wearing our own
    name").

    Unreachable lineage returns `UNKNOWN` for that candidate, not `UNCONSUMED` — bounded by
    the pre-filter to ~30 on granite rather than all 659 (decision 7).

    A DIRECT card and a TRANSITIVE one are both `CONSUMED`. They are recorded separately
    because they mean different things to a reviewer — a governance-report gap versus a view
    or blend edge no report carries — but neither is a lesser authority. See the module
    docstring: the one attempt to treat a direct claim as weaker, by corroborating it against
    a card-keyed read, would have kept the two granite candidates whose consumer is a live
    card's drill view.
    """
    from shared.domo import DomoError  # local: `domo` imports nothing from here, keep it that way

    # Legacy warn_above arguments are accepted but no longer drive warnings.
    # Workload warnings require a completed comparable prior run.
    result = Adjudication()
    contested = sorted(set(contested))
    log.info("adjudicating %d contested dataset(s); every dataset will be checked", len(contested))

    for dataset_id in contested:
        try:
            payload = client._request(
                "GET", LINEAGE_PATH.format(kind=DATA_SOURCE, id=dataset_id)
            ).json()
            result.calls += 1
        except DomoError as exc:
            result.verdicts[dataset_id] = UNKNOWN
            result.evidence[dataset_id] = {
                "reason": "lineage-unreachable",
                "detail": f"{type(exc).__name__}: {exc}",
            }
            result.unreachable.append(dataset_id)
            continue

        node = payload.get(f"{DATA_SOURCE}{dataset_id}") or {}
        counts = node.get("descendantCounts") or {}
        children = node.get("children") or []
        cards = _as_count(counts.get("CARD"))
        alerts = _as_count(counts.get("ALERT"))
        dataflows = _as_count(counts.get("DATAFLOW"))
        datasets = _as_count(counts.get("DATA_SOURCE"))
        card_is_direct = any(c.get("type") == "CARD" for c in children)

        if cards > 0:
            result.verdicts[dataset_id] = CONSUMED
            result.evidence[dataset_id] = {
                "reason": "card-report-incomplete" if card_is_direct else "view-edge",
                "detail": (
                    f"product lineage reaches {cards} card(s) "
                    + (
                        "hanging DIRECTLY off this dataset that `DOMO | Cards and Datasets` "
                        "does not carry — including, on granite, a `drill_view` card that no "
                        "card index contains at all"
                        if card_is_direct
                        else f"through {datasets} downstream dataset(s) with {dataflows} "
                             f"downstream dataflow(s) — a view or blend edge carried by no "
                             f"governance report"
                    )
                ),
                "lineage_cards": cards,
                "card_is_direct": card_is_direct,
            }
            continue

        if alerts > 0:
            # Decision 2. An alert notifies a human when this stops; that is a reader.
            result.verdicts[dataset_id] = CONSUMED
            result.evidence[dataset_id] = {
                "reason": "alert-consumer",
                "detail": f"product lineage reaches {alerts} alert(s)",
                "lineage_alerts": alerts,
            }
            continue

        if dataflows > 0:
            result.verdicts[dataset_id] = CONSUMED
            result.evidence[dataset_id] = {
                "reason": "downstream-dataflow",
                "detail": (
                    f"product lineage reports {dataflows} downstream dataflow(s) where the "
                    f"governance edge table reported none"
                ),
                "lineage_downstream_dataflows": dataflows,
            }
            continue

        # Decision 5: the dead chains KEEP `unconsumed` and gain evidence naming the
        # downstream dataset that terminates without a consumer. This hands ticket 22 its
        # population and deliberately does not resolve it.
        result.verdicts[dataset_id] = UNCONSUMED
        if datasets > 0:
            result.evidence[dataset_id] = {
                "reason": "dead-chain",
                "detail": (
                    f"{datasets} downstream dataset(s) reaching 0 cards — the chain is dead "
                    f"end to end, so flagging its head is correct. Whether the chain collapses "
                    f"into one candidate is ticket 22's open decision, not this pass's"
                ),
                "lineage_downstream_datasets": datasets,
            }
        else:
            result.evidence[dataset_id] = {
                "reason": "no-consumer",
                "detail": (
                    "the enumeration's counts flagged this dataset and product lineage found "
                    "no card, alert, dataflow or dataset downstream of it — the enumeration "
                    "over-reported, which is why it is a filter and not the authority"
                ),
            }

    return result
