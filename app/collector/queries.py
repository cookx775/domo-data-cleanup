"""Every query the collector issues, in one module.

The five graph queries live in `shared.queries` (instance-documentation reads them too) and are
imported here so `ALL` and every `queries.<name>` call site stay whole.

Two reasons it is one module rather than one query per consumer:

* **The drift test enumerates it.** `ALL` below is the complete list, and
  `collector/tests/test_drift.py` asserts in both directions against `reports.yaml` —
  every column referenced appears in that report's read set, and every read-set column is
  referenced by some query. That is the mechanism that keeps the manifest honest once the
  queries start changing.
* **Aggregation belongs in SQL** (ticket 04 decision 4). The collector never bulk-pulls a
  raw history table, so the shape of what comes back is decided here rather than in a
  pillar.

Every nullable column is read through `{?Column}` so SQL NULL survives a transport that
serializes it as `""`. Row counts in the comments are granite 2026-08-28, the portfolio's
largest instance; limits sit well clear of them because the client raises when a result
lands exactly on its own ceiling.
"""

from __future__ import annotations

from typing import List

from shared.queries import (  # the graph queries; composed into ALL below
    card_dataset_linkage,
    dataflow_grain,
    dataflow_input_edges,
    dataflow_output_edges,
    dataset_grain,
)
from shared.sql import Query, build, literal

# ---------------------------------------------------------------------------------------
# credit_usage — metered cost
# ---------------------------------------------------------------------------------------


def credit_by_entity(domain: str, start: str, end: str, limit: int = 200_000) -> Query:
    """Metered credits per entity x sku x category over the window. Granite 90d: 2,286 groups.

    Kept at entity x sku x category rather than summed to the entity, because the sku
    decides which saving term a credit belongs to (execution stops on quarantine, storage
    only on delete) and the category decides whether it recurs at all (`manual` does not).
    Collapsing either here would make those decisions unrecoverable downstream.

    `date` is a 'YYYY-MM-DD' STRING and compares lexicographically.
    """
    return build(
        "credit_usage",
        f"""
        SELECT {{entityId}}      AS `entityId`,
               {{?entityType}},
               {{skuId}}         AS `skuId`,
               {{?skuGroup}},
               {{?category}},
               SUM({{creditsUsed}})    AS credits,
               SUM({{usageQuantity}})  AS quantity,
               COUNT(*)                AS metered_days
        FROM table
        WHERE {{domain}} = {literal(domain)}
          AND {{date}} >= {literal(start)}
          AND {{date}} <= {literal(end)}
          AND {{entityId}} IS NOT NULL
        GROUP BY {{entityId}}, {{~entityType}}, {{skuId}}, {{~skuGroup}}, {{~category}}
        """,
        limit=limit,
        name="credit_by_entity",
        guaranteed=("entityId",),            # the WHERE clause excludes absence
    )


def credit_instance_total(domain: str, start: str, end: str, limit: int = 10) -> Query:
    """The attributed-coverage denominator: every credit the instance burned in the window.

    Reported, never a gate. Attributed entities are legitimately a subset — Jupyter, AI,
    workflow and publish spend sit outside the cleanup surface — so a hard variance gate
    would fail a run that was working correctly.

    Aliased `total` rather than `credits` because `usage_instance_total` below fills the
    same slot in executions. One reader, one column name, and the unit travels as data.
    """
    return build(
        "credit_usage",
        f"""
        SELECT SUM({{creditsUsed}}) AS total,
               COUNT(DISTINCT {{entityId}}) AS entities,
               COUNT(DISTINCT {{skuId}}) AS skus,
               MIN({{date}}) AS first_date,
               MAX({{date}}) AS last_date
        FROM table
        WHERE {{domain}} = {literal(domain)}
          AND {{date}} >= {literal(start)}
          AND {{date}} <= {literal(end)}
        """,
        limit=limit,
        name="credit_instance_total",
    )


# ---------------------------------------------------------------------------------------
# usage_metrics — the same meter, per instance, without the credit conversion
# ---------------------------------------------------------------------------------------


