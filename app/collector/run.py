"""`python -m collector.run` — produce a findings file for one instance.

The orchestration is deliberately linear and deliberately boring: preflight gates, the
graph loads, cost loads, the pillar scores, the ledger folds, reconciliation classifies,
ranking happens over unsuppressed candidates only, and the document is validated against
`schema/findings.schema.json` BEFORE it is written. A document that does not validate is
never written at all — an invalid artifact of record is worse than no artifact.

Read-only against Domo throughout (ADR-0002).
"""

from __future__ import annotations

import argparse
import collections
import datetime as _dt
import glob
import json
import logging
import os
import sys
import time
from typing import Dict, List, Optional, Set

from . import __version__ as COLLECTOR_VERSION
from . import config as config_mod
from . import cost as cost_mod
from . import fingerprint as fingerprint_mod
from . import ledger as ledger_mod
from . import appdb as appdb_mod
import uuid
from shared import lineage as lineage_mod
from . import manifest as manifest_mod
from . import queries, reconcile
from shared import consumption as consumption_mod
from shared import publications as publications_mod
from shared import schemas as schemas_mod
from shared import domo as domo_mod
from . import triage as triage_mod
from shared.domo import DomoClient, DomoError, load_token
from .pillars import data as data_pillar

log = logging.getLogger("collector.run")


#: Ticket 71. Timing is measured, printed, and NOT persisted — not in the findings file
#: (ticket 41) and not beside it as a sidecar. The full per-family split exists in memory at
#: run end and needs no new instrumentation: `client.timing` (`RequestTiming`) carries request
#: count and in-flight seconds for all five path families, and `run_timing()` assembles them
#: with the adjudication counts, after which the dict is hung on `document["_timing"]` for the
#: terminal and dropped. Writing it would be one `json.dump`. It is refused for two measured
#: reasons.
#:
#: **1. `runs/` is not a neutral place to put a second file.** Four readers glob
#: `runs_dir/*.json` — `reconcile.find_prior_run` (the second-run diff ADR-0001 exists to
#: protect), `verdict.load_findings`, `annotate.load_findings`, and `review/report.py` and
#: `package/cleanup_web.py` through those — and each picks the document with the newest
#: `run.started_at`. A sidecar keyed to its run must name that run to be honest about what it
#: describes, and the moment it does it is a candidate findings document in all four.
#: Measured, 2026-09-04, on `runs/<run_id>.timing.json` beside a real artifact:
#: `annotate.load_findings` selects the SIDECAR even when both carry the same timestamp (its
#: tie-break is `>=` and `.timing.json` sorts after `.json`), and with a sidecar timestamped
#: one second later — which is what recording its own write time would give, since
#: `run_timing()` runs after the artifact is on disk — `find_prior_run` and
#: `verdict.load_findings` select it too, handing the reconciliation diff a prior run with
#: ZERO candidates. So "outside the reconciliation read" is a property of the filename glob,
#: not of the schema, and the name ticket 71 proposed does not have it. Fixable, but the fix
#: is a guard in every one of those readers plus a test, to defend a property the current
#: design holds for free because nothing is there.
#:
#: **2. It would make a per-call figure re-derivable without making it true.** See
#: `LINEAGE_COST_PER_CONTESTED_DATASET`. The per-call number is a sample from a distribution
#: whose run-to-run spread is larger than the precision the figure claims, and the run that
#: needs it already prints its own block.
TIMING_IS_NOT_PERSISTED = (
    "ticket 71: measured, printed, never written — not in the artifact, not beside it"
)

#: Ticket 71. What the fifteen recorded runs can say about the cost of one contested dataset,
#: derived from `coverage.consumption.adjudication.lineage_calls` and `run.started_at` against
#: each artifact's write time — the two figures the artifact of record actually carries.
#:
#: The contested count has taken exactly two values: 31 (four runs on 2026-09-01, wall
#: 23.8/25.1/37.5/54.1s) and 846 (five runs from `2026-09-01-009` on, wall
#: 161.1/188.3/189.1/204.8/229.4s). Over that 815-call step the marginal cost of one contested
#: dataset is **0.13-0.25 s**, mean-to-mean 0.196 s. That is the honest form of the number: a
#: bound, not a constant.
#:
#: It cannot be sharpened by recording it more precisely, because it is not stable. At a FIXED
#: 846 calls the wall spread is 68.3s — 35% of the mean run, and 2.7x the entire 25.4s that
#: separates the only two per-call figures this repo has ever quoted (ADR-0004's ~0.11 s and
#: ticket 41's 0.14 s, over 846 calls). Ticket 83 (2026-09-08) attributed that instability
#: WITHOUT adding a field: a fixed contested count is not a controlled run, because
#: `coverage.duplication.enumeration.attempts` — already in the artifact — runs 1 to 5 across
#: those runs, and one read costs 15.26s (measured three ways, 12 requests each). Corrected to
#: a one-read basis the 68.3s becomes 22.6s, the 31-call spread becomes 2.8s from 30.3s, and
#: `granite-2026-09-01-006`'s 64.0s with no lineage pass at all becomes 18.3s — faster than
#: every 31-contested run, which is the ordering its work implies. So wall time DOES order
#: these runs by their work, once the enumeration reads the artifact records are counted as
#: work; what it does not do is order them by lineage work alone. Lineage sets a run's level,
#: enumeration re-reads its variance. No single run's per-call figure is still a forecast for
#: the next one, and the bound above stands: 0.13-0.25s raw, 0.15-0.19s on a one-read basis
#: that does not correct for the publish family the later runs carry.
#:
#: This is the argument FOR the decision below rather than against it. The covariate that
#: attributed the spread was deterministic and already in the artifact; it is the SECONDS that
#: two runs over an unchanged instance legitimately disagree on, and they stay on the terminal.
#:
#: Therefore per-call point figures are not the operative number anywhere they are quoted.
#: `docs/prerequisites.md` already ships the right shape — "roughly 0.1-0.2 seconds each",
#: which contains this bound, plus "you do not need to estimate it: the run ends with a timing
#: block" — and ticket 41's 0.14 s survives only as one dated in-flight observation of one
#: run, never as the basis for arithmetic. The arithmetic in `defaults.yaml`'s
#: `adjudication_growth_pct` note is on this bound instead: 5,000 contested datasets is
#: 11-21 minutes, not a confident 12.
LINEAGE_COST_PER_CONTESTED_DATASET = (0.13, 0.25)


def make_run_id(instance_key: str, runs_dir: str, today: _dt.date) -> str:
    """`<instance>-<date>-<nnn>`. The sequence makes three runs in one afternoon three
    distinct artifacts, which is what lets a re-run be free: a cycle is a date, so
    suppression state is identical across all three.

    **MAX + 1, not COUNT + 1**, and it refuses rather than returning a colliding id.

    Counting was wrong the moment today's files stopped being a contiguous 1..n — which
    happens as soon as one is renamed, archived, or written by a run that started on the
    other side of midnight. With 003, 004, 005 and 006 on disk, `len + 1` returns 005 and
    the run **silently overwrites a recorded artifact**. That is exactly what it did on
    granite on 2026-09-01, destroying the findings file the ticket-32 supplementary review
    had been drawn from; the content survived only because run 006 held the same candidate
    population. `runs/*.json` is gitignored, so there was nothing to recover from.

    Same family as `rowCount` and `Execution Count`: a confident, well-formed answer that is
    simply wrong, which no typed reader can catch. The guard is the only thing that can — so
    the id is asserted unused rather than assumed unused.
    """
    prefix = f"{instance_key}-{today.isoformat()}-"
    highest = 0
    for path in glob.glob(os.path.join(runs_dir, f"{prefix}*.json")):
        suffix = os.path.basename(path)[len(prefix):-len(".json")]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    run_id = f"{prefix}{highest + 1:03d}"
    target = os.path.join(runs_dir, f"{run_id}.json")
    if os.path.exists(target):
        # Unreachable by construction, and asserted anyway: a run that overwrites an
        # artifact must fail loudly, never write.
        raise RuntimeError(
            f"run id {run_id} is already on disk at {target} — refusing to overwrite a "
            f"recorded findings file. Move or archive it and re-run."
        )
    return run_id


