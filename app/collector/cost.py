"""Cost. **Metered, not modeled** (ticket 06) — and metered by one of TWO meters (ticket 20).

Ticket 06's premise — "per-dataflow credits exist in no Domo source, so cost is modeled" —
was false. `Credit Usage` meters credits per entity, per day, per SKU, and for an
`executions-dataflow-*` SKU the entity is the **output dataset**, which joins to
`Dataflow Details`.`Output Dataset ID`. Each output of a multi-output dataflow bills
independently at the same execution count, so a partial saving is *exactly measured* rather
than apportioned: disabling one output of a five-output dataflow saves precisely that
output's credits.

Ticket 20 then found that `Credit Usage` is a **customer-level** report, held by the
instance holding the contract, and that a second report — `Usage Metrics` — carries the
same meter per instance, minus the credit conversion. So `basis` does not mean
measured-or-guessed. It means **which meter**:

* `metered-credits` — `Credit Usage` is readable. Figures in credits per month.
* `metered-executions` — it is not, but `Usage Metrics` is. The recurring figure is
  **executions per month**, and it is **never converted** into credits. Credits per
  execution runs 0.2049 to 0.9993 across the portfolio at one price on one contract, and
  granite's own moved 0.212 -> 0.526 in twelve months, so there is no rate to apply and
  no rate worth borrowing from the parent.
* `none` — neither report is readable. Every figure is null and the ranking cascade falls
  through to storage -> age -> id.

**A figure carries its unit, and the units are not interchangeable.** A reader who cannot
tell 510 credits from 510 executions is the failure this module is shaped to prevent.

Consequences that shape this module:

* **The recurring figure is a trailing-90-day total restated per month.** Ninety days
  matches the stale window, so the cost window and the condition window cannot contradict
  each other. Not lifetime, which ranks historical spend rather than recoverable spend.
* **Quarantine stops executions; it does NOT stop storage.** A quarantined dataset's rows
  stay materialized and keep billing, so storage is reported separately as "additional if
  deleted". This corrects an empirical claim inside ADR-0002.
* **Executions are a FLOW and storage is a STOCK, and only the credit conversion hid it.**
  Verified on granite 2026-08-28. `Credit Usage` charges storage once a month (granite:
  1,730 entities on the 3rd, and nothing on any other day) while `Usage Metrics` reports
  the same stock EVERY day — and on a shared date the two agree exactly, 1,730 entities and
  360,618,348 rows on both sides. So summing a window of credits is right and summing a
  window of quantities is not: 90 daily snapshots of ~374M rows sum to a 33-billion
  "row-day" figure that is not rows and is not comparable to anything. On the executions
  basis the storage term is therefore the **mean daily stock, in rows**, not a per-month
  total — a different reduction because it is a differently-shaped series, never a
  conversion.
* **`manual` runs are excluded from the recurring figure.** A hand-triggered run does not
  recur, so counting it inflates what quarantining recovers. Reported separately. On the
  credits basis that is the `category` column; `Usage Metrics` has no `category`, and folds
  it into the SKU name as a `(Manual)` suffix instead.
* **A zero recurring saving is reported AT ZERO, never dropped.** On granite that is the
  largest population in the pillar. Shown zeroes are what make the non-zero figures
  credible, and they must never sort to the top on cost.
* **Credits are a common unit; executions are not.** The portfolio is on one contract at one
  price per credit, so raw credit totals ARE comparable across instances as money. An
  execution count is neither money nor comparable to another instance's. What it does not
  lose is the ORDERING: within an instance, ranking by quantity reproduces the credit
  ranking to 0.1-0.2% discordance (ticket 20, measured on granite and granite-altimate).
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
from typing import Dict, List, Optional, Set

from shared.typed import Result

#: Which meter produced the figures. NOT a measured-or-guessed axis — see the module
#: docstring. `metered` and `estimated` were the pre-ticket-20 values and are gone: the
#: rate card they existed for never produced a figure at all.
BASIS_CREDITS = "metered-credits"
BASIS_EXECUTIONS = "metered-executions"
BASIS_NONE = "none"

#: Units a figure can be in. Carried alongside every figure rather than implied by the
#: field name, because the same field holds credits on one instance and executions on the
#: next, and `rows_under_management` is a stock where the other two are monthly rates.
UNIT_CREDITS = "credits_per_month"
UNIT_EXECUTIONS = "executions_per_month"
UNIT_ROWS = "rows_under_management"

#: (recurring unit, storage unit) per basis. Storage diverges and recurring does not:
#: `Credit Usage` prices a row-month, so storage lands in the same unit as executions,
#: while `Usage Metrics` reports the stock itself.
_UNITS = {
    BASIS_CREDITS: (UNIT_CREDITS, UNIT_CREDITS),
    BASIS_EXECUTIONS: (UNIT_EXECUTIONS, UNIT_ROWS),
    BASIS_NONE: (None, None),
}


class EntityCost:
    """Metered usage for one billed entity over the window, kept split by purpose.

    Every figure is already in the model's basis unit by the time it lands here: the two
    loaders below do the reduction, so nothing downstream has to know which meter it came
    from beyond reading `unit`.
    """

    __slots__ = (
        "entity_id",
        "entity_type",
        "recurring",
        "storage",
        "manual",
        "other",
        "skus",
        "metered_days",
    )

    def __init__(self, entity_id: str, entity_type: Optional[str]) -> None:
        self.entity_id = entity_id
        self.entity_type = entity_type
        self.recurring = 0.0
        self.storage = 0.0
        self.manual = 0.0
        self.other = 0.0
        self.skus: Set[str] = set()
        self.metered_days = 0

    @property
    def total(self) -> float:
        """Only meaningful where every term shares a unit — i.e. on the credits basis.
        `CostModel.attributed` is the one caller and it guards on that."""
        return self.recurring + self.storage + self.manual + self.other


class CostModel:
    """Metered cost for one instance, one window and one meter.

    Construct with `from_config()` and `load_credits()` a `credit_by_entity` result, or
    `load_usage()` a `usage_by_entity` result. `unavailable()` builds the `none` basis.
    """

    def __init__(
        self,
        window_days: int,
        quarantine_patterns: List[str],
        delete_only_patterns: List[str],
        exclude_categories: List[str],
        available: bool = True,
        basis: str = BASIS_CREDITS,
        unavailable_reason: str = "",
        manual_suffix: str = " (Manual)",
    ) -> None:
        self.window_days = window_days
        self.quarantine_patterns = list(quarantine_patterns)
        self.delete_only_patterns = list(delete_only_patterns)
        self.exclude_categories = {c.lower() for c in exclude_categories}
        self.manual_suffix = manual_suffix
        self.available = available
        self.basis = basis
        self.unavailable_reason = unavailable_reason
        self.entities: Dict[str, EntityCost] = {}
        self.instance_total: Optional[float] = None
        self.instance_entities: Optional[int] = None
        self.window_start: Optional[str] = None
        self.window_end: Optional[str] = None
        self.unmatched_skus: Dict[str, float] = {}

    # -- units ----------------------------------------------------------------------------

    @property
    def recurring_unit(self) -> Optional[str]:
        return _UNITS[self.basis][0]

    @property
    def storage_unit(self) -> Optional[str]:
        return _UNITS[self.basis][1]

    @property
    def storage_is_per_month(self) -> bool:
        """True where the storage series is a monthly CHARGE that divides down like any
        other window total; false where it is a daily STOCK already reduced to its mean."""
        return self.storage_unit == UNIT_CREDITS

    @property
    def meter_report(self) -> Optional[str]:
        return {
            BASIS_CREDITS: "credit_usage",
            BASIS_EXECUTIONS: "usage_metrics",
        }.get(self.basis)

    # -- window ---------------------------------------------------------------------------

    @staticmethod
    def window(window_days: int, end: Optional[_dt.date] = None) -> tuple:
        # Ticket 26. UTC, never the operator's local date. `run.py` always passes the
        # run's `today`, so this fallback is unreachable from a run -- but a LOCAL date
        # here would put an operator west of UTC on a different window than the same
        # instance read an hour later, and two instances in two timezones would stop
        # being comparable. The data is UTC; so is every date derived from it.
        end = end or _dt.datetime.now(_dt.timezone.utc).date()
        start = end - _dt.timedelta(days=window_days)
        return start.isoformat(), end.isoformat()

    @property
    def months_in_window(self) -> float:
        return self.window_days / 30.0

    def per_month(self, value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        return round(value / self.months_in_window, 3)

    # -- loading --------------------------------------------------------------------------

    def _classify(self, sku: str) -> str:
        """Which saving a unit of usage belongs to. Patterns come from config, so an
        instance can correct the sku families without a code change — which matters more on
        this axis than most, because the two meters name the same SKU in two different
        spaces (`executions-dataflow-magic-v2` against `Magic ETL v2 DataFlow Execution`)
        and a connector release can rename a display string."""
        for pattern in self.quarantine_patterns:
            if fnmatch.fnmatch(sku, pattern):
                return "quarantine"
        for pattern in self.delete_only_patterns:
            if fnmatch.fnmatch(sku, pattern):
                return "delete_only"
        return "other"

    def _entity(self, entity_id: str, entity_type: Optional[str]) -> EntityCost:
        entity = self.entities.get(entity_id)
        if entity is None:
            entity = EntityCost(entity_id, entity_type)
            self.entities[entity_id] = entity
        return entity

    def load_credits(self, result: Result) -> None:
        """`credit_by_entity` — entity x sku x category, in credits.

        Every term is a window total in one unit, so every term sums. That uniformity is
        the credit conversion doing its job, and it is exactly what `load_usage` cannot
        assume.
        """
        for row in result:
            entity_id = row.get("entityId").as_str()
            sku = row.get("skuId").as_str()
            category = row.get("category").as_str(absent="")
            credits = row.get("credits").as_float(absent=0.0)
            days = row.get("metered_days").as_int(absent=0)

            entity = self._entity(entity_id, row.get("entityType").as_str(absent=None))
            entity.skus.add(sku)
            entity.metered_days = max(entity.metered_days, days)

            bucket = self._classify(sku)
            if category.lower() in self.exclude_categories:
                # A hand-triggered run is real spend and a real operational finding, but it
                # is not spend that quarantining recovers.
                entity.manual += credits
            elif bucket == "quarantine":
                entity.recurring += credits
            elif bucket == "delete_only":
                entity.storage += credits
            else:
                entity.other += credits
                self.unmatched_skus[sku] = self.unmatched_skus.get(sku, 0.0) + credits

    def load_usage(self, result: Result) -> None:
        """`usage_by_entity` — entity x SKU x unit, in whatever that SKU is metered in.

        Two things differ from `load_credits`, and both are the absence of the credit
        conversion rather than a choice:

        * **`category` does not exist.** The `manual` split survives only as a `(Manual)`
          suffix on the SKU display name, so it is read off the name and the name is
          stripped before classification. (`legacy` vs `standard` does NOT survive, and
          nothing here needs it — it moves the credit RATE, and there is no rate.)
        * **Storage is a stock, not a flow.** The execution SKUs are daily run counts and
          sum over the window; `* Rows under Management` reports the same rows every day,
          so it is reduced to its MEAN over the days it was metered. Summing it would
          produce row-days — 90 daily snapshots of ~374M rows adding to 33 billion of
          nothing.
        """
        for row in result:
            entity_id = row.get("Entity ID").as_str()
            sku = row.get("SKU").as_str()
            quantity = row.get("quantity").as_float(absent=0.0)
            days = row.get("metered_days").as_int(absent=0)

            is_manual = sku.endswith(self.manual_suffix)
            classified_as = sku[: -len(self.manual_suffix)] if is_manual else sku

            entity = self._entity(entity_id, row.get("Entity Type").as_str(absent=None))
            entity.skus.add(sku)
            entity.metered_days = max(entity.metered_days, days)

            bucket = self._classify(classified_as)
            if is_manual:
                entity.manual += quantity
            elif bucket == "quarantine":
                entity.recurring += quantity
            elif bucket == "delete_only":
                # The stock's mean over the days it was actually metered — never over the
                # window length, which would read a dataset created halfway through the
                # window as holding half the rows it holds.
                entity.storage += (quantity / days) if days else 0.0
            else:
                entity.other += quantity
                self.unmatched_skus[sku] = self.unmatched_skus.get(sku, 0.0) + quantity

    def load_instance_total(self, result: Result) -> None:
        """The coverage denominator, in the recurring unit.

        On the credits basis that is one row: every credit the instance burned. On the
        executions basis the query filters to the executions unit before aggregating, for
        the same reason `load_usage` does not sum a stock — granite's window holds 57,455
        Executions alongside 38.1 billion Rows, and one number spanning both would be
        arithmetic performed on two different things.
        """
        if not len(result):
            return
        row = result.one()
        self.instance_total = row.get("total").as_float(absent=None)
        self.instance_entities = row.get("entities").as_int(absent=None)
        self.window_start = row.get("first_date").as_str(absent=None)
        self.window_end = row.get("last_date").as_str(absent=None)

    # -- reading --------------------------------------------------------------------------

    def dataset_saving(self, dataset_id: str) -> Optional[float]:
        """Recurring usage per month that quarantining this dataset stops, in
        `recurring_unit`.

        Returns None — never 0.0 — when no meter was readable. A null means "not measured";
        a zero means "measured, and it is zero", and those two must not be shown to a
        reviewer as the same thing.
        """
        if not self.available:
            return None
        entity = self.entities.get(dataset_id)
        return self.per_month(entity.recurring if entity else 0.0)

    def dataset_delete_only(self, dataset_id: str) -> Optional[float]:
        if not self.available:
            return None
        entity = self.entities.get(dataset_id)
        return self._storage_figure(entity.storage if entity else 0.0)

    def dataset_manual(self, dataset_id: str) -> Optional[float]:
        if not self.available:
            return None
        entity = self.entities.get(dataset_id)
        return self.per_month(entity.manual if entity else 0.0)

    def raw_term(self, entity_id: str, term: str) -> Optional[float]:
        """One entity's RAW window total for one term — no per-month restatement, no stock
        reduction.

        Exists so the duplicate-group reduction happens in the same space `terms()` sums in.
        Reducing a group in the *published* space and subtracting it from a raw sum would
        round twice and, on the executions basis, subtract a mean stock from a summed one.
        """
        if not self.available:
            return None
        entity = self.entities.get(entity_id)
        if entity is None:
            return 0.0
        return {"recurring": entity.recurring,
                "storage": entity.storage,
                "manual": entity.manual}[term]

    def _storage_figure(self, value: float) -> float:
        """A monthly charge divides down; a mean stock is already the figure."""
        if self.storage_is_per_month:
            return self.per_month(value)
        return round(value, 3)

    def dataflow_saving(self, output_dataset_ids: Set[str]) -> Optional[float]:
        """A dataflow's recurring saving is the SUM over its outputs, because billing is
        per-output and linear. A dataflow with no outputs bills nothing by this join and
        gets a zero, not a guess — granite's DataFusions are the live case, and they leave
        the population before scoring anyway."""
        if not self.available:
            return None
        total = 0.0
        for dataset_id in output_dataset_ids:
            entity = self.entities.get(dataset_id)
            if entity:
                total += entity.recurring
        return self.per_month(total)

    def dataflow_delete_only(self, output_dataset_ids: Set[str]) -> Optional[float]:
        """Storage summed across outputs. This is a sum of STOCKS on the executions basis,
        which is legitimate — the rows held by five output datasets really are the rows
        held by the dataflow's outputs — where a sum across TIME would not be."""
        if not self.available:
            return None
        total = 0.0
        for dataset_id in output_dataset_ids:
            entity = self.entities.get(dataset_id)
            if entity:
                total += entity.storage
        return self._storage_figure(total)

    def dataflow_manual(self, output_dataset_ids: Set[str]) -> Optional[float]:
        if not self.available:
            return None
        total = 0.0
        for dataset_id in output_dataset_ids:
            entity = self.entities.get(dataset_id)
            if entity:
                total += entity.manual
        return self.per_month(total)

    def basis_detail(self, dataset_ids: Set[str]) -> Optional[str]:
        """Human-readable provenance for one candidate's figure: which meter, which SKUs.

        Names the METER as well as the SKUs, because the same sentence has to be readable
        on an instance the reader has never seen and the unit is the thing most likely to
        be misread.
        """
        if not self.available:
            return self.unavailable_reason or "cost not measured on this instance"
        skus: Set[str] = set()
        days = 0
        for dataset_id in dataset_ids:
            entity = self.entities.get(dataset_id)
            if entity:
                skus |= entity.skus
                days = max(days, entity.metered_days)
        meter = self.meter_report
        if not skus:
            return (
                f"no metered usage in the {self.window_days}-day window "
                f"(enabled but idle, or not billed); meter: {meter}"
            )
        return (
            f"metered from {len(dataset_ids)} entity(ies) over {days} metered day(s) "
            f"via {meter}, reported in {self.recurring_unit}; "
            f"skus: {', '.join(sorted(skus))}"
        )

    # -- integrity ------------------------------------------------------------------------

    def attributed(self, matched: Optional[Set[str]] = None) -> Optional[float]:
        """Usage the collector could attribute to an asset it actually examined.

        `matched` is the set of billed entity ids the pillar recognised — every dataset in
        the population plus every dataflow output. Without it this would sum every unit in
        the window and divide by itself, which is a 100% coverage figure that means nothing.

        **Returns None on the executions basis**, and that is the honest answer rather than
        a gap. Attribution here means "add up everything this entity cost", and on the
        credits basis every term is credits so the sum is a number. On the executions basis
        the terms are executions, rows and tokens — adding them would be the exact category
        error the credit column used to make impossible. Coverage is reported against the
        recurring term alone instead; see `coverage()`.
        """
        if not self.available or self.basis != BASIS_CREDITS:
            return None
        if matched is None:
            return sum(e.total for e in self.entities.values())
        return sum(e.total for entity_id, e in self.entities.items() if entity_id in matched)

    def terms(self, matched: Optional[Set[str]] = None) -> dict:
        """The three saving terms summed over a POPULATION, each billed entity exactly once.

        The summation was never population-specific — only its caller was, and for a long
        time there was only one caller, which is how ticket 34's mistake survived. Two
        populations ask two different questions off the same arithmetic:

        * the entities the run **examined** — "how much of this instance's spend can this
          tool even see", which is coverage, and where examined-not-candidate is exactly
          right (ticket 06);
        * the entities under a **candidate** — "how much would acting on these findings
          recover", which is opportunity.

        On granite the two differ by **6.3x** (9,957.667 against 1,571.660 credits/month),
        so one figure serving both is not an approximation, it is a different measurement.

        Raw window totals, in the meter's own unit. Restating per month and reducing a
        stock belong to the caller, because the two sides label their figures differently.
        """
        recurring = 0.0
        storage = 0.0
        manual = 0.0
        for entity_id, entity in self.entities.items():
            if matched is not None and entity_id not in matched:
                continue
            recurring += entity.recurring
            storage += entity.storage
            manual += entity.manual
        return {"recurring": recurring, "storage": storage, "manual": manual}

    def opportunity(
        self,
        candidate_entities: Set[str],
        suppressed_entities: Optional[Set[str]] = None,
        survivors: Optional[dict] = None,
    ) -> dict:
        """What acting on these findings would recover — the figure the retired name
        `recoverable_by_quarantine` promised and never carried (ticket 34).

        Three reductions, in this order, and each one is load-bearing:

        1. **Per ENTITY, not per candidate.** A candidate id is `asset + condition`, so a
           dataset that is both stale and unconsumed is two rows billing one entity, and a
           dataflow candidate's outputs are billed entities in their own right. On granite
           the naive row sum is 2,922.325 against a true 1,571.660.
        2. **The union of both GRAINS.** A dataset candidate contributes its own id and a
           dataflow candidate its outputs, so overlap collapses. The dataflow grain adds
           only 9.333 credits/month on granite beyond the output grain — but it is not
           redundant: dataflow 577 is `failing` with an output that is not itself a
           candidate, and dropping the grain would lose it silently.
        3. **Each duplicate group counted ONCE**, by subtracting the surviving member —
           `survivors`, from `Duplication.survivors()`. Nothing in the data picks which twin
           survives, so this is a FLOOR, the same floor `duplication.realisable_recurring`
           reports one level down.

        `suppressed_entities` is the entities whose candidates are ALL suppressed by a prior
        verdict. Their subtotal is reported beside the headline rather than folded into it:
        the recommendation is withheld, the spend stays visible (ticket 07 decision 11).
        """
        if not self.available:
            return {
                "recurring": None, "storage": None, "human_triggered": None,
                "entity_once_recurring": None, "group_survivors_recurring": None,
                "suppressed_recurring": None,
                "entities": len(candidate_entities),
                "recurring_unit": self.recurring_unit,
                "storage_unit": self.storage_unit,
            }
        suppressed_entities = suppressed_entities or set()
        actionable = set(candidate_entities) - suppressed_entities
        terms = self.terms(actionable)
        survivors = survivors or {}
        survivor_recurring = survivors.get("recurring") or 0.0
        survivor_storage = survivors.get("storage") or 0.0
        survivor_manual = survivors.get("manual") or 0.0
        return {
            "recurring": self.per_month(terms["recurring"] - survivor_recurring),
            "storage": self._storage_figure(terms["storage"] - survivor_storage),
            "human_triggered": self.per_month(terms["manual"] - survivor_manual),
            "recurring_unit": self.recurring_unit,
            "storage_unit": self.storage_unit,
            "entities": len(actionable),
            # Published so the reduction is auditable rather than asserted: the difference
            # between these two IS the group structure, and a reader who cannot see it
            # cannot check the headline against the candidate rows.
            "entity_once_recurring": self.per_month(terms["recurring"]),
            "group_survivors_recurring": self.per_month(survivor_recurring),
            "suppressed_recurring": self.per_month(
                self.terms(suppressed_entities)["recurring"]
            ),
        }

    def coverage(self, matched: Optional[Set[str]] = None) -> dict:
        """Attributed usage over the instance total for the same window.

        REPORTED, never a gate. Attributed entities are legitimately a subset — Jupyter,
        AI, workflow and publish spend sit outside the cleanup surface — so a hard variance
        gate would fail a run that was working correctly. `min_attributed_coverage_pct` in
        `defaults.yaml` decides only whether the ratio is published prominently.

        Every figure here is on the COVERAGE side of ticket 34's split: the population is
        the billed entities this run examined, candidate or not. That is why the recurring
        term is named `attributed_recurring` and not, as it was until ticket 34,
        `recoverable_by_quarantine` — 84% of granite's entire spend is examined, and almost
        none of it is recoverable. See `opportunity()` for the other question.
        """
        attributed = self.attributed(matched)
        terms = self.terms(matched)
        recoverable = terms["recurring"]
        storage = terms["storage"]
        manual = terms["manual"]

        # The numerator is the RECURRING term on the executions basis, because that is the
        # only term sharing a unit with the denominator. On the credits basis it stays the
        # full attributed total, which is what ticket 06 calibrated against.
        #
        # Note what this line means: on the executions basis the coverage numerator and
        # `attributed_recurring` are the SAME accumulator. That is correct — both are
        # coverage figures — and it is also how the old name got away with being wrong for
        # so long. Nothing here is an opportunity figure; see `opportunity()`.
        numerator = attributed if attributed is not None else (recoverable if self.available else None)
        pct = None
        if self.instance_total and numerator is not None:
            pct = round(100.0 * numerator / self.instance_total, 2)
        unattributed = None
        if self.instance_total is not None and numerator is not None:
            unattributed = round(self.instance_total - numerator, 3)

        # EVERY figure below is restated per month, in the SAME unit as the per-candidate
        # figures — with storage on the executions basis excepted, exactly as it is
        # excepted per candidate, because a stock is not a rate.
        #
        # This closes an inconsistency that predates ticket 29 rather than one it
        # introduced: these aggregates were window totals while `report_as` declared
        # `credits_per_month` and every candidate carried a per-month figure, so the
        # headline and the rows it summarised were in different units with nothing saying
        # so. Harmless while there was one unit to be confused about. Not harmless now.
        return {
            "basis": self.basis,
            "meter": self.meter_report,
            "window_days": self.window_days,
            "recurring_unit": self.recurring_unit,
            "storage_unit": self.storage_unit,
            "attributed": self.per_month(numerator),
            "attributed_is_recurring_only": attributed is None and self.available,
            "instance_total": self.per_month(self.instance_total),
            "unattributed": self.per_month(unattributed),
            "attributed_coverage_pct": pct,
            # The RECURRING TERM over examined entities. Until ticket 34 this shipped as
            # `recoverable_by_quarantine`, which promised a saving and delivered a coverage
            # numerator — on granite 9,957.667 of an 11,823.782 instance total, 84% of
            # everything the instance spends, most of it on healthy consumed assets no
            # reviewer would quarantine. The name is RETIRED rather than reassigned: a
            # reader holding the old key gets nothing rather than a number 6.3x smaller
            # under words they already trust.
            "attributed_recurring": (
                self.per_month(recoverable) if self.available else None
            ),
            "attributed_storage": self._storage_figure(storage) if self.available else None,
            "attributed_human_triggered": self.per_month(manual) if self.available else None,
            "billed_entities": len(self.entities),
            "billed_entities_matched": (
                len([e for e in self.entities if e in matched]) if matched is not None else None
            ),
            "instance_billed_entities": self.instance_entities,
            # The SKUs that matched neither saving term. On granite these are exactly the
            # spend families outside the cleanup surface: publish, AI, workflows, Jupyter,
            # and — on the executions basis only — `Publish Rows under Management`, which
            # `Credit Usage` does not bill at the dataset grain at all.
            # Listed so a reader can see WHY coverage is below 100 rather than suspecting
            # a join failure. Values are NOT summable across SKUs on the executions basis.
            "skus_outside_cleanup_surface": {
                sku: round(value, 3)
                for sku, value in sorted(self.unmatched_skus.items(), key=lambda kv: -kv[1])
            },
        }


