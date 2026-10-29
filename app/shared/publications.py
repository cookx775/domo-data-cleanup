"""Publication to another instance, as the FIFTH consumer type. Ticket 65.

Domo Publish copies a dataset into a subscribing instance, where it feeds that instance's
cards. No signal in the report roster carries the edge: not `Cards and Datasets`, not
`Dataflow Details`, not the dataset enumeration's `impactCardCount`, and not product lineage —
the lineage impact index stops at the instance boundary. So a published dataset agrees with
itself at zero on every aggregate read the collector had, and on Altimate's first run that put
its own governance reports — `Usage Metrics`, `Cards and Datasets`, `Dataset Schemas`, each
replicated into granite thirty times a month — at ranks 7–13 of the head as `unconsumed`. 24 of
the 61 head rows and 51% of the opportunity figure were published datasets.

This is the view edge (ticket 28) and the drill view (ticket 31) a third time: a real reader,
invisible to every index the collector reads, and it cannot be folded into the lineage
adjudication because the pre-filter never flags it — every union field reads 0. It needs its
own read, and the read is cheap: **one list, one summaries call, one detail per publication**.
Bounded by how many publications an instance has (Altimate 7, granite 55), never by the
population, so ADR-0004 is not engaged.

## What the API says, measured 2026-09-03 on both instances

* `GET /api/publish/v2/publications` lists what THIS instance publishes. Its `children` array
  is EMPTY on the list and its `subscribers` array is `[]` on every item on both instances —
  that field is not populated at this endpoint and must never be read as "no subscribers".
* `GET /api/publish/v2/publications/{id}` carries the members as `children[].content` with
  `domoObjectId` and `type` (`DATASET`, `PAGE`, `DATA_APP`, ...). `subscribers` is `[]` here
  too.
* `GET /api/publish/v2/publications/summaries` is the publisher-side endpoint that names the
  live subscriber count: `totalSubscribers` and `subscribers` (an integer here) per
  publication, plus `invalidSubscribers`, `status` and `executionStatusDescription`. It is
  POPULATED: every one of Altimate's 7 publications reports 1, granite's 53 report 1–11, and
  the figure cross-checks against the subscriber side — the 5 Altimate publications granite
  subscribes to all show granite's subscription as `READY`, and all 16 of Altimate's
  subscriptions from granite map to a granite publication reporting at least one subscriber.
  The `subscription/summaries` endpoint on the RECEIVING side carries a `totalSubscribers: 0`
  inside its nested `publicationSummary`; that nested copy is the unpopulated one.
* Two of granite's 55 publications (`type: CONFIG`) have no summary at all. A publication with
  no summary has an UNKNOWN subscriber count, and a dataset whose only publication is one of
  those is `unknown`, never `consumed` and never `unconsumed` (ADR-0003).

## The decision this module implements

A dataset in a publication with at least one subscriber **is consumed** — it feeds cards on
another instance — so publication joins cards, downstream dataflows, view edges and alerts in
the union `unconsumed` negates, and a live publication removes the candidate exactly as a
lineage-confirmed card does (ticket 28 decision 3). A dataset published to **nobody** is
closer to unconsumed than consumed: it keeps the condition and the publication is evidence on
the row, never a consumer. The two are not folded.

The subscriber COUNT from the publisher's own API is the whole signal. The cards the copy feeds
on the subscribing instance are never read: a run reads its own instance (ticket 14), and the
publisher's summaries endpoint is enough to know a reader exists without looking at it.
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Optional, Set

log = logging.getLogger("collector.publications")

PUBLICATIONS_PATH = "/api/publish/v2/publications"
SUMMARIES_PATH = "/api/publish/v2/publications/summaries"
DETAIL_PATH = "/api/publish/v2/publications/{id}"

#: The `children[].content.type` value for a dataset member. Pages, cards and data apps are
#: members too; only datasets are this pillar's assets.
MEMBER_DATASET = "DATASET"


class Publication:
    """One publication as the publisher's own API describes it, members and subscriber count
    joined from two endpoints. `total_subscribers` is **None** when the summaries endpoint did
    not describe this publication — granite's two `CONFIG` publications — which is not zero."""

    __slots__ = (
        "id",
        "name",
        "type",
        "dataset_ids",
        "member_count",
        "total_subscribers",
        "invalid_subscribers",
        "status",
        "execution_status",
    )

    def __init__(self, publication_id: str, name: str, publication_type: Optional[str]) -> None:
        self.id = publication_id
        self.name = name
        self.type = publication_type
        self.dataset_ids: Set[str] = set()
        self.member_count = 0
        self.total_subscribers: Optional[int] = None
        self.invalid_subscribers: Optional[int] = None
        self.status: Optional[str] = None
        self.execution_status: Optional[str] = None

    @property
    def live(self) -> Optional[bool]:
        """Whether anyone receives this publication: True, False, or unknown (no summary)."""
        if self.total_subscribers is None:
            return None
        return self.total_subscribers > 0

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "datasets": len(self.dataset_ids),
            "members": self.member_count,
            "total_subscribers": self.total_subscribers,
            "invalid_subscribers": self.invalid_subscribers,
            "status": self.status,
            "execution_status": self.execution_status,
        }


