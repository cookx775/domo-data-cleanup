"""The edge graph. **Collects only, judges nothing** (scoping Q10).

Every consumption question the data pillar asks is answered from here, and every answer is
a count or a set — never a verdict. Keeping the judgement out means the graph can be
reused by the content pillar and by the chain analysis that ticket 22 will decide, without
either inheriting the `unconsumed` test's current shape.

Three edge families, from two reports:

    dataflow --produces--> dataset     `Dataflow Details`.`Output Dataset ID`
    dataset  --feeds-----> dataflow    `Dataflow Details`.`Input Dataset ID`
    dataset  --powers----> card        `Cards and Datasets`.`Dataset ID`

The two dataflow families come from the same table and the same identifier space (the
Governance UUID). The DomoStats numeric dataflow id is a DIFFERENT identifier space that
does not join and is never mixed in here.
"""

from __future__ import annotations

from typing import Dict, Optional, Set

from .typed import Result


class UnmeasuredPageStateError(Exception):
    """`Cards and Datasets` presented a card state the placement floor has never measured.

    The floor counts cards with no positive `Page ID` (ticket 58, deciding ticket 48), and it
    is claimed on the strength of exactly two populations that were sampled against the
    product path: all-absent cards and all-negative-sentinel cards, both 20/20 on no page
    (ticket 40). A card that is in the floor and in neither population — absent and sentinel
    rows mixed with no positive id, or a `Page ID` of exactly 0 — carries no measurement at
    all. Folding it into either bucket would publish one population's verified no-page rate
    over another population's cards, which is the grain mistake ticket 24 made and ticket 40
    caught. So it is named and raised. Granite has 0 such cards (2026-09-04).
    """


class CardLinkage:
    """Card consumption of one dataset, as `DOMO | Cards and Datasets` records it.

    `cards` is the card count from the real linkage table and is what the pillar carries as
    consumption evidence. `cards_with_no_page_recorded` is a FLOOR: the number of those cards
    for which the report records no PAGE — no positive `Page ID` on any of the card's rows,
    so absent-only cards and all-sentinel cards alike (ticket 58; a card with any positive id
    is excluded even when it also carries a sentinel row). It is a floor because `Page ID`
    over-reports placement (ticket 24) — a card removed from a page keeps its row, and the
    negative range is a sentinel that says nothing about placement — so placement can be
    proved absent and never proved present. It is evidence about the CARDS, at the card
    grain, and it does not
    reduce `cards`: a card reads its dataset whether or not a person can navigate to it, and a
    drill view is a live consumer that sits on no page at all (`CONTEXT.md`, **Unplaced
    card**, **Drill view**). `pages` counts distinct positive page ids and over-reports too.

    Counts are keyword-only. A positional call could swap the card count for the floor
    without a type error, and a fixture that silently meant "every card unplaced" would
    invert the evidence it was written to carry.
    """

    __slots__ = ("dataset_id", "dataset_name", "cards", "cards_with_no_page_recorded", "pages")

    def __init__(
        self,
        dataset_id: str,
        dataset_name: Optional[str],
        *,
        cards: int,
        cards_with_no_page_recorded: int,
        pages: int,
    ) -> None:
        self.dataset_id = dataset_id
        self.dataset_name = dataset_name
        self.cards = cards
        self.cards_with_no_page_recorded = cards_with_no_page_recorded
        self.pages = pages