def usage_by_entity(start: str, end: str, limit: int = 200_000) -> Query:
    """Metered usage per entity x SKU x unit over the window. Granite 90d: 2,278 groups.

    Deliberately parallel to `credit_by_entity`, with three differences that are all the
    absence of the credit conversion rather than a choice:

    * **No `domain` predicate**, because there is no `domain` column. This report is
      single-instance by construction — that is the whole reason a subsidiary has it and
      cannot have `Credit Usage`.
    * **No `category`.** The `manual` split rides in the SKU display name as a `(Manual)`
      suffix and is read off it in `cost.py`.
    * **`metered_days` is load-bearing here, not just evidence.** `Usage Unit` distinguishes
      a FLOW (`Executions` — a daily run count, which sums) from a STOCK (`Rows` — the same
      rows reported every day, which must be averaged), and the mean needs its denominator.
      Verified on granite 2026-08-28: `Credit Usage` charges storage once a month while this
      report carries it daily, and on a shared date the two agree exactly.

    `Date` is a 'YYYY-MM-DD' STRING and compares lexicographically, same as `credit_usage`.
    """
    return build(
        "usage_metrics",
        f"""
        SELECT {{Entity ID}}    AS `Entity ID`,
               {{?Entity Type}},
               {{SKU}}          AS `SKU`,
               {{?Usage Unit}},
               SUM({{Usage Quantity}}) AS quantity,
               COUNT(*)                AS metered_days
        FROM table
        WHERE {{Date}} >= {literal(start)}
          AND {{Date}} <= {literal(end)}
          AND {{Entity ID}} IS NOT NULL
        GROUP BY {{Entity ID}}, {{~Entity Type}}, {{SKU}}, {{~Usage Unit}}
        """,
        limit=limit,
        name="usage_by_entity",
        guaranteed=("Entity ID",),           # the WHERE clause excludes absence
    )


def usage_instance_total(unit: str, start: str, end: str, limit: int = 10) -> Query:
    """The coverage denominator on the executions basis, in ONE unit.

    Filtered to `unit` rather than aggregated across all of them, because `Usage Quantity`
    is not summable across units: granite's 90-day window holds 57,455 Executions alongside
    38.1 BILLION Rows, and one number spanning both would be arithmetic on two different
    things. The credit report's single `creditsUsed` column made that error impossible; this
    one does not, so the query has to.

    The unit string comes from config so an instance can correct a connector's display
    string without a code change — the same reasoning as the saving-term patterns.
    """
    return build(
        "usage_metrics",
        f"""
        SELECT SUM({{Usage Quantity}}) AS total,
               COUNT(DISTINCT {{Entity ID}}) AS entities,
               COUNT(DISTINCT {{SKU}}) AS skus,
               MIN({{Date}}) AS first_date,
               MAX({{Date}}) AS last_date
        FROM table
        WHERE {{Usage Unit}} = {literal(unit)}
          AND {{Date}} >= {literal(start)}
          AND {{Date}} <= {literal(end)}
        """,
        limit=limit,
        name="usage_instance_total",
        guaranteed=("Usage Unit",),          # the WHERE clause excludes absence
    )


# ---------------------------------------------------------------------------------------
# people — owner attribution
# ---------------------------------------------------------------------------------------


def people_grain(limit: int = 20_000) -> Query:
    """One row per user. Granite: 68. Feeds owner attribution and ticket 08's
    owner-departed review stratum; never a condition."""
    return build(
        "people",
        """
        SELECT {User ID}    AS `User ID`,
               {?Display Name},
               {?Email},
               {?Role},
               {?Last Login},
               {?Account Locked},
               {?Department},
               {?Title}
        FROM table
        """,
        limit=limit,
        name="people_grain",
    )


# ---------------------------------------------------------------------------------------
# activity_log — optional historical context, never a scoring input
# ---------------------------------------------------------------------------------------


def dataflow_state_events(limit: int = 100_000) -> Query:
    """Actor and time for observed dataflow enable/disable events.

    The two action values are the ones measured on the reference instance; they are not an
    assertion that Domo's Activity Log vocabulary is exhaustive.  The result stays at event
    grain (including ``Event_ID``) so the loader can detect simultaneous or contradictory
    latest events rather than resolving them through SQL ordering.
    """
    return build(
        "activity_log",
        """
        SELECT {?User_ID},
               {?Name},
               {?Action},
               {?Object_Type},
               {?Object_ID},
               {?Event_Time},
               {?Event_ID}
        FROM table
        WHERE {Object_Type} = 'DATAFLOW_TYPE'
          AND {Action} IN ('DISABLED', 'ENABLED')
          AND {Object_ID} IS NOT NULL
          AND {Event_Time} IS NOT NULL
        GROUP BY {~User_ID},
                 {~Name},
                 {~Action},
                 {~Object_Type},
                 {~Object_ID},
                 {~Event_Time},
                 {~Event_ID}
        """,
        limit=limit,
        name="dataflow_state_events",
    )


# ---------------------------------------------------------------------------------------
# The complete roster, for the drift test.
# ---------------------------------------------------------------------------------------

#: Every query the collector can issue, built with representative arguments. A query that
#: is not reachable from here is invisible to the drift test, which is the same as saying
#: the manifest does not cover it.
def ALL() -> List[Query]:
    return [
        dataflow_grain(),
        dataflow_output_edges(),
        dataflow_input_edges(),
        dataset_grain(),
        card_dataset_linkage(),
        credit_by_entity("example.domo.com", "2026-01-01", "2026-03-31"),
        credit_instance_total("example.domo.com", "2026-01-01", "2026-03-31"),
        usage_by_entity("2026-01-01", "2026-03-31"),
        usage_instance_total("Executions", "2026-01-01", "2026-03-31"),
        people_grain(),
        dataflow_state_events(),
    ]