def _pillar_status(preflight_result, manifest) -> tuple:
    """('ok'|'partial'|'blocked', exception-or-None) for the data pillar.

    A missing FLOOR report blocks. A missing per-condition report degrades to `partial`
    with a named exception — one blocked leg never stops the others.
    """
    floor_failures = [c for c in preflight_result.checks.values() if c.floor and not c.ok]
    if floor_failures:
        check = floor_failures[0]
        return "blocked", {
            "reason": check.reason,
            "detail": check.detail,
            "dataset": check.dataset_id,
        }
    # `cards_and_datasets` degrades on its own — nothing else supplies card linkage.
    degraded = [
        c
        for c in preflight_result.checks.values()
        if not c.floor and not c.ok and c.key == "cards_and_datasets"
    ]
    # The two metering reports degrade TOGETHER, not each on its own. Either one alone
    # produces a full cost figure — that is the whole finding of ticket 20 — so reporting
    # `partial` because granite's customer-level `Credit Usage` is missing from a subsidiary
    # would call the normal subsidiary run degraded. Only losing BOTH loses cost.
    meters = [
        c for c in preflight_result.checks.values() if c.key in ("credit_usage", "usage_metrics")
    ]
    if meters and not any(c.ok for c in meters):
        check = meters[0]
        return "partial", {
            "reason": check.reason,
            "detail": (
                "neither metering report is readable ("
                + "; ".join(f"{c.key}: {c.detail}" for c in meters)
                + "). Candidates are still listed with full evidence; only the cost "
                "ranking degrades to storage -> age -> id."
            ),
            "dataset": check.dataset_id,
        }
    if degraded:
        check = degraded[0]
        return "partial", {
            "reason": check.reason,
            "detail": (
                f"`{check.key}` unavailable: {check.detail}. Conditions consuming it are "
                f"reported per candidate rather than silently omitted."
            ),
            "dataset": check.dataset_id,
        }
    missing_read = [
        (c.key, c.missing_read) for c in preflight_result.checks.values() if c.missing_read
    ]
    if missing_read:
        key, columns = missing_read[0]
        return "partial", {
            "reason": "shape-mismatch",
            "detail": f"`{key}` is missing read columns {columns}",
            "dataset": preflight_result.checks[key].dataset_id,
        }
    return "ok", None


def material_shift_line(cfg) -> dict:
    """The two floors a candidate-population movement must clear to be called material.

    The one place config is read for the line, and it is read for the DOCUMENT: ticket 72
    moved the line into `reconciliation.material_shift`, so `build_document` hands this to
    `reconcile` and the completion summary then reads the line back off the artifact it is
    describing. A summary that says "none" without the line it was measured against is not
    reporting a result (ticket 52) — and a summary that reads that line out of config rather
    than out of the document is describing a run with someone else's threshold, which is the
    defect tickets 51 and 59 named one level down.
    """
    return {
        "min_candidates": int(
            cfg.get("reconciliation", "material_shift", "min_candidates", default=20)
        ),
        "min_pct": float(cfg.get("reconciliation", "material_shift", "min_pct", default=5.0)),
    }


from . import workload as workload_mod