class Lineage:
    def __init__(self) -> None:
        self.outputs: Dict[str, Set[str]] = {}          # dataflow -> output datasets
        self.inputs: Dict[str, Set[str]] = {}           # dataflow -> input datasets
        self.produced_by: Dict[str, Set[str]] = {}      # dataset -> dataflows producing it
        self.feeds: Dict[str, Set[str]] = {}            # dataset -> dataflows consuming it
        self.output_names: Dict[str, str] = {}          # dataset -> name, as the edge saw it
        self.cards: Dict[str, CardLinkage] = {}         # dataset -> card linkage
        self.card_linkage_available = False
        #: Which edge families have been LOADED, as distinct from which have rows. A graph
        #: with no edges loaded and an instance with no dataflows look identical in
        #: `outputs`/`inputs`; the input fingerprint (ticket 42) must not hash them the same.
        self.edge_tables_loaded: Set[str] = set()

    # -- loading -------------------------------------------------------------------------

    def load_output_edges(self, result: Result) -> None:
        self.edge_tables_loaded.add("outputs")
        for row in result:
            dataflow = row.get("Dataflow ID").as_str()
            dataset = row.get("Output Dataset ID").as_str()
            self.outputs.setdefault(dataflow, set()).add(dataset)
            self.produced_by.setdefault(dataset, set()).add(dataflow)
            name = row.get("Output Dataset Name")
            if name.is_present:
                self.output_names[dataset] = name.as_str()

    def load_input_edges(self, result: Result) -> None:
        self.edge_tables_loaded.add("inputs")
        for row in result:
            dataflow = row.get("Dataflow ID").as_str()
            dataset = row.get("Input Dataset ID").as_str()
            self.inputs.setdefault(dataflow, set()).add(dataset)
            self.feeds.setdefault(dataset, set()).add(dataflow)

    def load_card_linkage(self, result: Result) -> None:
        for row in result:
            dataset = row.get("Dataset ID").as_str()
            name = row.get("Dataset Name")
            # Read before the floor it guards, and strictly for the same reason: the floor
            # must not be constructed at all for a dataset whose cards include a state
            # nobody measured. See `UnmeasuredPageStateError`.
            unclassified = row.get("cards_with_no_page_unclassified").as_int()
            if unclassified != 0:
                raise UnmeasuredPageStateError(
                    f"cards_and_datasets: dataset {dataset} has {unclassified} card(s) with "
                    f"no positive `Page ID` that are neither all-absent nor all-sentinel — "
                    f"either absent and sentinel rows on one card, or `Page ID` = 0. The "
                    f"placement floor's 20/20 no-page rate was measured on those two "
                    f"populations only (ticket 40), so this card cannot be folded into "
                    f"either. Measure the state, then widen the floor's decomposition."
                )
            self.cards[dataset] = CardLinkage(
                dataset_id=dataset,
                dataset_name=name.as_str(absent=None),
                cards=row.get("cards").as_int(absent=0),
                # Strict on purpose: a COUNT over a group is never NULL, so absence here
                # is a query bug, and a bare "" is a read that lost its typing. Neither
                # may become a zero that then reads as "every card is placed".
                cards_with_no_page_recorded=row.get("cards_with_no_page_recorded").as_int(),
                pages=row.get("pages").as_int(absent=0),
            )
        self.card_linkage_available = True

    # -- queries -------------------------------------------------------------------------

    def outputs_of(self, dataflow_id: str) -> Set[str]:
        return self.outputs.get(dataflow_id, set())

    def inputs_of(self, dataflow_id: str) -> Set[str]:
        return self.inputs.get(dataflow_id, set())

    def downstream_dataflows(self, dataset_id: str) -> Set[str]:
        """Dataflows this dataset feeds: consumer-graph edges, not terminal consumers.

        An intermediate is consumed when a path through it reaches a real consumer; a dead
        chain closes over these edges instead of treating the first edge as an exemption.
        """
        return self.feeds.get(dataset_id, set())

    def card_count(self, dataset_id: str) -> Optional[int]:
        """Cards on this dataset from the REAL linkage table, or None when that report was
        unavailable. None is not zero: without the linkage table the collector does not
        know, and a condition that treats not-knowing as no-consumers would recommend
        quarantining a consumed dataset."""
        if not self.card_linkage_available:
            return None
        linkage = self.cards.get(dataset_id)
        return linkage.cards if linkage else 0

    def cards_with_no_page_recorded(self, dataset_id: str) -> Optional[int]:
        """The placement FLOOR for this dataset's cards — see `CardLinkage` — or None when
        the linkage report was unavailable. A dataset with no cards has a floor of 0."""
        if not self.card_linkage_available:
            return None
        linkage = self.cards.get(dataset_id)
        return linkage.cards_with_no_page_recorded if linkage else 0

    def page_count(self, dataset_id: str) -> Optional[int]:
        if not self.card_linkage_available:
            return None
        linkage = self.cards.get(dataset_id)
        return linkage.pages if linkage else 0

    # -- shape ---------------------------------------------------------------------------

    def stats(self) -> dict:
        return {
            "dataflows_with_outputs": len(self.outputs),
            "dataflows_with_inputs": len(self.inputs),
            "output_edges": sum(len(v) for v in self.outputs.values()),
            "input_edges": sum(len(v) for v in self.inputs.values()),
            "datasets_produced": len(self.produced_by),
            "datasets_feeding_dataflows": len(self.feeds),
            "datasets_with_cards": len(self.cards),
            "card_linkage_available": self.card_linkage_available,
        }
