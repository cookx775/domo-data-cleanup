"""The graph queries: the reads every project that maps an instance needs.

Moved out of `collector/queries.py` when instance-documentation became their second consumer
(2026-10-14). Five queries — the dataflow grain, both dataflow edge families, the dataset grain,
and the card -> dataset linkage — are the asset graph's raw material. Everything that reads cost
or usage stays with instance-cleanup.

The rules that bind every query still apply here, unchanged (see `shared.sql`): an explicit
LIMIT, ADR-0003's banned idioms rejected before sending, nullable columns read through
`{?Column}`. instance-cleanup's drift test still enumerates these through
`collector.queries.ALL`, which composes them with its own.
"""

from __future__ import annotations

from .sql import Query, build, literal
from .typed import ABSENT_SENTINEL

# ---------------------------------------------------------------------------------------
# dataflow_details — the dataflow grain entire
# ---------------------------------------------------------------------------------------


def dataflow_grain(limit: int = 20_000) -> Query:
    """One row per dataflow, with observed edge counts alongside the declared ones.

    `Dataflow Details` is one row per dataflow x input-edge x output-edge, so the grain is
    reached by grouping. The attribute columns are functionally dependent on the id and
    grouping by them is what collapses the fan-out (granite: 1,812 rows -> 374 dataflows).

    `COUNT(DISTINCT ...)` on the edge columns excludes SQL NULL server-side, which is what
    makes `outputs_observed` honest: granite's DataFusions declare `Number of Outputs: 1`
    and observe 0. ADR-0003 decision 4 requires that disagreement be recorded rather than
    resolved in favour of either side.

    `DAP Dataflow ID` (ticket 44, after ticket 35) is the second identifier space the report
    carries: a UUID beside the numeric `Dataflow ID` that matches the UI URL. It is read only
    as a GUARD on the constructed deep link — a row whose `Dataflow ID` is not decimal or whose
    `DAP Dataflow ID` is absent is keyed from the wrong space and gets no link. Nullable, and
    on granite absent on exactly the three DataFusions the population filter removes.
    """
    return build(
        "dataflow_details",
        """
        SELECT {Dataflow ID}                        AS `Dataflow ID`,
               {?DAP Dataflow ID},
               {?Name},
               {?Database ProcessingType},
               {?Owner ID},
               {?Owner},
               {?Run State},
               {?Enabled},
               {?Draft},
               {?Last Execution Begin Time},
               {?Last Execution Failed},
               {?Last Execution State},
               {?Execution Count},
               {?Execution Success Count},
               {?Number of Outputs},
               COUNT(DISTINCT {Output Dataset ID}) AS outputs_observed,
               COUNT(DISTINCT {Input Dataset ID})  AS inputs_observed
        FROM table
        GROUP BY {Dataflow ID},
                 {~DAP Dataflow ID},
                 {~Name},
                 {~Database ProcessingType},
                 {~Owner ID},
                 {~Owner},
                 {~Run State},
                 {~Enabled},
                 {~Draft},
                 {~Last Execution Begin Time},
                 {~Last Execution Failed},
                 {~Last Execution State},
                 {~Execution Count},
                 {~Execution Success Count},
                 {~Number of Outputs}
        """,
        limit=limit,
        name="dataflow_grain",
    )


def dataflow_output_edges(limit: int = 50_000) -> Query:
    """dataflow -> output dataset. Granite: 458 edges over 333 dataflows.

    Rows with an absent `Output Dataset ID` are the input-edge rows of the same table;
    including them would give every dataflow a phantom output (ticket 06's first collector
    hazard).
    """
    return build(
        "dataflow_details",
        """
        SELECT {Dataflow ID}          AS `Dataflow ID`,
               {Output Dataset ID}    AS `Output Dataset ID`,
               {?Output Dataset Name}
        FROM table
        WHERE {Output Dataset ID} IS NOT NULL
        GROUP BY {Dataflow ID}, {Output Dataset ID}, {~Output Dataset Name}
        """,
        limit=limit,
        name="dataflow_output_edges",
        guaranteed=("Output Dataset ID",),   # the WHERE clause excludes absence
    )


def dataflow_input_edges(limit: int = 50_000) -> Query:
    """dataset -> dataflow. This is the edge that makes a dataset an INPUT to something,
    and therefore the edge that decides whether it is consumed."""
    return build(
        "dataflow_details",
        """
        SELECT {Dataflow ID}       AS `Dataflow ID`,
               {Input Dataset ID}  AS `Input Dataset ID`
        FROM table
        WHERE {Input Dataset ID} IS NOT NULL
        GROUP BY {Dataflow ID}, {Input Dataset ID}
        """,
        limit=limit,
        name="dataflow_input_edges",
        guaranteed=("Input Dataset ID",),    # the WHERE clause excludes absence
    )


# ---------------------------------------------------------------------------------------
# datasets — the dataset grain
# ---------------------------------------------------------------------------------------