def build_document(
    cfg,
    manifest,
    preflight_result,
    pillar: data_pillar.DataPillar,
    cost_model: cost_mod.CostModel,
    ledger: ledger_mod.Ledger,
    run_id: str,
    started_at: _dt.datetime,
    today: _dt.date,
    prior: Optional[dict],
    prior_path: Optional[str],
    schemas: Optional[schemas_mod.DatasetSchemas] = None,
    enumeration: Optional[dict] = None,
) -> dict:
    cycle_days = int(cfg.get("ledger", "cycle_days", default=30))
    candidates = pillar.score(ledger.retained_unconsumed_ids(today))
    status, exception = _pillar_status(preflight_result, manifest)
    pillar_status = {"data": status}

    scored_ids = {c.candidate_id for c in candidates}
    suppressed_ids = {
        candidate_id
        for candidate_id in scored_ids
        if (ledger.get(candidate_id) or None) is not None
        and ledger.get(candidate_id).suppressed(today)
    }

    # -- input fingerprints (ticket 42) ----------------------------------------------------
    # Captured off the pillar AFTER scoring and BEFORE reconciliation, so the hashes describe
    # exactly the inputs the candidates were decided from, and the change report can say
    # whether any of them moved in the same run a condition's population did. Hashes and a
    # few counts, never rows: the candidate list is already the file's one unbounded artifact.
    input_fingerprints = fingerprint_mod.capture(pillar)
    input_movement = fingerprint_mod.compare(input_fingerprints, prior)

    recon = reconcile.reconcile(
        run_id=run_id,
        scored_ids=scored_ids,
        suppressed_ids=suppressed_ids,
        observed_keys=pillar.observed_keys,
        presence_keys=pillar.presence_keys,
        presence_complete_types=pillar.presence_complete_types,
        pillar_status=pillar_status,
        ledger=ledger,
        today=today,
        prior=prior,
        prior_path=prior_path,
        conditions_unavailable=pillar.conditions_unavailable,
        unevaluable_ids=pillar.unevaluable_candidate_ids,
        input_movement=input_movement,
        material=material_shift_line(cfg),
        versions={"collector_version": COLLECTOR_VERSION, "config_version": cfg.version},
    )

    # -- cost per candidate ---------------------------------------------------------------
    def candidate_cost(candidate) -> dict:
        asset = candidate.asset
        # `duplicated` is the one condition whose saving belongs to a SET rather than to the
        # asset. The per-candidate figure stays true — it is what that entity bills, and
        # hiding it would be its own lie — but a reader adding the rows up would count one
        # opportunity once per member per grain, which on granite is 4.0x over. The flag is
        # carried on EVERY candidate rather than only on the duplicated ones so that
        # "is this figure additive?" is answerable from any row without knowing the rule.
        counted = candidate.condition != data_pillar.CONDITION_DUPLICATED
        if asset.asset_type == data_pillar.ASSET_DATAFLOW:
            recurring = cost_model.dataflow_saving(asset.cost_entity_ids)
            storage = cost_model.dataflow_delete_only(asset.cost_entity_ids)
            manual = cost_model.dataflow_manual(asset.cost_entity_ids)
        else:
            recurring = cost_model.dataset_saving(asset.asset_id)
            storage = cost_model.dataset_delete_only(asset.asset_id)
            manual = cost_model.dataset_manual(asset.asset_id)
        # The units travel WITH the figures, per candidate, rather than only in the header.
        # A candidate record is the unit a reviewer reads, quotes and pastes into a
        # conversation with an owner, and "510" leaving this file without "executions"
        # attached is the misreading the whole basis split exists to prevent.
        return {
            "recurring": recurring,
            "recurring_unit": cost_model.recurring_unit,
            "basis": cost_model.basis,
            "basis_detail": cost_model.basis_detail(asset.cost_entity_ids),
            "additional_if_deleted": storage,
            "additional_if_deleted_unit": cost_model.storage_unit,
            "human_triggered": manual,
            "engine": asset.engine,
            "counted_in_aggregate": counted,
        }

    # -- rank -----------------------------------------------------------------------------
    # Computed over UNSUPPRESSED candidates only, so the actionable top-N has no holes.
    # A null or zero saving sorts to the BOTTOM, never the top, and ties break on
    # candidate_id so two runs over identical data rank identically (ADR-0001).
    #
    # A FOUR-KEY CASCADE, not a composite score (ticket 10). Recurring cost alone ranks 64 of
    # granite's 2,475 candidates and leaves 2,411 tied at zero in no meaningful order, because
    # cost and staleness are near-disjoint by construction: a stale object is not running, and
    # an object that is not running bills nothing. Storage reaches 1,044 more of them, and age
    # orders the rest. A weighted severity score across cost, age and consumers was rejected -
    # it is a guess wearing a number's clothes, and it would not survive the determinism the
    # second-run diff depends on.
    #
    # The cascade is UNCHANGED by which meter ran, and being lexicographic is why: each key is
    # compared only against itself, so key one holding executions rather than credits reorders
    # nothing, and keys one and two never have to share a unit. Ticket 20 measured the two
    # orderings against each other on granite and granite-altimate — identical top-10/25/50,
    # 0.1-0.2% discordant pairs.
    actionable = [c for c in candidates if c.candidate_id not in suppressed_ids]
    costs = {c.candidate_id: candidate_cost(c) for c in candidates}
    guidance = {
        candidate.candidate_id: triage_mod.guidance(
            candidate.condition,
            costs[candidate.candidate_id]["recurring"],
            candidate.candidate_id in suppressed_ids,
        )
        for candidate in candidates
    }

    def rank_key(candidate):
        cost = costs[candidate.candidate_id]
        days = candidate.asset.days_stale
        # An object that has NEVER run has an UNDEFINED age, not an infinite one. It sorts
        # LAST within its cost tier rather than first: treating absent as maximally stale is
        # ADR-0003's mistake run in reverse, and would put granite's 18 never-succeeded
        # netsuite-jdbc datasets at the top of a list ordered by an age they do not have.
        # `False < True`, so aged candidates precede never-run ones. The `or 0` below is only
        # ever compared among peers that are all absent, never against a real age.
        never_run = days is None
        return (
            -(cost["recurring"] or 0.0),
            -(cost["additional_if_deleted"] or 0.0),
            never_run,
            -(days or 0),
            candidate.candidate_id,
        )

    actionable.sort(key=rank_key)
    ranks = {c.candidate_id: index for index, c in enumerate(actionable, start=1)}

    # -- candidate records -----------------------------------------------------------------
    records: List[dict] = []
    bands = cfg.require("triage", "bands")
    for candidate in candidates:
        asset = candidate.asset
        folded = ledger.get(candidate.candidate_id)
        evidence = {
            "run_history": asset.run_history,
            "last_run": (
                asset.last_run.replace(tzinfo=None).isoformat(timespec="seconds") + "Z"
                if asset.last_run
                else None
            ),
            "last_execution_status": asset.last_execution_status,
            "executions_retained": asset.executions_retained,
            "run_state": asset.run_state,
            "enabled": asset.enabled,
            "outputs_declared": asset.outputs_declared,
            "outputs_observed": asset.outputs_observed,
            "days_stale": asset.days_stale,
            "schedule": asset.schedule,
            "source_datasets": asset.source_datasets,
        }
        evidence.update(candidate.evidence_extra)
        if asset.asset_type == data_pillar.ASSET_DATASET:
            evidence["tags"] = asset.tags
            evidence["import_type"] = asset.import_type
            evidence["data_provider"] = asset.data_provider
            evidence["inputs_observed"] = None
            # Ticket 43: the LANDING, beside the ATTEMPT above, on every dataset candidate
            # and on no dataflow — a dataflow's landing is its outputs'. `null` here is
            # readable only with `landing_history` next to it: `never` is the finding
            # (ran, nothing ever landed), `unknown` is the enumeration not having spoken.
            evidence["landing_history"] = asset.landing_history
            evidence["last_data_at"] = (
                asset.last_data_at.astimezone(_dt.timezone.utc)
                .replace(tzinfo=None)
                .isoformat(timespec="seconds")
                + "Z"
                if asset.last_data_at
                else None
            )
        else:
            # Dataflows have no tag field on the product path. Attribute every tag to the
            # output dataset that actually carries it rather than implying the flow is tagged.
            evidence["output_tags"] = asset.output_tags
            evidence["inputs_observed"] = asset.inputs_observed
            # Optional Activity Log context for a current DISABLED state. The field is null
            # for other states and when the report was unavailable; the latter is also named
            # once in coverage.notes. No condition or guidance function reads this value.
            evidence["pause_history"] = asset.pause_history

        evidence["tag_relationships"] = data_pillar.tag_relationships(
            candidate.condition,
            tags=asset.tags,
            output_tags=asset.output_tags,
        )

        stale_window = int(
            cfg.require(
                "conditions",
                "stale",
                "dataflow_days" if asset.asset_type == data_pillar.ASSET_DATAFLOW else "dataset_days",
            )
        )
        record = {
            "candidate_id": candidate.candidate_id,
            "pillar": "data",
            "asset_type": asset.asset_type,
            "asset_id": asset.asset_id,
            "asset_name": asset.name,
            "deep_link": asset.deep_link,
            "owner": {"user_id": asset.owner_id, "name": asset.owner_name},
            "condition": candidate.condition,
            # Ticket 25 decision 11. Presentation is part of the artifact of record so
            # the shipped renderer never recomputes it from a later config. The band
            # reads the condition (especially `unconsumed`) instead of inventing a
            # second consumption test from the evidence counts.
            "band": triage_mod.band_of(
                {"condition": candidate.condition, "evidence": evidence},
                bands,
                stale_window,
            ),
            "evidence": evidence,
            "consumers": {
                "cards": asset.cards,
                "cards_with_no_page_recorded": asset.cards_with_no_page_recorded,
                "pages": asset.pages,
                "downstream_dataflows": asset.downstream_dataflows,
                "distinct_users": None,
            },
            "cost": costs[candidate.candidate_id],
            **guidance[candidate.candidate_id].as_dict(),
            "rank": ranks.get(candidate.candidate_id),
            "ledger": (
                folded.as_dict(today, cycle_days)
                if folded
                else {"suppressed": False}
            ),
            "first_seen_run_id": recon.first_seen.get(candidate.candidate_id, run_id),
        }
        records.append(record)
    records.sort(key=lambda r: r["candidate_id"])

    # -- summary ---------------------------------------------------------------------------
    by_condition = collections.Counter(r["condition"] for r in records)
    by_asset_type = collections.Counter(r["asset_type"] for r in records)
    by_pair = collections.Counter(f"{r['asset_type']}:{r['condition']}" for r in records)
    per_asset = collections.Counter(f"{r['asset_type']}:{r['asset_id']}" for r in records)

    # -- the two cost populations (ticket 34) ---------------------------------------------
    # COVERAGE is over the billed entities the run examined; OPPORTUNITY is over the
    # entities under a candidate. They differ by 6.3x on granite, so they are two figures
    # and not one figure with two readings. The old `suppressed_recoverable` summed per
    # CANDIDATE ID, which triple-counted asset+condition multiplicity, both grains, and
    # duplicate groups — 2,922.325 against a true 1,571.660 had granite carried a full
    # ledger. It moves inside `opportunity` and gains the same reduction.
    candidate_entities, suppressed_entities = data_pillar.DataPillar.candidate_cost_entities(
        candidates, suppressed_ids
    )
    survivors = None
    if pillar.duplication is not None:
        survivors = {
            term: pillar.duplication.survivors(
                lambda dataset_id, term=term: cost_model.raw_term(dataset_id, term)
            )
            for term in ("recurring", "storage", "manual")
        }
    opportunity_block = cost_model.opportunity(
        candidate_entities | suppressed_entities, suppressed_entities, survivors
    )

    coverage_block = cost_model.coverage(pillar.cost_entities)
    window_start, window_end = cost_mod.CostModel.window(cost_model.window_days, today)

    cost_block = {
        "basis": coverage_block["basis"],
        "meter": coverage_block["meter"],
        "unavailable_reason": (
            cost_model.unavailable_reason if not cost_model.available else None
        ),
        "window_days": cost_model.window_days,
        "window": {"start": window_start, "end": window_end},
        "recurring_unit": coverage_block["recurring_unit"],
        "storage_unit": coverage_block["storage_unit"],
        "attributed": coverage_block["attributed"],
        "attributed_is_recurring_only": coverage_block["attributed_is_recurring_only"],
        "instance_total": coverage_block["instance_total"],
        "unattributed": coverage_block["unattributed"],
        "attributed_coverage_pct": coverage_block["attributed_coverage_pct"],
        "attributed_recurring": coverage_block["attributed_recurring"],
        "attributed_storage": coverage_block["attributed_storage"],
        "attributed_human_triggered": coverage_block["attributed_human_triggered"],
        "billed_entities": coverage_block["billed_entities"],
        "billed_entities_matched": coverage_block["billed_entities_matched"],
        "instance_billed_entities": coverage_block["instance_billed_entities"],
        "skus_outside_cleanup_surface": coverage_block["skus_outside_cleanup_surface"],
        "opportunity": opportunity_block,
    }

    asserted = [
        str(e).upper()
        for e in (cfg.get("population", "assert_non_executing_engines", default=[]) or [])
    ]
    excluded_engines = sorted(
        {str(n.get("engine") or "").upper() for n in pillar.non_executing if n.get("engine")}
    )

    # -- duplication ----------------------------------------------------------------------
    if pillar.duplication is None:
        duplication_block = {
            "available": False,
            "why": (
                "no dataset enumeration was available, so `duplicated` did not run at "
                "either grain. Nothing is reported rather than something being reported on "
                "the two axes that remain — the typed column set is not optional."
            ),
        }
    else:
        aggregate = pillar.duplication.aggregate(
            recurring_of_output=cost_model.dataset_saving,
            recurring_of_dataflow=lambda flow_id: cost_model.dataflow_saving(
                pillar.dataflows[flow_id].cost_entity_ids
            ),
        )
        displayed = aggregate["displayed_recurring"]
        realisable = aggregate["realisable_recurring"]
        duplication_block = {
            "available": True,
            "recurring_unit": cost_model.recurring_unit,
            "overstatement_factor": (
                round(displayed / realisable, 2)
                if displayed and realisable
                else None
            ),
            "groups_dataset": aggregate["groups"]["dataset"],
            "groups_dataflow": aggregate["groups"]["dataflow"],
            "candidates_dataset": aggregate["candidates"]["dataset"],
            "candidates_dataflow": aggregate["candidates"]["dataflow"],
            "counted_once_at": aggregate["counted_once_at"],
            "displayed_recurring": displayed,
            "realisable_recurring": realisable,
            # WHICH objects the rule did not settle, not how many — whether it matters
            # depends on which objects they are, and a count cannot say. Split two ways on
            # purpose: `unknown_*` is missing data (no column schema, absent engine) and
            # might be fixable on the instance; `excluded_*` is the rule declining, because a
            # producer with no observed inputs has no job identity to match on.
            "unknown_outputs": pillar.duplication.unknown_outputs,
            "unknown_dataflows": pillar.duplication.unknown_dataflows,
            "excluded_outputs": pillar.duplication.excluded_outputs,
            "excluded_dataflows": pillar.duplication.excluded_dataflows,
            # The enumeration's OWN completeness, carried beside the finding it produced.
            # This surface is non-deterministically incomplete in two forms, and a twin
            # missing from a short read would silently unmake a duplicate group — so the
            # run records what it saw rather than leaving a reader to assume 2,205 of 2,205.
            "enumeration": dict(enumeration or {}),
            "schemas": schemas.stats() if schemas else None,
            "groups": [
                group.as_dict()
                for group in pillar.duplication.output_sets + pillar.duplication.dataflow_sets
            ],
        }

    consumption_block = {
        "signal": pillar.stats()["consumption_signal"],
        "counts": pillar.stats()["consumption"],
        "adjudication": pillar.stats()["adjudication"],
        # Named so the number is auditable: the pre-filter is only defensible while its
        # membership is visible, and "31 contested" with no ids is not a measurement.
        "contested": sorted(pillar.contested_datasets()),
        "unreachable": sorted(pillar.adjudication.unreachable) if pillar.adjudication else [],
        # Ticket 65. The fifth consumer type, with its own signal triad and call count: a
        # reviewer told a candidate was removed because another instance subscribes to it
        # needs to see that the publish API was actually read, and how much it cost.
        "publications": {
            "signal": pillar.stats()["publications_signal"],
            "counts": pillar.stats()["publications"],
            "calls": pillar.publications.calls if pillar.publications else 0,
            "detail": pillar.publications_error,
        },
    }

    document = {
        "run": {
            "run_id": run_id,
            "instance": cfg.instance,
            "domain": cfg.domain,
            "started_at": started_at.replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            # Ticket 26. The run's ONE reference date, in UTC. `started_at` is an instant
            # and moves between two runs on the same day; this does not, and every age,
            # the cost window, `run_id` and every ledger evaluation are measured from it.
            # It is in the document so "what was this age measured against?" is answerable
            # from the artifact rather than inferred from `started_at` by a reader who
            # already knows how ages are derived.
            "as_of_date": today.isoformat(),
            "collector_version": COLLECTOR_VERSION,
            "config_version": cfg.version,
            "source": cfg.source,
            "window": {"start": window_start, "end": window_end},
            "prior_run_id": recon.prior_run_id,
        },
        "coverage": {
            "population": {
                "filter": "run history absent => non-executing object, out of the "
                "dataflow population before any condition runs",
                "excluded_count": len(pillar.non_executing),
                "excluded_engines": excluded_engines,
                "asserted_engines": asserted,
                "assertion_held": all(e in asserted for e in excluded_engines),
                "excluded": pillar.non_executing,
            },
            # The constructed dataflow link (ticket 35, built by ticket 44), as COVERAGE: how
            # many population dataflows received one and WHICH did not, by name and with the
            # guard that withheld it. A `deep_link: null` on a dataflow row is otherwise
            # indistinguishable from the pre-ticket-35 file where every one was null.
            "deep_links": {
                "dataflow_route": data_pillar.DATAFLOW_ROUTE,
                "guards": (
                    "granted only when `Dataflow ID` is decimal AND `DAP Dataflow ID` is "
                    "present; a row failing either keeps deep_link null and is listed below"
                ),
                "dataflows_linked": pillar.deep_links_granted,
                "dataflows_unlinked": pillar.deep_links_withheld,
                "acceptance": (
                    "the route is constructed from the row, not observed on this run; an "
                    "authenticated instance owner opened it on granite and on a second "
                    "instance (ticket 47, 2026-09-03), so a linked row is presented as usable. "
                    "A permission error on click-through is a statement about the viewer's "
                    "Domo access, not about the link."
                ),
            },
            "integrity": {
                "declared_vs_observed_disagreements": len(pillar.count_disagreements),
                "detail": pillar.count_disagreements,
            },
            # `duplicated` is the only condition whose saving belongs to a SET, so its
            # arithmetic has to be stated rather than left for a reader to attempt. Both
            # figures are true: `displayed_recurring` is what adding up the candidate rows
            # gives, `realisable_recurring` is what acting on every group actually recovers.
            # The gap is not an error — it is the group structure, and naming it is the
            # whole reason ticket 21 made the group the unit that owns the saving.
            "duplication": duplication_block,
            # What `unconsumed`'s two NEW inputs actually saw (ticket 31). Carried because
            # the condition's answer now depends on two reads beyond the governance reports,
            # and a reviewer asked to trust that a candidate was REMOVED — or kept despite a
            # consumption signal — needs the counts and the call budget in the artifact
            # rather than in a log line that scrolled past.
            "consumption": consumption_block,
            # What `unconsumed` was decided FROM, as three hashes plus counts (ticket 42).
            # The prior run's block is what the next run's change report compares against;
            # a run written before this existed compares as "prior fingerprints absent",
            # never as "unchanged".
            "input_fingerprints": input_fingerprints,
            # Coverage of the HUMAN JUDGEMENT input, alongside coverage of the instance.
            # A rejected ledger line is a verdict that did not apply: the asset sits in the
            # list as though nobody ever judged it. Hand-editing is the documented fallback,
            # so one wrong letter must produce a line in the artifact rather than silence.
            "ledger": {
                "lines_read": len(ledger.entries),
                "candidates_with_state": len(ledger.state),
                "malformed_lines": len(ledger.malformed),
                "malformed": ledger.malformed,
            },
            "notes": pillar.coverage_notes,
            "reports": [c.as_dict() for c in preflight_result.checks.values()],
            "pillars": [
                {
                    "pillar": "data",
                    "status": status,
                    "exception": exception,
                    "assets_examined": len(pillar.datasets) + len(pillar.dataflows),
                },
                {
                    "pillar": "content",
                    "status": "blocked",
                    "exception": {
                        "reason": "not-requested",
                        "detail": "the MVP is the data pillar; content follows a working spine",
                        "dataset": None,
                    },
                    "assets_examined": None,
                },
                {
                    "pillar": "user",
                    "status": "blocked",
                    "exception": {
                        "reason": "not-requested",
                        "detail": "the MVP is the data pillar; user follows a working spine",
                        "dataset": None,
                    },
                    "assets_examined": None,
                },
            ],
        },
        "cost_model": cost_block,
        "summary": {
            "candidates_total": len(records),
            "candidates_actionable": len(actionable),
            "candidates_suppressed": len(suppressed_ids),
            "by_condition": dict(sorted(by_condition.items())),
            "by_asset_type": dict(sorted(by_asset_type.items())),
            "by_asset_type_condition": dict(sorted(by_pair.items())),
            "assets_with_multiple_conditions": sum(1 for v in per_asset.values() if v > 1),
            "run_history_never": sum(
                1 for r in records if r["evidence"]["run_history"] == "never"
            ),
            # Ticket 54. Counted beside `run_history_never` and never folded into it: the two
            # populations overlap without nesting. On granite 45 datasets never attempted a
            # run AND never landed, 3 attempted and landed nothing, and 18 DATAFLOWS never ran
            # and have no landing to report at all. `unknown` is excluded — a dataset the
            # enumeration did not describe is not a dataset known to be empty.
            "landing_history_never": sum(
                1 for r in records if r["evidence"].get("landing_history") == "never"
            ),
        },
        "candidates": records,
        "reconciliation": recon.as_dict(),
        "watch_list": ledger.watch_list(
            today,
            pillar.observed_keys,
            pillar.presence_keys,
            pillar.presence_complete_types,
        ),
    }
    consumption_block["workload_change"] = workload_mod.compare(
        document, prior, cfg.get("conditions", "unconsumed", "adjudication_growth_pct", default=25)
    )
    return document