class Publications:
    """Every publication this instance makes, keyed by dataset for the pillar's question.

    Built by `read` from the live API or by `from_payloads` from captured payloads in the same
    shape, so a fixture exercises the same parsing the run does.
    """

    __slots__ = ("publications", "_by_dataset", "calls")

    def __init__(self) -> None:
        self.publications: Dict[str, Publication] = {}
        self._by_dataset: Dict[str, List[str]] = {}
        self.calls = 0

    # -- construction --------------------------------------------------------------------

    @classmethod
    def read(cls, client) -> "Publications":
        """One list, one summaries call, one detail per publication. Any failure raises —
        a partial picture of what is published cannot support a single `unconsumed` verdict,
        because the dataset whose publication was not read is exactly the one that would be
        scored wrongly. The caller records the exception as a coverage exception and takes
        `unconsumed` off the run, following `load_consumption`'s precedent."""
        listing = client._request("GET", PUBLICATIONS_PATH).json()
        summaries = client._request("GET", SUMMARIES_PATH).json()
        details = []
        for item in listing if isinstance(listing, list) else []:
            publication_id = item.get("id")
            if not publication_id:
                continue
            details.append(client._request("GET", DETAIL_PATH.format(id=publication_id)).json())
        self = cls.from_payloads(listing, summaries, details)
        self.calls = 2 + len(details)
        return self

    @classmethod
    def from_payloads(cls, listing, summaries, details: Iterable[dict]) -> "Publications":
        self = cls()
        for item in listing if isinstance(listing, list) else []:
            publication_id = item.get("id")
            if not publication_id:
                continue
            self.publications[publication_id] = Publication(
                publication_id, item.get("name") or "", item.get("type")
            )
        for summary in summaries if isinstance(summaries, list) else []:
            publication = self.publications.get(summary.get("id") or "")
            if publication is None:
                continue
            publication.total_subscribers = _as_count(summary.get("totalSubscribers"))
            publication.invalid_subscribers = _as_count(summary.get("invalidSubscribers"))
            publication.status = summary.get("status")
            publication.execution_status = summary.get("executionStatusDescription")
        for detail in details:
            publication = self.publications.get((detail or {}).get("id") or "")
            if publication is None:
                continue
            for child in detail.get("children") or []:
                content = child.get("content") or {}
                publication.member_count += 1
                if content.get("type") != MEMBER_DATASET:
                    continue
                dataset_id = str(content.get("domoObjectId") or "")
                if not dataset_id:
                    continue
                publication.dataset_ids.add(dataset_id)
                self._by_dataset.setdefault(dataset_id, []).append(publication.id)
        return self

    # -- the pillar's questions -----------------------------------------------------------

    def publications_of(self, dataset_id: str) -> List[Publication]:
        return [self.publications[p] for p in self._by_dataset.get(dataset_id, [])]

    def live(self, dataset_id: str) -> Optional[bool]:
        """Three states. **True**: some publication carrying this dataset has a subscriber —
        a consumer exists. **False**: the dataset is unpublished, or published only to nobody.
        **None**: no live publication, but at least one carrying it has an unknown subscriber
        count, so neither answer is available."""
        publications = self.publications_of(dataset_id)
        if any(p.live for p in publications):
            return True
        if any(p.live is None for p in publications):
            return None
        return False

    def published_without_subscribers(self, dataset_id: str) -> List[str]:
        """Names of the publications carrying this dataset to nobody — evidence for a row
        that KEEPS `unconsumed`."""
        return sorted(
            p.name for p in self.publications_of(dataset_id) if p.live is False
        )

    def __iter__(self):
        """Dataset ids in publications, sorted, for the input fingerprint."""
        return iter(sorted(self._by_dataset))

    def stats(self) -> dict:
        live = [p for p in self.publications.values() if p.live]
        unknown = [p for p in self.publications.values() if p.live is None]
        datasets = set(self._by_dataset)
        return {
            "publications": len(self.publications),
            "publications_with_subscribers": len(live),
            "publications_without_summary": len(unknown),
            "published_datasets": len(datasets),
            "published_datasets_with_live_subscriber": sum(
                1 for d in datasets if self.live(d) is True
            ),
            "published_datasets_subscriber_unknown": sum(
                1 for d in datasets if self.live(d) is None
            ),
            "calls": self.calls,
        }


def _as_count(value) -> Optional[int]:
    """A count, or None for a value the payload did not carry. Never 0 for absent: the
    difference between "no subscribers" and "the summary did not say" is the whole reason
    `Publication.live` has three states."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