def unavailable(window_days: int, reason: str) -> CostModel:
    """The `none` basis — for an instance that can read NEITHER metering report.

    It deliberately produces no per-candidate figure. Before ticket 20 this path existed
    because a rate card could not be applied: the card prices an output-execution, so
    applying it needs a run count **for the window**, and the report roster had none. That
    windowed run count turned out to exist — it is `Usage Metrics`, present on every
    instance in the portfolio — so this path is now genuinely the floor rather than the
    common case, and the rate card is deleted rather than demoted. It was never a
    capability: it predicts the one thing that varies (credits consumed per execution)
    4.9x wrong across the portfolio and 2.5x stale inside granite within a year.

    The honest output is `basis: none`, every figure null, and a named coverage exception.
    It invents nothing, and the ranking cascade falls through to age -> id.
    """
    return CostModel(
        window_days=window_days,
        quarantine_patterns=[],
        delete_only_patterns=[],
        exclude_categories=[],
        available=False,
        basis=BASIS_NONE,
        unavailable_reason=reason,
    )


def from_config(cfg, basis: str, window_days: Optional[int] = None) -> CostModel:
    """Build the model for whichever meter preflight found.

    The two meters name the same SKU in two different spaces, so the pattern sets are
    separate config keys rather than one list doing double duty — `executions-dataflow-*`
    would match nothing in `Usage Metrics`, and silently, producing a run where every
    recurring figure is zero and nothing says why.
    """
    if basis == BASIS_CREDITS:
        terms = cfg.get("cost_model", "saving_terms", default={}) or {}
        exclude = list(cfg.get("cost_model", "exclude_categories", default=[]) or [])
    elif basis == BASIS_EXECUTIONS:
        terms = cfg.get("cost_model", "usage_saving_terms", default={}) or {}
        exclude = []          # no `category` column; the `(Manual)` suffix carries it
    else:
        raise ValueError(f"no meter for basis {basis!r} — use unavailable()")
    return CostModel(
        window_days=window_days or int(cfg.get("cost_model", "window_days", default=90)),
        quarantine_patterns=list(terms.get("quarantine") or []),
        delete_only_patterns=list(terms.get("delete_only") or []),
        exclude_categories=exclude,
        basis=basis,
        manual_suffix=str(cfg.get("cost_model", "usage_manual_suffix", default=" (Manual)")),
    )