def validate(document: dict) -> None:
    import jsonschema

    schema_path = os.path.join(os.path.dirname(__file__), "schema", "findings.schema.json")
    with open(schema_path, "r", encoding="utf-8") as handle:
        schema = json.load(handle)
    jsonschema.Draft202012Validator(schema).validate(document)


def collect(instance_key: str, env_path: Optional[str] = None, prior_override: Optional[str] = None) -> dict:
    started_at = _dt.datetime.now(_dt.timezone.utc)
    today = started_at.date()
    # Wall time for the WHOLE collection, on the same monotonic clock the client attributes
    # its requests with, so the summary's total provably contains every request (ticket 41).
    run_started = time.monotonic()

    cfg = config_mod.load(instance_key)
    manifest = manifest_mod.load()
    client = DomoClient(cfg.domain, load_token(env_path or cfg.env_path), clock=time.monotonic)

    # Preflight is the gate. Imported here rather than at module scope because `shared`
    # imports `collector`, and a top-level import would make the cycle load-order
    # dependent.
    from shared import preflight as preflight_mod

    preflight_result = preflight_mod.preflight(client, cfg, manifest, offer_discovery=False)
    if preflight_result.custody:
        raise SystemExit(
            "credential custody problem:\n  "
            + "\n  ".join(preflight_result.custody)
        )
    if not preflight_result.ok:
        detail = "; ".join(f"{c.key}: {c.detail}" for c in preflight_result.floor_failures)
        raise preflight_mod.PrerequisiteFailure(
            f"floor report failure, the data pillar cannot run — {detail}"
        )

    # -- the graph -------------------------------------------------------------------------
    graph = lineage_mod.Lineage()
    dataflow_details = cfg.report_id("dataflow_details")
    datasets_id = cfg.report_id("datasets")
    graph.load_output_edges(client.execute(dataflow_details, queries.dataflow_output_edges()))
    graph.load_input_edges(client.execute(dataflow_details, queries.dataflow_input_edges()))
    if preflight_result.available("cards_and_datasets"):
        graph.load_card_linkage(
            client.execute(
                cfg.report_id("cards_and_datasets"), queries.card_dataset_linkage()
            )
        )

    # -- cost ------------------------------------------------------------------------------
    # WHICH METER, resolved here from what preflight could actually read — never from the
    # config, which only ships a default for an instance nobody has probed. Credits win where
    # both are present, because credits are money on one contract and roll up across
    # instances where an execution count does not. Granite is the only instance in the
    # portfolio expected to take that branch.
    if preflight_result.available("credit_usage"):
        cost_model = cost_mod.from_config(cfg, cost_mod.BASIS_CREDITS)
        window_start, window_end = cost_mod.CostModel.window(cost_model.window_days, today)
        cost_model.load_credits(
            client.execute(
                cfg.report_id("credit_usage"),
                queries.credit_by_entity(cfg.credit_domain, window_start, window_end),
            )
        )
        cost_model.load_instance_total(
            client.execute(
                cfg.report_id("credit_usage"),
                queries.credit_instance_total(cfg.credit_domain, window_start, window_end),
            )
        )
    elif preflight_result.available("usage_metrics"):
        # The standard path for every instance that is not the contract holder. No domain
        # predicate anywhere below, and deliberately: this report has no `domain` column,
        # which is exactly why a subsidiary has one and cannot have `Credit Usage`.
        cost_model = cost_mod.from_config(cfg, cost_mod.BASIS_EXECUTIONS)
        window_start, window_end = cost_mod.CostModel.window(cost_model.window_days, today)
        cost_model.load_usage(
            client.execute(
                cfg.report_id("usage_metrics"),
                queries.usage_by_entity(window_start, window_end),
            )
        )
        cost_model.load_instance_total(
            client.execute(
                cfg.report_id("usage_metrics"),
                queries.usage_instance_total(
                    str(cfg.get("cost_model", "usage_recurring_unit", default="Executions")),
                    window_start,
                    window_end,
                ),
            )
        )
    else:
        cost_model = cost_mod.unavailable(
            int(cfg.get("cost_model", "window_days", default=90)),
            "",
        )
        cost_model.unavailable_reason = (
            "neither metering report was readable on this instance — not `Credit Usage` "
            "(customer-level, held by the contract holder) and not `Usage Metrics` "
            "(per-instance). No cost figure is invented: there is no rate card, because a "
            "rate predicts credits-consumed-per-execution and that runs 0.2049 to 0.9993 "
            "across the portfolio at one price. Candidates are still listed with full "
            "evidence; only the ranking degrades to storage -> age -> id."
        )

    # -- the pillar ------------------------------------------------------------------------
    pillar = data_pillar.DataPillar(cfg, graph, cost_model, as_of=today)
    if preflight_result.available("people"):
        pillar.load_people(client.execute(cfg.report_id("people"), queries.people_grain()))
    pillar.load_dataflows(client.execute(dataflow_details, queries.dataflow_grain()))
    if preflight_result.available("activity_log"):
        try:
            pause_history = client.execute(
                cfg.report_id("activity_log"), queries.dataflow_state_events()
            )
        except DomoError as exc:
            # Activity Log is evidence-only. It may pass the preflight probe and fail this
            # larger bounded query (including by landing exactly on its LIMIT); that race
            # removes actor/time context, never the current state or the run itself.
            log.warning(
                "Activity Log query failed after preflight; pause actor/time evidence "
                "is unavailable: %s",
                exc,
            )
            pillar.load_pause_history(
                None,
                exception={
                    "reason": "dataset-error",
                    "detail": (
                        "passed, then the production query failed "
                        f"({type(exc).__name__}: {exc})"
                    ),
                },
            )
        else:
            pillar.load_pause_history(pause_history)
    else:
        pillar.load_pause_history(
            None, exception=preflight_result.exception_for("activity_log")
        )
    pillar.load_datasets(client.execute(datasets_id, queries.dataset_grain()))

    # -- typed column sets, for `duplicated` -----------------------------------------------
    # `datasources/search` carries every dataset's schema INLINE, so this is a second reader
    # over a call ticket 15 already pays for: no new query, no new governance report, no new
    # column. It runs AFTER the dataflow load so the population filter has already removed
    # the non-executing objects — a DataFusion has no job to duplicate, and letting one pair
    # with a real dataflow would put it at the top of the cost ranking.
    #
    # A failure here degrades ONE condition, it does not fail the run. `duplicated` is not a
    # floor report and 2,411 of granite's candidates do not depend on it; the honest outcome
    # is a coverage note plus `duplicated` scoring nothing, and reconciliation then reads a
    # prior duplicated candidate as `unverifiable` rather than as resolved.
    #
    # ONE enumeration, FOUR readers: column schemas for `duplicated` (ticket 32), the
    # consumption counts for `unconsumed` (ticket 31), tag/landing evidence (tickets 64/43),
    # and the completeness assertion itself.
    # Ticket 15 forbids the collector reading `discovery.proposed.yaml`, so it makes the call
    # itself — 12 calls and ~18s on granite — and paying it once for three questions is what
    # keeps a run in the shape ticket 04 promised.
    schemas = None
    consumption_counts = None
    try:
        items = client.search_datasets()
        schemas = schemas_mod.DatasetSchemas.from_search(items)
        consumption_counts = consumption_mod.ConsumptionCounts.from_search(items)
    except DomoError as exc:
        log.warning(
            "dataset enumeration failed, `duplicated` will not run and `unconsumed` is off "
            "this run: %s",
            exc,
        )
        pillar.coverage_notes.append(
            {
                "reason": "dataset-error",
                "detail": (
                    f"the dataset enumeration could not be read completely, so `duplicated` "
                    f"did not run at either grain: {exc}"
                ),
            }
        )
    pillar.load_schemas(schemas)
    # Declared even when it is None — that is the difference between "the enumeration was
    # unavailable" and "nobody asked", and only the first may take `unconsumed` off the run.
    pillar.load_consumption(consumption_counts)

    # -- what this instance publishes: the fifth consumer type (ticket 65) -----------------
    # One list, one summaries call, one detail per publication — bounded by the number of
    # publications (Altimate 7, granite 55), never by the population. A dataset in a
    # publication with a live subscriber feeds cards on another instance, and NO other read
    # the collector makes can see that: the enumeration's union fields and product lineage
    # both read 0, so this cannot ride on the adjudication pass. A failure here is a coverage
    # exception that takes `unconsumed` off the run, by `load_consumption`'s precedent — a
    # partial picture of what is published cannot support a single `unconsumed` verdict.
    publications = None
    try:
        publications = publications_mod.Publications.read(client)
        log.info("read %d publication(s) in %d call(s): %s",
                 len(publications.publications), publications.calls, publications.stats())
    except DomoError as exc:
        log.warning(
            "the publish API could not be read, `unconsumed` is off this run: %s", exc
        )
        publications_error = f"{type(exc).__name__}: {exc}"
    else:
        publications_error = None
    pillar.load_publications(publications, error=publications_error)

    # -- the bounded lineage pass, over exactly what the filter flagged --------------------
    # ADR-0004: a per-asset read only over a set an aggregate read has already narrowed. The
    # narrowing read has no false negatives and this pass is never capped, only warned about.
    #
    # Granite, from the recorded runs rather than from a remembered figure: the contested set
    # is a property of the instance on the day and it has moved by 27x. Four runs on
    # 2026-09-01 adjudicated **31** datasets and took 24-54s wall; the five runs from
    # 2026-09-01-009 on, after ticket 22 stopped excluding datasets that feed a dataflow,
    # adjudicate **846** and take 161-229s, the newest (`granite-2026-09-03-001`) 188s. The
    # five runs that predate this pass entirely ran in 5-6s. So the pass is most of the run,
    # and the figure to quote is the timing block the run itself prints, never a constant
    # here. The recorded artifacts carry no per-family timing and ticket 71 decided they never
    # will, so the cost of one contested dataset is a BOUND rather than a constant:
    # **0.13-0.25 s**, from the 31 -> 846 step across those runs
    # (`LINEAGE_COST_PER_CONTESTED_DATASET`). Ticket 41's 0.14 s is one dated in-flight
    # observation of one run (2026-09-02, 117.3s over 846 calls), not a forecast — at a fixed
    # 846 calls these runs differ by 68.3s, 2.7x the gap between the two per-call figures ever
    # quoted here.
    adjudication = None
    if consumption_counts is not None:
        contested = pillar.contested_datasets()
        if contested:
            adjudication = consumption_mod.adjudicate(
                client,
                contested,
            )
            log.info(
                "adjudicated %d contested dataset(s) in %d lineage call(s): %s",
                len(contested),
                adjudication.calls,
                adjudication.stats(),
            )
    pillar.load_adjudication(adjudication)
    pillar.attach_owner_names()

    # -- ledger and prior run ---------------------------------------------------------------
    shared = appdb_mod.open_store(cfg) if appdb_mod.configured(cfg) else None
    base = shared.head() if shared else None
    ledger = shared.ledger(base) if shared else ledger_mod.Ledger(
        cfg.ledger_path,
        cycle_days=int(cfg.get("ledger", "cycle_days", default=30)),
        quarantine_days=int(cfg.get("ledger", "quarantine_days", default=90)),
    ).load()
    if shared and prior_override:
        raise appdb_mod.StorageError("Shared runs use the published AppDB baseline; import historical files through migration.")
    run_id = (f"{instance_key}-{today.isoformat()}-{uuid.uuid4().hex}" if shared
              else make_run_id(instance_key, cfg.runs_dir, today))
    prior = shared.run(head=base) if shared else None
    prior_path = "AppDB" if prior else None
    if not shared:
        if prior_override:
            prior = reconcile.load_prior(prior_override)
            prior_path = prior_override
        else:
            found = reconcile.find_prior_run(cfg.runs_dir, exclude_run_id=run_id)
            if found:
                prior = found["document"]
                prior_path = found["path"]

    document = build_document(
        cfg=cfg,
        manifest=manifest,
        preflight_result=preflight_result,
        pillar=pillar,
        cost_model=cost_model,
        ledger=ledger,
        run_id=run_id,
        started_at=started_at,
        today=today,
        prior=prior,
        prior_path=prior_path,
        schemas=schemas,
        enumeration=client.last_enumeration,
    )
    if prior_override:
        latest = reconcile.find_prior_run(cfg.runs_dir, exclude_run_id=run_id)
        document["coverage"]["consumption"]["workload_change"] = workload_mod.compare(
            document, latest["document"] if latest else None,
            cfg.get("conditions", "unconsumed", "adjudication_growth_pct", default=25),
        )
    movement = document["coverage"]["consumption"]["workload_change"]
    if movement["warned"]:
        log.warning("%s", workload_mod.describe(movement))
    document["_client_stats"] = None
    del document["_client_stats"]

    # Validate BEFORE writing. An invalid artifact of record is worse than none.
    validate(document)

    if shared:
        shared.publish(document, base=base, operation_id=run_id)
        out_path = "AppDB:" + run_id
    else:
        os.makedirs(cfg.runs_dir, exist_ok=True)
        out_path = os.path.join(cfg.runs_dir, f"{run_id}.json")
        # WRITE-ONCE, enforced by the OS. Mode "x" fails if the path exists.
        #
        # A findings file is the artifact of record for one run, and `make_run_id` has always
        # said so — "three runs in one afternoon, three distinct artifacts". Nothing enforced
        # it. `runs/*.json` is gitignored, so an overwrite here is permanent, and the file
        # destroyed may be the one a review drew its gates from. That happened on granite on
        # 2026-09-01 (ticket 34).
        #
        # The guard in `make_run_id` is NOT sufficient and is not the right layer: it protects
        # one caller's choice of name, while this protects the file. Any future output flag, any
        # second caller, any re-derived path bypasses the first and cannot bypass this. The
        # ledger has had the same property since ticket 07 for the same reason — mode "a", so
        # no code path can rewrite recorded judgement.
        try:
            handle = open(out_path, "x", encoding="utf-8")
        except FileExistsError:
            raise RuntimeError(
                f"a findings file already exists at {out_path} — refusing to overwrite the "
                f"artifact of record for run {run_id}. A run adds a file; it never replaces "
                f"one. Move or archive the existing file and re-run."
            ) from None
        with handle:
            json.dump(document, handle, indent=2, ensure_ascii=False)
            handle.write("\n")

    # -- completion summary (ticket 41) ----------------------------------------------------
    # Wall time leads, and the per-family split follows, with the lineage line carrying the
    # adjudication count and its warning threshold. Until this change the line ended in
    # `client.query_seconds` — the query API alone — and the 2026-09-01 dry run printed
    # `4.0s` after roughly two and a half minutes, 846 of its 932 requests being lineage.
    timing = run_timing(
        client,
        wall_seconds=time.monotonic() - run_started,
        adjudication=adjudication,
        workload_change=document["coverage"]["consumption"]["workload_change"],
    )
    log.info(
        "%s: %d candidates (%d actionable, %d suppressed) from %d assets",
        run_id,
        document["summary"]["candidates_total"],
        document["summary"]["candidates_actionable"],
        document["summary"]["candidates_suppressed"],
        document["coverage"]["pillars"][0]["assets_examined"],
    )
    for line in format_timing(timing):
        log.info("%s", line)
    # Beside `_path`: for the terminal, never for the artifact. Timing is the one thing about
    # a run that two runs over an unchanged instance legitimately disagree on, and the
    # findings file is the artifact the second-run diff depends on (ADR-0001). Ticket 71 asked
    # whether it should at least land beside the artifact as `runs/<run_id>.timing.json` and
    # the answer is no, on two measured grounds — see `TIMING_IS_NOT_PERSISTED`. This dict is
    # the whole per-family split; it dies here on purpose.
    document["_timing"] = timing
    document["_path"] = out_path
    return document