def dataset_grain(limit: int = 50_000) -> Query:
    """One row per dataset. Granite: 2,205.

    `Status` here is a LAST-RUN STATE, not a lifecycle state, and 597 rows carry it absent.
    `Cards_Powered` is read but is NOT the consumption signal on its own — ticket 02 found
    it unreliable, so `cards_and_datasets` carries the real linkage and this is kept as
    corroborating evidence.
    """
    return build(
        "datasets",
        """
        SELECT {ID}         AS `ID`,
               {?Name},
               {?Link},
               {?Owner_User_ID},
               {?Created_Date},
               {?Last_Run_Date},
               {?Status},
               {?Schedule},
               {?Import_Type},
               {?Data_Provider},
               {?Cards_Powered}
        FROM table
        """,
        limit=limit,
        name="dataset_grain",
    )


# ---------------------------------------------------------------------------------------
# cards_and_datasets — the real card -> dataset linkage
# ---------------------------------------------------------------------------------------


def card_dataset_linkage(limit: int = 50_000) -> Query:
    """Per dataset: total cards, and the FLOOR on how many of them are on no page.

    `Page ID` is a stale snapshot that over-reports placement (ticket 24, measured against
    the product API 2026-09-01): a card removed from a page keeps its row, the negative
    range is a sentinel carrying no placement information, and even positive ids are stale
    10% of the time. The report never claims "no page" for a card that has one, so the one
    statement it supports is *the report records no page for this card* — a lower bound on
    unplaced cards (`CONTEXT.md`, **Unplaced card**), never a reachability measurement.

    `cards_with_no_page_recorded` is that floor, and since ticket 58 (deciding ticket 48) it
    is **cards with no POSITIVE `Page ID` on any row** — absent-only cards plus cards whose
    every row is a negative sentinel. Both populations measure the same on the product path,
    20/20 on no page at the card grain (ticket 40, 2026-09-02). A card carrying ANY positive
    id is excluded even when it also carries a sentinel row, which is the 702 granite cards
    whose sentinel sits beside a placement the product confirms. Read as `cards` minus cards
    with a positive id, so it is a floor by construction at whatever grain the group is. The
    counts are carried as evidence; neither decides consumption, which lineage owns, and a
    card reads its dataset whether or not anyone can navigate to it.

    `cards_with_no_page_unclassified` is the loud-failure guard, and it must be 0. The floor
    is claimed on the strength of exactly two measured populations, so it has to decompose
    into them: all-absent cards, plus all-negative cards. The column is
    `floor − all_absent − all_negative`, which by complement arithmetic over a group is
    `(cards with a non-absent row) + (cards with a non-negative row) − cards − (cards with a
    positive id)`. A non-zero value means the report is presenting a card state nobody has
    measured — a card mixing absent and sentinel rows with no positive id, or a `Page ID` of
    exactly 0 — and the loader raises rather than folding it into a bucket whose 20/20
    no-page rate was measured on a different population. Granite 2026-09-04: 0 over 788
    datasets, and 0 cards report-wide carry `Page ID = 0`.

    Absence is read through the sentinel form. `{?Page ID}` is the SELECT-list read and
    cannot alias inside an aggregate, so its expression twin `{~Page ID}` is compared to the
    sentinel — identical to `IS NULL` server-side (0 of 788 granite datasets disagree,
    2026-09-02) and visible to the drift test as an absence-preserving read. The reader then
    takes both counts strictly: a COUNT is never NULL, so an absent or `""` value in either
    column is a query bug and raises rather than reading as zero.

    `pages` counts DISTINCT POSITIVE page ids. The negative range is not a page: a card whose
    only page evidence is a sentinel is on no page 20/20 at the card grain, and where a
    sentinel card IS placed the page is unrelated to the id — so counting the range made 117
    granite datasets (287 cards) look placed on the strength of noise. (Ticket 24's 62% was
    measured over ROWS and mixed in the 702 cards that also carry a real placement; ticket 40
    corrected it at the grain the field aggregates at.) What remains is a recorded figure, not
    a measurement — it still over-reports (~10% stale).
    """
    return build(
        "cards_and_datasets",
        """
        SELECT {Dataset ID}                     AS `Dataset ID`,
               {?Dataset Name},
               COUNT(DISTINCT {Card ID})        AS cards,
               COUNT(DISTINCT {Card ID})
                 - COUNT(DISTINCT CASE WHEN {Page ID} > 0 THEN {Card ID} END)
                                                AS cards_with_no_page_recorded,
               COUNT(DISTINCT CASE WHEN {~Page ID} = """ + literal(ABSENT_SENTINEL) + """
                                        THEN NULL ELSE {Card ID} END)
                 + COUNT(DISTINCT CASE WHEN {~Page ID} = """ + literal(ABSENT_SENTINEL) + """
                                         OR {Page ID} >= 0 THEN {Card ID} END)
                 - COUNT(DISTINCT {Card ID})
                 - COUNT(DISTINCT CASE WHEN {Page ID} > 0 THEN {Card ID} END)
                                                AS cards_with_no_page_unclassified,
               COUNT(DISTINCT CASE WHEN {Page ID} > 0 THEN {Page ID} END)
                                                AS pages,
               COUNT(DISTINCT {Card Type})      AS card_types,
               COUNT(DISTINCT {Owner User ID})  AS card_owners,
               COUNT(DISTINCT {Title})          AS card_titles
        FROM table
        WHERE {Dataset ID} IS NOT NULL
        GROUP BY {Dataset ID}, {~Dataset Name}
        """,
        limit=limit,
        name="card_dataset_linkage",
        guaranteed=("Dataset ID",),          # the WHERE clause excludes absence
    )