def run_timing(client, wall_seconds: float, adjudication, warn_above=None, workload_change=None) -> dict:
    """The run's cost, as the terminal and log report it.

    `wall_seconds` is the whole collection on a monotonic clock; the families are the
    client's per-path attribution (`RequestTiming`, recorded in `_request`). The guard is the
    point: a wall clock SHORTER than the request seconds it is supposed to contain means the
    caller measured the wrong span, and the summary raises rather than print a total that
    omits the lineage pass — which is exactly the number the dry run printed.
    """
    families = client.timing.as_dict()
    in_flight = client.timing.total_seconds
    # A hair of tolerance for float rounding on the same clock; anything more is a real gap.
    if wall_seconds + 1e-6 < in_flight:
        raise ValueError(
            f"wall time {wall_seconds:.3f}s is shorter than the {in_flight:.3f}s the client "
            f"spent in flight ({families[domo_mod.LINEAGE]['seconds']:.3f}s of it on "
            f"product lineage). The total must contain every request; a summary that omits "
            f"the lineage pass is the defect ticket 41 closed."
        )
    enumeration_reads = (client.last_enumeration or {}).get("attempts")
    return {
        "workload_change": workload_change,
        "wall_seconds": wall_seconds,
        "requests_total": client.timing.total_requests,
        "in_flight_seconds": in_flight,
        "query": families[domo_mod.QUERY],
        "enumeration": dict(families[domo_mod.ENUMERATION], reads=enumeration_reads),
        "lineage": families[domo_mod.LINEAGE],
        "publish": families[domo_mod.PUBLISH],
        "other": families[domo_mod.OTHER],
        "adjudication": None
        if adjudication is None
        else {
            "contested": len(adjudication.verdicts),
            "lineage_calls": adjudication.calls,
            "unreachable": len(adjudication.unreachable),
        },
    }


def format_timing(timing: dict) -> List[str]:
    """The timing block, one line per family. Every figure carries its unit and the narrow
    ones carry their scope, because this is the block most likely to be pasted into a
    sentence as 'the run took'."""
    lines = [
        f"timing: {timing['wall_seconds']:.1f}s wall, {timing['requests_total']} HTTP "
        f"request(s), {timing['in_flight_seconds']:.1f}s in flight",
    ]
    q, e, l, o = timing["query"], timing["enumeration"], timing["lineage"], timing["other"]
    lines.append(
        f"    query API (governance SQL): {q['requests']} request(s), {q['seconds']:.1f}s"
    )
    # Ticket 55 (merged into 52): the re-read COUNT alone does not say what a re-read cost,
    # and on granite the enumeration has been 44.3s of a 167.9s run at three reads. The
    # per-read figure is divided out of this run's own two numbers rather than quoted from a
    # past observation, because that is the distinction an administrator needs and cannot
    # make from the total: one slow read is a slow instance, three normal reads are an
    # unstable enumeration, and both land on the same number of seconds.
    #
    # ADR-0003: `reads` is ABSENT when no enumeration ran, and absent is not one read. A
    # per-read cost is never divided by a count that was never taken, and the line says which
    # of the two it is instead of printing a confident figure over a missing denominator.
    e_reads = e.get("reads")
    if e_reads and e_reads > 1:
        detail = (
            f"{e_reads} reads unioned, {e['requests'] / e_reads:.1f} request(s) / "
            f"{e['seconds'] / e_reads:.1f}s per read"
        )
    elif e_reads:
        detail = "1 read, not re-read — the figures above ARE the per-read cost"
    elif e["requests"]:
        detail = "read count not recorded, so no per-read cost"
    else:
        detail = "no enumeration ran"
    lines.append(
        f"    dataset enumeration       : {e['requests']} request(s), {e['seconds']:.1f}s"
        f"  = {detail}"
    )
    adj = timing["adjudication"]
    if adj is None:
        detail = "no adjudication ran"
    else:
        detail = (
            f"{adj['contested']} contested dataset(s) adjudicated"
            + (f", {adj['unreachable']} unreachable" if adj["unreachable"] else "")
        )
    detail += "; " + workload_mod.describe(timing.get("workload_change"))

    lines.append(
        f"    product lineage           : {l['requests']} request(s), {l['seconds']:.1f}s"
        f"  = {detail}"
    )
    pub = timing.get("publish")
    if pub and pub["requests"]:
        lines.append(
            f"    publish API               : {pub['requests']} request(s), {pub['seconds']:.1f}s"
            f"  = what this instance publishes, one detail per publication"
        )
    if o["requests"]:
        lines.append(
            f"    other (schema, whoami)    : {o['requests']} request(s), {o['seconds']:.1f}s"
        )
    return lines


def format_population_shift(document: dict) -> str:
    """The one terminal line for ticket 42's `material-population-shift` note.

    Ticket 42 built the note, put it in `reconciliation.notes`, and left the terminal alone
    on purpose — this region was ticket 41's at the time. The consequence is the thing the
    note exists to prevent: a reviewer meets a candidate population that moved by hundreds,
    the artifact explains it in a file nobody opens to read a change report, and the
    movement gets read as asset change. One line, and the explanation stays in the note —
    which claims no cause, and neither does this.

    **ADR-0003 governs the ways there can be no note, and they are three different things.**
    The test was never computed (a document carrying no `population` block — every granite
    run through `2026-09-02-001` is one), it could not be computed (no prior run on the other
    side), or it ran over every condition and nothing crossed the line. Only the last of
    those may print "none"; the first two are absent, not zero. A condition the run could not
    evaluate is named separately for the same reason — its population drop is the absence of
    a test, never a movement, which is why `reconcile` tables it and never calls it material.

    **It takes ONE argument, and that is the ticket 72 point.** The line used to arrive as a
    second parameter read out of `config`, so this function needed an input to say what the
    document it was handed had just been measured against — and a reader of the stored file
    had no way to find out at all. The basis now travels in the artifact
    (`reconciliation.material_shift`) and is read from there. A document that records no line
    is a FOURTH state and says so: it predates the field, which is not a line of zero.
    """
    recon = document.get("reconciliation") or {}
    population = recon.get("population")
    if not population:
        return (
            "material shift: NOT COMPUTED — this run records no per-condition populations, "
            "so nothing here is a 'none'"
        )

    material = recon.get("material_shift")
    if material:
        line = (
            f" against the line of {material['min_candidates']} candidates "
            f"and {material['min_pct']}%"
        )
    else:
        # ADR-0003 in the terminal: no recorded line is not a line of zero. Every granite run
        # through `granite-2026-09-03-001` lands here, and saying nothing would let a reader
        # take "none" for a result measured against the threshold their config holds today.
        line = (
            " against a line this run does not record — absent, not zero, so what `material` "
            "meant here cannot be recovered from the file"
        )

    untested = sorted(
        condition
        for condition, entry in population.items()
        if entry.get("condition_unavailable")
    )
    untested_text = (
        f"; {len(untested)} condition(s) NOT EVALUATED, so untested rather than unmoved "
        f"({', '.join(untested)})"
        if untested
        else ""
    )

    notes = [
        note
        for note in (recon.get("notes") or [])
        if note.get("name") == reconcile.NOTE_MATERIAL_SHIFT
    ]
    if notes:
        moved = "; ".join(
            f"{note['condition']} {note['prior']} -> {note['current']} ({note['delta']:+d}"
            + (f", {note['delta_pct']}%)" if note.get("delta_pct") is not None else ")")
            for note in notes
        )
        return (
            f"material shift: {moved}{line} — reconciliation.notes says what else moved "
            f"in the same run and claims no cause{untested_text}"
        )

    if all(entry.get("prior") is None for entry in population.values()):
        return (
            "material shift: not testable — no prior run to compare this run's "
            f"{len(population)} condition population(s) against{untested_text}"
        )

    tested = len(population) - len(untested)
    return f"material shift: none over {tested} condition(s){line}{untested_text}"


def print_change_summary(document: dict) -> None:
    """The compact terminal change block, including zero-valued categories.

    Everything printed here comes off the document, the material line included since ticket
    72 — so the block describes the run it was handed and nothing else.
    """
    recon = document["reconciliation"]["counts"]
    counts = ", ".join(f"{key}={recon.get(key, 0)}" for key in reconcile.CATEGORIES)
    print(f"\n  change vs prior: {counts}")
    print(f"  {format_population_shift(document)}")
    if document["reconciliation"]["unmatched_verdicts"]:
        print(f"  unmatched verdicts: {len(document['reconciliation']['unmatched_verdicts'])}")
    rejected = (document.get("coverage", {}).get("ledger", {}).get("malformed") or [])
    if rejected:
        lines = ", ".join(str(entry.get("line")) for entry in rejected)
        print(f"  rejected ledger lines: {len(rejected)} (line(s) {lines})")
    if document["watch_list"]:
        print(f"  watch list: {len(document['watch_list'])} quarantined asset(s)")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m collector.run",
        description="Produce a findings file for one instance. Read-only against Domo.",
    )
    parser.add_argument("--instance", required=True)
    parser.add_argument("--env", default=None)
    parser.add_argument("--prior", default=None, help="Path to the prior findings file")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = config_mod.load(args.instance)
    if not appdb_mod.configured(cfg):
        appdb_mod.ensure_connection(cfg)
    document = collect(args.instance, args.env, args.prior)

    run = document["run"]
    summary = document["summary"]
    cost = document["cost_model"]
    print(f"\n{run['run_id']}  {run['instance']} ({run['domain']})")
    print(f"  window {run['window']['start']} -> {run['window']['end']}   "
          f"prior run: {run['prior_run_id'] or 'none (first run)'}")
    print(f"  coverage: data={document['coverage']['pillars'][0]['status']}  "
          f"assets examined={document['coverage']['pillars'][0]['assets_examined']}  "
          f"excluded by population filter={document['coverage']['population']['excluded_count']}")
    change_first = bool(cfg.get("cadence", "change_first", default=False))
    # Ticket 52: the material line travels with the change block, so "none" is a result read
    # against a stated threshold rather than a bare word. Ticket 72: it travels there IN THE
    # DOCUMENT, so `cfg` is no longer consulted here — the summary quotes the line the run
    # recorded, which is the only line that can be true of it.
    if change_first:
        print_change_summary(document)
    print(f"\n  candidates: {summary['candidates_total']} total, "
          f"{summary['candidates_actionable']} actionable, "
          f"{summary['candidates_suppressed']} suppressed by prior verdict")
    for key, value in summary["by_asset_type_condition"].items():
        print(f"      {key:24s} {value}")
    print(f"      {'(never-run assets)':24s} {summary['run_history_never']}")
    print(f"      {'(never-landed datasets)':24s} {summary['landing_history_never']}")
    print(f"      {'(assets w/ 2+ conditions)':24s} {summary['assets_with_multiple_conditions']}")
    # Every printed figure names its unit. The terminal is where a figure is most likely to
    # be copied into a sentence without its header, and on the executions basis the header is
    # the only thing that says these are not credits.
    recurring_unit = cost["recurring_unit"] or "not measured"
    storage_unit = cost["storage_unit"] or "not measured"
    print(f"\n  cost ({cost['basis']} via {cost['meter'] or 'no meter'}, "
          f"trailing {cost['window_days']}d):")
    # Printed as a PAIR of populations, following the precedent set for `duplication`
    # below. The terminal is where a figure is most likely to be copied into a sentence
    # without its header, and until ticket 34 the figure standing here alone was the
    # examined-entity total wearing the words "recoverable by quarantine" — 84% of
    # granite's whole spend. Whichever line a reader lifts, the population comes with it.
    opp = cost["opportunity"]
    print(f"      OPPORTUNITY (candidates)  : {opp['recurring']} [{recurring_unit}]"
          f"  over {opp['entities']} billed entity(ies), duplicate groups counted once")
    print(f"        + if deleted            : {opp['storage']} [{storage_unit}]")
    print(f"        human-triggered         : {opp['human_triggered']} [{recurring_unit}]")
    if opp.get("group_survivors_recurring"):
        print(f"        before group reduction  : {opp['entity_once_recurring']} "
              f"- {opp['group_survivors_recurring']} retained by surviving twins")
    if opp.get("suppressed_recurring"):
        print(f"        suppressed by verdict   : {opp['suppressed_recurring']} "
              f"[{recurring_unit}] (withheld from the headline, not hidden)")
    print(f"      coverage (examined)       : {cost['attributed_recurring']} "
          f"[{recurring_unit}]  <- what the tool can SEE, not what it can recover")
    print(f"        + storage / human-trig. : {cost['attributed_storage']} "
          f"[{storage_unit}] / {cost['attributed_human_triggered']} [{recurring_unit}]")
    scope = " (recurring term only)" if cost["attributed_is_recurring_only"] else ""
    print(f"      attributed / instance     : {cost['attributed']} / "
          f"{cost['instance_total']} = {cost['attributed_coverage_pct']}%{scope}")
    if cost["basis"] == "metered-executions":
        # Ticket 20 decision 5: say what the missing meter would have added, rather than
        # letting an execution count read as spend.
        print("      NOTE: executions rank this instance's own assets exactly as credits "
              "would,\n            but they are not money and do not compare to another "
              "instance's.")
    dup = document["coverage"]["duplication"]
    if dup.get("available"):
        # Printed as a PAIR, always. The displayed figure is the one a reader would reach by
        # adding the rows, so showing it alone would be the 4x overstatement in print, and
        # showing only the realisable one would contradict every candidate row in the file.
        print(f"\n  duplication ({dup['groups_dataset']} output group(s) / "
              f"{dup['groups_dataflow']} dataflow group(s)):")
        print(f"      candidates                : {dup['candidates_dataset']} dataset + "
              f"{dup['candidates_dataflow']} dataflow")
        print(f"      displayed across those    : {dup['displayed_recurring']} "
              f"[{dup['recurring_unit'] or 'not measured'}]")
        print(f"      realisable (group once)   : {dup['realisable_recurring']} "
              f"[{dup['recurring_unit'] or 'not measured'}]"
              + (f"  = {dup['overstatement_factor']}x over"
                 if dup.get("overstatement_factor") else ""))
        if dup["unknown_outputs"] or dup["unknown_dataflows"]:
            print(f"      unknown (no schema/engine): {len(dup['unknown_outputs'])} output(s), "
                  f"{len(dup['unknown_dataflows'])} dataflow(s) — unknown, NOT clean")
        if dup["excluded_outputs"] or dup["excluded_dataflows"]:
            print(f"      excluded (no inputs, rule): {len(dup['excluded_outputs'])} output(s), "
                  f"{len(dup['excluded_dataflows'])} dataflow(s)")
    else:
        print(f"\n  duplication: DID NOT RUN — {dup.get('why')}")

    if not change_first:
        print_change_summary(document)
    # Ticket 41: wall time first, then the split. The lineage line carries the adjudication
    # count and threshold so the run's dominant cost is in the summary, not only in the
    # coverage block of a file nobody opens to learn why the run took three minutes.
    print()
    for line in format_timing(document["_timing"]):
        print(f"  {line}")
    print(f"\n  written to {document['_path']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
