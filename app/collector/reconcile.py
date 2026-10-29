"""The change report. Ticket 07 decisions 7, 8 and 9.

**The diff reads the prior findings file**, not `first_seen_run_id`. The findings file is
already the artifact of record and is self-contained; `first_seen_run_id` is a
denormalization computed FROM that load. Deriving the diff from it instead cannot
distinguish "resolved" from "never existed", because it only knows about candidates that
are still candidates.

**Absence is never evidence of success.** A candidate that does not come back has distinct
meanings, and current presence is read independently from condition evaluation:

    resolved            asset OBSERVED in this run's population, condition no longer met
    removed             asset absent from the complete current presence source, with no
                        lifecycle record that makes the transition expected
    retired             observed removal agrees with approved/quarantined/deleted intent
    disappeared         asset absent, pillar coverage `ok`. Probably deleted. Reported,
                        not celebrated.
    unverifiable        asset absent, pillar coverage `partial` or `blocked`. Says nothing.
    unmatched-verdict   an active candidate_id in the LEDGER that no findings artifact has
                        established. A typo, or an identity that shifted.

`retired` is an edge in the counts and a level in `retirement_register`. The split is what
lets the confirming run announce the outcome once while every later run remembers completed
work without relabelling it as an unmatched mistake. A reasoned `withdrawn` ledger record is
the separate terminal answer for an identity that was never real.

`resolved` requiring positive observation is the direct answer to ticket 05's hazard: a
dataflow missing from `Dataflow Details` because the Governance report failed to refresh
must never be reported as a resolved candidate.

**And observing the ASSET is not enough: the CONDITION has to have run.** Ticket 32 found
the second half of that rule missing. `resolved` was decided by asset observation alone, so
a run whose card linkage report was unavailable — `unconsumed` scoring nothing — reported
every prior `unconsumed` candidate as resolved, because the assets were all still there. The
same hole would have swallowed `duplicated` whenever the dataset enumeration failed. So a
condition that could not be evaluated at all is passed in as `conditions_unavailable`, and
its prior candidates are `unverifiable`: absence is not evidence of success, and neither is
the absence of a test.

**`returned` is the mirror of `suppressed`, and it is edge-triggered.** It means the
candidate was OFF the actionable list in the prior run and is ON it now because a human
verdict stopped applying — a keep-for-now that ran out, a quarantine restored, a verdict
revoked. It is decided by the PRIOR RECORD's `ledger.suppressed`, not by the ledger's
current state: asking the ledger alone gives a level, and a level would report `returned`
forever after the grant lapsed rather than in the one run where it actually happened.

**A material population shift is named, and the inputs are checked alongside it** (ticket
42). The change categories say WHAT moved; they cannot say whether a condition's population
moved because assets changed, because a report changed content, or because a coverage
source changed shape. So each condition's prior and current count is reported, a movement
past the calibrated `material` line gets a named note, and the note carries how each input
fingerprint (`fingerprint.py`) moved in the same run. The note records COINCIDENCE, never
cause: an input that changed in the same run as the population is worth a reviewer's
attention, and that is all the artifact can prove.

**The line travels IN the document** (ticket 72). `reconciliation.material_shift` records the
two floors this run applied, so a stored findings file answers "against what?" without its
config beside it — the defect tickets 51 and 59 named. It is one block for the whole
comparison rather than a `threshold` on each `population` entry, because `is_material` is one
test and a per-entry copy would claim a measurement on the entries where none was taken: a
condition the run could not evaluate, a baseline run's null `prior`, and the `prior: 0` case
where the relative floor never applies. An absent block means a run that predates the field,
never a line of zero.
"""

from __future__ import annotations

import collections
import datetime as _dt
import glob
import json
import os
from typing import Dict, List, Optional, Set

from . import fingerprint as fingerprint_mod
from .ledger import Ledger, parse_candidate_id

#: The one note this module emits, by name. A count of notes is not a change report.
NOTE_MATERIAL_SHIFT = "material-population-shift"

#: Shipped defaults for the material line, used only when the caller passes none. The
#: calibrated values live in `config/defaults.yaml` under `reconciliation.material_shift`.
DEFAULT_MATERIAL = {"min_candidates": 20, "min_pct": 5.0}


def is_findings_artifact(path: str, document: object) -> bool:
    """Whether a path and JSON document identify one findings artifact.

    `runs/` is a findings read surface, not a general JSON directory. A sibling sidecar can
    carry a `run` timestamp without carrying the candidate population reconciliation reads;
    a human copy can carry the whole shape without being the canonical `<run_id>.json` file.
    """
    if not isinstance(document, dict):
        return False
    run = document.get("run")
    return (
        isinstance(run, dict)
        and isinstance(run.get("run_id"), str)
        and bool(run["run_id"])
        and os.path.basename(path) == f"{run['run_id']}.json"
        and isinstance(run.get("started_at"), str)
        and bool(run["started_at"])
        and isinstance(document.get("candidates"), list)
    )


def material_line(material: Optional[dict] = None) -> dict:
    """The line `is_material` will actually apply, normalised and typed.

    One reader, so the line `reconcile` tests against, the line the note quotes and the line
    `reconciliation.material_shift` records in the document are the same object rather than
    three constructions of it (ticket 72). A caller that passes nothing gets the shipped
    default, and the document then records the default — what matters is that the artifact
    states the line the run used, not where the run got it.
    """
    merged = dict(DEFAULT_MATERIAL, **(material or {}))
    return {
        "min_candidates": int(merged["min_candidates"]),
        "min_pct": float(merged["min_pct"]),
    }


def is_material(prior_count: int, current_count: int, min_candidates: int, min_pct: float) -> bool:
    """Whether a condition's population moved MATERIALLY between two runs.

    Both floors must clear: an absolute one, so a one-off asset change — which moves a
    condition by one candidate per asset, a handful when a dataflow's outputs go dead with
    it — never reaches the warning path; and a relative one, so a small instance is judged
    against its own size rather than granite's. A condition appearing from nothing clears the
    relative floor by definition: 0 -> 64 `duplicated` is material however it happened.
    """
    delta = abs(current_count - prior_count)
    if delta < min_candidates:
        return False
    if prior_count == 0:
        return True
    return 100.0 * delta / prior_count >= min_pct

CATEGORIES = (
    "new",
    "returned",
    "still_open",
    "resolved",
    "removed",
    "retired",
    "disappeared",
    "unverifiable",
    "suppressed",
)


def find_prior_run(runs_dir: str, exclude_run_id: Optional[str] = None) -> Optional[dict]:
    """The prior run is the newest canonical findings artifact by `started_at`.

    Artifact identity is structural; among artifacts, `started_at` orders runs. Filename
    sorting is neither identity nor a timestamp tie-break.
    """
    newest = None
    newest_started = ""
    for path in sorted(glob.glob(os.path.join(runs_dir, "*.json"))):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                document = json.load(handle)
        except (ValueError, OSError):
            continue
        if not is_findings_artifact(path, document):
            continue
        run = document.get("run") or {}
        if exclude_run_id and run.get("run_id") == exclude_run_id:
            continue
        started = run.get("started_at") or ""
        if started > newest_started:
            newest_started = started
            newest = {"path": path, "document": document}
    return newest


def load_prior(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _returned_entry(
    candidate_id: str, prior_entry: Optional[dict], folded, today: _dt.date
) -> dict:
    """One `returned` row, carrying WHY it came back.

    "returned=1" tells an admin something happened without telling them what: a grant they
    gave in March lapsing reads very differently from a quarantine somebody reversed
    yesterday, and the two demand different next moves.
    """
    parsed = parse_candidate_id(candidate_id)
    prior_entry = prior_entry or {}
    reason = None
    if folded is not None:
        if folded.verdict == "keep-for-now" and folded.suppress_until:
            reason = (
                f"keep-for-now granted {(folded.verdict_at or '')[:10] or 'unknown'} by "
                f"{folded.verdict_by or 'unknown'} lapsed {folded.suppress_until}"
            )
        elif folded.lifecycle == "restored":
            reason = (
                f"quarantine restored {(folded.lifecycle_at or '')[:10]} by "
                f"{folded.lifecycle_by or 'unknown'}"
            )
        elif folded.verdict == "revoked":
            reason = (
                f"verdict revoked {(folded.verdict_at or '')[:10]} by "
                f"{folded.verdict_by or 'unknown'}"
            )
    return {
        "candidate_id": candidate_id,
        "asset_type": prior_entry.get("asset_type") or (parsed[0] if parsed else None),
        "asset_id": prior_entry.get("asset_id") or (parsed[1] if parsed else None),
        "asset_name": (
            prior_entry.get("asset_name")
            or (folded.asset_name_at_verdict if folded else None)
            or (parsed[1] if parsed else candidate_id)
        ),
        "condition": prior_entry.get("condition") or (parsed[2] if parsed else "unconsumed"),
        "first_seen_run_id": prior_entry.get("first_seen_run_id"),
        "returned_from": reason,
    }


class Reconciliation:
    def __init__(self, prior_run_id: Optional[str], prior_path: Optional[str]) -> None:
        self.prior_run_id = prior_run_id
        self.prior_path = prior_path
        self.counts: Dict[str, int] = {name: 0 for name in CATEGORIES}
        self.resolved: List[dict] = []
        self.removed: List[dict] = []
        self.retired: List[dict] = []
        self.disappeared: List[dict] = []
        self.unverifiable: List[dict] = []
        self.unmatched_verdicts: List[dict] = []
        self.returned: List[dict] = []
        #: Persistent, ledger-backed levels. ``retired`` above is the transition in this
        #: run; this register is why successful cleanup does not become an unmatched verdict
        #: one run later or disappear from the audit trail entirely.
        self.retirement_register: List[dict] = []
        self.lifecycle_exceptions: List[dict] = []
        #: Identity memory for active ledger records whose asset currently exists but no
        #: longer meets the recorded condition. Without this small index, a later removal
        #: loses the proof that the candidate id was once real and becomes "unmatched".
        self.established_ledger_ids: List[str] = []
        self.category_of: Dict[str, str] = {}
        self.first_seen: Dict[str, str] = {}
        #: Per condition: prior count, current count, delta, and whether it is material.
        self.population: Dict[str, dict] = {}
        #: The two floors `material` above was decided by, recorded in the document because a
        #: stored `material: false` is meaningless without the line it was measured against
        #: (ticket 72). Set from `reconcile`'s `material` argument through `material_line`;
        #: the shipped default stands for a directly constructed result, so the block is
        #: never absent from a document this code writes.
        self.material_shift: Dict[str, float] = material_line(None)
        #: Named notes. Today only `material-population-shift`; never merely counted.
        self.notes: List[dict] = []

    def as_dict(self) -> dict:
        out = {
            "counts": dict(self.counts),
            "resolved": self.resolved,
            "removed": self.removed,
            "retired": self.retired,
            "disappeared": self.disappeared,
            "unverifiable": self.unverifiable,
            "unmatched_verdicts": self.unmatched_verdicts,
            # NAMED, not merely counted. Of the four categories whose members stay in the
            # candidate list, three are recoverable from the file itself — `new` from
            # `first_seen_run_id`, `suppressed` from `ledger.suppressed`, `still_open` from
            # neither holding. `returned` is recoverable from nothing, and it is the one an
            # admin most needs by name: a reprieve someone granted has run out.
            "returned": self.returned,
            "retirement_register": self.retirement_register,
            "lifecycle_exceptions": self.lifecycle_exceptions,
            "established_ledger_ids": self.established_ledger_ids,
            # BEFORE the table it governs: a reader meets the line, then the figures it
            # decided. Ticket 72.
            "material_shift": dict(self.material_shift),
            "population": self.population,
            "notes": self.notes,
        }
        return out


def _shift_note(
    condition: str,
    prior_run_id: Optional[str],
    prior_count: int,
    current_count: int,
    material: dict,
    input_movement: Optional[dict],
    versions: dict,
    prior_versions: dict,
) -> dict:
    """One `material-population-shift` note. The wording is the deliverable: it must give a
    reviewer a basis to tell an asset change from a report or coverage change WITHOUT
    claiming a cause the artifact cannot prove."""
    delta = current_count - prior_count
    pct = round(100.0 * abs(delta) / prior_count, 1) if prior_count else None
    pct_text = f", {pct}%" if pct is not None else ", from nothing"
    text = (
        f"`{condition}` moved {delta:+d} ({prior_count} -> {current_count}{pct_text}) against "
        f"{prior_run_id}, past the material line of {material['min_candidates']} candidates "
        f"and {material['min_pct']}% of the prior count."
    )

    prior_fingerprints = (input_movement or {}).get("prior_fingerprints")
    inputs = dict((input_movement or {}).get("inputs") or {})
    moved = {
        name: movement
        for name, movement in inputs.items()
        if movement
        in (
            fingerprint_mod.CHANGED,
            fingerprint_mod.BECAME_UNAVAILABLE,
            fingerprint_mod.BECAME_AVAILABLE,
        )
    }
    version_text = (
        f"collector_version {prior_versions.get('collector_version')} -> "
        f"{versions.get('collector_version')}, config_version "
        f"{prior_versions.get('config_version')} -> {versions.get('config_version')}"
    )

    if input_movement is None:
        text += (
            " No input fingerprints were captured this run, so nothing can be said about "
            "whether the inputs moved."
        )
    elif prior_fingerprints != fingerprint_mod.PRIOR_PRESENT:
        # The 008 -> 009 case exactly: the prior artifact predates fingerprints. Nothing on
        # the other side of the comparison means NO result, and "unchanged" is a result.
        text += (
            " The prior run recorded no input fingerprints, so whether the dataflow-edge "
            "graph, the card-linkage aggregate or the enumeration consumption signal moved "
            "between the two runs cannot be established from the artifacts: absent is not "
            "unchanged."
        )
    elif moved:
        text += (
            " In the same run "
            + "; ".join(f"the `{name}` fingerprint {movement}" for name, movement in sorted(moved.items()))
            + ". Coincidence in one run is all this note establishes; it does not show that "
            "the input movement caused the candidate movement."
        )
    else:
        text += (
            " Every recorded input fingerprint is unchanged, so the movement is not "
            "explained by a change in the dataflow-edge graph, the card-linkage aggregate or "
            "the enumeration consumption signal as recorded. An instance change, or a "
            f"scoring change the version stamps may not show ({version_text}), both remain "
            "possible; this note does not choose between them."
        )
    text += (
        " Explanatory only: no candidate is suppressed, altered or re-scored by this note. "
        "Verify a sample of the moved candidates on the product path before treating them "
        "as asset changes."
    )
    return {
        "name": NOTE_MATERIAL_SHIFT,
        "condition": condition,
        "prior_run_id": prior_run_id,
        "prior": prior_count,
        "current": current_count,
        "delta": delta,
        "delta_pct": pct,
        # The same object `reconciliation.material_shift` records, copied onto the note so a
        # note lifted out of the artifact and quoted on its own still carries its basis.
        "threshold": dict(material),
        "prior_fingerprints": prior_fingerprints,
        "inputs": inputs or None,
        "versions": {
            "collector_version": {
                "prior": prior_versions.get("collector_version"),
                "current": versions.get("collector_version"),
            },
            "config_version": {
                "prior": prior_versions.get("config_version"),
                "current": versions.get("config_version"),
            },
        },
        # Ticket 42 decision 5. A review frame is bound to the candidate set it was drawn
        # from (`review.candidate_set_hash`); the moved candidates were not in that set, so
        # its gates say nothing about them. Not an automatic re-review: a supplementary
        # stratum over the moved candidates, in the shape ticket 32's `s1-supplementary`
        # pass used, is what is owed.
        "review_frame": (
            "a sample-review frame drawn before this run does not cover the moved "
            "candidates; a supplementary stratum over them is owed before that frame's "
            "gates are quoted against this run"
        ),
        "detail": text,
    }


def reconcile(
    run_id: str,
    scored_ids: Set[str],
    suppressed_ids: Set[str],
    observed_keys: Set[str],
    pillar_status: Dict[str, str],
    ledger: Ledger,
    today: _dt.date,
    prior: Optional[dict] = None,
    prior_path: Optional[str] = None,
    candidate_index: Optional[Dict[str, dict]] = None,
    presence_keys: Optional[Set[str]] = None,
    presence_complete_types: Optional[Set[str]] = None,
    conditions_unavailable: Optional[Set[str]] = None,
    unevaluable_ids: Optional[Set[str]] = None,
    input_movement: Optional[dict] = None,
    material: Optional[dict] = None,
    versions: Optional[dict] = None,
) -> Reconciliation:
    """Classify every candidate this run scored and every candidate the prior run scored.

    `observed_keys` is the full scoring grain. `presence_keys` is deliberately separate:
    datasets come from the complete live inventory while the scoring grain is a scheduled
    DomoStats snapshot. Without the split, an object removed between refreshes is still
    "observed" and is misreported as resolved. `presence_complete_types` says which asset
    types had an authoritative current inventory; absence from an incomplete inventory is
    never removal evidence.

    `input_movement` is `fingerprint.compare(...)`'s result for this run against the prior;
    `material` the `{min_candidates, min_pct}` line from config, which is normalised through
    `material_line` and written back out as `reconciliation.material_shift` so the stored
    table carries its own basis (ticket 72); `versions` this run's `collector_version` and
    `config_version`. All three feed only the population table and its note (ticket 42) and
    change no category.
    """
    prior_run = (prior or {}).get("run") or {}
    result = Reconciliation(prior_run.get("run_id"), prior_path)
    conditions_unavailable = set(conditions_unavailable or ())
    unevaluable_ids = set(unevaluable_ids or ())
    presence_keys = set(presence_keys or ())
    presence_complete_types = set(presence_complete_types or ())
    # Normalised once and then RECORDED: `population[*].material` is unreadable in a stored
    # artifact without the line it was decided by, so the line goes in the document beside
    # the table rather than staying in the caller's config (ticket 72).
    material = material_line(material)
    result.material_shift = dict(material)
    versions = dict(versions or {})

    prior_candidates: Dict[str, dict] = {}
    for candidate in (prior or {}).get("candidates") or []:
        candidate_id = candidate.get("candidate_id")
        if candidate_id:
            prior_candidates[candidate_id] = candidate

    prior_ids = set(prior_candidates)
    prior_reconciliation = (prior or {}).get("reconciliation") or {}
    prior_retired = {
        entry.get("candidate_id"): entry
        for entry in prior_reconciliation.get("retirement_register") or []
        if entry.get("candidate_id")
    }
    prior_exceptions = {
        entry.get("candidate_id"): entry
        for entry in prior_reconciliation.get("lifecycle_exceptions") or []
        if entry.get("candidate_id")
    }
    prior_established = set(prior_reconciliation.get("established_ledger_ids") or [])

    def _presence(asset_key: Optional[str]) -> str:
        if not asset_key:
            return "unknown"
        asset_type = asset_key.partition(":")[0]
        if asset_key in presence_keys:
            return "present"
        if asset_type in presence_complete_types:
            return "removed"
        return "unknown"

    def _ledger_expectation(folded) -> Optional[str]:
        if folded is None or folded.withdrawn:
            return None
        if folded.lifecycle in ("approved", "quarantined", "deleted"):
            return "removal"
        if folded.lifecycle == "restored":
            return "presence"
        if folded.verdict == "keep-permanent" or (
            folded.verdict == "keep-for-now" and not folded.keep_for_now_expired(today)
        ):
            return "presence"
        return None

    def _retirement_entry(candidate_id: str, source: Optional[dict], folded) -> dict:
        parsed = parse_candidate_id(candidate_id)
        source = source or prior_retired.get(candidate_id) or prior_exceptions.get(candidate_id) or {}
        lifecycle = folded.lifecycle if folded else source.get("recorded_lifecycle")
        return {
            "candidate_id": candidate_id,
            "asset_type": source.get("asset_type") or (parsed[0] if parsed else None),
            "asset_id": source.get("asset_id") or (parsed[1] if parsed else None),
            "asset_name": source.get("asset_name")
            or (folded.asset_name_at_verdict if folded else None)
            or (parsed[1] if parsed else candidate_id),
            "condition": source.get("condition") or (parsed[2] if parsed else "unconsumed"),
            "state_first_seen_run_id": source.get("state_first_seen_run_id") or run_id,
            "recorded_lifecycle": lifecycle,
        }

    retirement_register: Dict[str, dict] = {}
    lifecycle_exceptions: Dict[str, dict] = {}

    # A suppressed candidate STAYS IN THE FINDINGS FILE (ticket 07) — an admin must be able
    # to see what was hidden. So `prior_ids` alone cannot tell whether the prior run put a
    # candidate in front of anyone; only the prior record's own `ledger.suppressed` can.
    prior_suppressed = {
        candidate_id
        for candidate_id, record in prior_candidates.items()
        if (record.get("ledger") or {}).get("suppressed")
    }

    # -- this run's candidates ------------------------------------------------------------
    for candidate_id in sorted(scored_ids):
        prior_entry = prior_candidates.get(candidate_id)
        result.first_seen[candidate_id] = (
            (prior_entry or {}).get("first_seen_run_id") or run_id
            if prior_entry
            else run_id
        )
        if candidate_id in suppressed_ids:
            category = "suppressed"
        elif candidate_id in prior_suppressed:
            # OFF the actionable list last run, ON it now, and a human verdict is why it was
            # off. This is the single most informative line in any run after the first — a
            # reprieve that ran out, a quarantine reversed, a verdict revoked — and it MUST
            # be tested before `prior_ids`. Testing `prior_ids` first hid every one of them
            # behind `still_open`, because a suppressed candidate is still written to the
            # file it is diffed against. Ticket 11 caught it on live data; the fixture that
            # was supposed to cover it passed a prior run with NO candidates at all, a shape
            # the second run of a real instance can never have.
            category = "returned"
        elif candidate_id in prior_ids:
            category = "still_open"
        else:
            folded = ledger.get(candidate_id)
            # Absent from the prior run entirely, but carrying an expired grant: it left the
            # list on its own and has come back. Also a return, by the same reading.
            category = "returned" if (folded and folded.returned(today)) else "new"
        result.category_of[candidate_id] = category
        result.counts[category] += 1
        if category == "returned":
            result.returned.append(
                _returned_entry(candidate_id, prior_entry, ledger.get(candidate_id), today)
            )

    # -- prior candidates that did not come back ------------------------------------------
    for candidate_id in sorted(prior_ids - scored_ids):
        prior_entry = prior_candidates[candidate_id]
        parsed = parse_candidate_id(candidate_id)
        asset_key = f"{parsed[0]}:{parsed[1]}" if parsed else None
        entry = {
            "candidate_id": candidate_id,
            "asset_type": prior_entry.get("asset_type"),
            "asset_id": prior_entry.get("asset_id"),
            "asset_name": prior_entry.get("asset_name") or (parsed[1] if parsed else candidate_id),
            "condition": prior_entry.get("condition") or (parsed[2] if parsed else "unconsumed"),
            "first_seen_run_id": prior_entry.get("first_seen_run_id"),
            "last_seen_run_id": result.prior_run_id,
        }
        pillar = prior_entry.get("pillar") or "data"
        status = pillar_status.get(pillar, "blocked")
        condition = entry["condition"]

        presence = _presence(asset_key)
        folded = ledger.get(candidate_id)
        expectation = _ledger_expectation(folded)

        if condition in conditions_unavailable or candidate_id in unevaluable_ids:
            # The asset may well be sitting right there in the population — but the test
            # that would have flagged it did not run, so its absence from the candidate list
            # is a fact about this run and not about the asset. Checked BEFORE observation,
            # because observation is exactly what would otherwise make it read as resolved.
            scope = "condition" if condition in conditions_unavailable else "candidate"
            entry["coverage_reason"] = (
                f"{scope} `{condition if scope == 'condition' else candidate_id}` could not "
                "be evaluated in this run, so the candidate's absence says nothing about "
                "whether the condition still holds"
            )
            result.unverifiable.append(entry)
            result.counts["unverifiable"] += 1
            result.category_of[candidate_id] = "unverifiable"
        elif presence == "removed":
            if expectation == "removal":
                retired = _retirement_entry(candidate_id, prior_entry, folded)
                result.retired.append(retired)
                retirement_register[candidate_id] = retired
                result.counts["retired"] += 1
                result.category_of[candidate_id] = "retired"
            else:
                if expectation == "presence":
                    removal_reason = "removed-despite-keep-or-restoration"
                    lifecycle_exceptions[candidate_id] = {
                        **_retirement_entry(candidate_id, prior_entry, folded),
                        "kind": removal_reason,
                        "detail": (
                            "the asset was removed even though the active ledger state says "
                            "it should remain present"
                        ),
                    }
                else:
                    removal_reason = "removed-without-recorded-decision"
                entry["removal_reason"] = removal_reason
                result.removed.append(entry)
                result.counts["removed"] += 1
                result.category_of[candidate_id] = "removed"
        elif asset_key and asset_key in observed_keys:
            # Positively observed and no longer meeting the condition. The ONLY good news.
            result.resolved.append(entry)
            result.counts["resolved"] += 1
            result.category_of[candidate_id] = "resolved"
        elif status == "ok":
            result.disappeared.append(entry)
            result.counts["disappeared"] += 1
            result.category_of[candidate_id] = "disappeared"
        else:
            entry["coverage_reason"] = (
                f"pillar `{pillar}` coverage was `{status}`, so the asset's absence from "
                f"this run says nothing about whether it still exists"
            )
            result.unverifiable.append(entry)
            result.counts["unverifiable"] += 1
            result.category_of[candidate_id] = "unverifiable"

    # -- persistent terminal state and lifecycle contradictions ----------------------------
    # A current/prior candidate or an existing terminal/exception record proves the ledger
    # id once named a real candidate. A ledger line alone does not: that is the typo case
    # unmatched-verdict exists to surface.
    anchored_ids = (
        prior_ids
        | set(scored_ids)
        | set(prior_retired)
        | set(prior_exceptions)
        | prior_established
    )
    for candidate_id in sorted(anchored_ids):
        folded = ledger.get(candidate_id)
        if folded is None or folded.withdrawn:
            continue
        parsed = parse_candidate_id(candidate_id)
        if not parsed:
            continue
        asset_key = f"{parsed[0]}:{parsed[1]}"
        presence = _presence(asset_key)
        expectation = _ledger_expectation(folded)
        source = prior_candidates.get(candidate_id) or prior_retired.get(candidate_id) or prior_exceptions.get(candidate_id)
        if presence == "removed" and expectation == "removal":
            retirement_register.setdefault(
                candidate_id, _retirement_entry(candidate_id, source, folded)
            )
        elif presence == "removed" and expectation == "presence":
            lifecycle_exceptions[candidate_id] = {
                **_retirement_entry(candidate_id, source, folded),
                "kind": "removed-despite-keep-or-restoration",
                "detail": (
                    "the asset was removed even though the active ledger state says it "
                    "should remain present"
                ),
            }
            retirement_register.pop(candidate_id, None)
        elif presence == "present" and folded.lifecycle == "deleted":
            lifecycle_exceptions[candidate_id] = {
                **_retirement_entry(candidate_id, source, folded),
                "kind": "deletion-not-observed",
                "detail": "the ledger says deleted, but the asset is still present",
            }
            retirement_register.pop(candidate_id, None)
        elif presence == "present" and candidate_id in prior_retired:
            lifecycle_exceptions[candidate_id] = {
                **_retirement_entry(candidate_id, source, folded),
                "kind": "retired-asset-present",
                "detail": "an asset previously confirmed retired is present again",
            }
            retirement_register.pop(candidate_id, None)
        elif candidate_id in prior_retired:
            # An incomplete presence source cannot disprove a terminal observation already
            # made. Carry the register, but do not claim a fresh verification.
            retirement_register[candidate_id] = dict(prior_retired[candidate_id])

    result.retirement_register = [retirement_register[cid] for cid in sorted(retirement_register)]
    result.lifecycle_exceptions = [
        lifecycle_exceptions[cid] for cid in sorted(lifecycle_exceptions)
    ]
    result.established_ledger_ids = [
        candidate_id
        for candidate_id in sorted(anchored_ids)
        if ledger.has_active_state(candidate_id)
    ]

    # -- ledger entries naming an asset this run never saw ---------------------------------
    for candidate_id, folded in sorted(ledger.state.items()):
        if folded.withdrawn or candidate_id in anchored_ids:
            continue
        parsed = parse_candidate_id(candidate_id)
        if not parsed:
            # Malformed ids never reach here — the ledger collects them separately — but a
            # belt-and-braces skip keeps this loop from inventing an entry.
            continue
        asset_key = f"{parsed[0]}:{parsed[1]}"
        if asset_key in observed_keys:
            # The asset exists and simply does not meet the condition any more. Not an
            # unmatched verdict, and not `resolved` either: the prior run never flagged it,
            # so there is nothing to resolve.
            continue
        axis = "verdict" if folded.verdict else "lifecycle"
        result.unmatched_verdicts.append(
            {
                "candidate_id": candidate_id,
                "axis": axis,
                "value": folded.verdict if axis == "verdict" else folded.lifecycle,
                "at": folded.verdict_at if axis == "verdict" else folded.lifecycle_at,
                "by": folded.verdict_by if axis == "verdict" else folded.lifecycle_by,
                # Recorded in the ledger precisely so this case produces a message a human
                # can act on rather than a bare UUID.
                "asset_name_at_verdict": folded.asset_name_at_verdict,
            }
        )

    # -- the population, per condition, and whether it moved materially -------------------
    # Over EVERY candidate this run scored and every candidate the prior run scored,
    # suppressed included: a verdict changes what is recommended, not what the condition's
    # population is. A condition that could not be evaluated this run is reported with its
    # counts but is never material — its prior candidates are already `unverifiable` above,
    # and calling that drop a population movement would be the absence-of-a-test mistake in
    # a second place.
    def _condition_of(candidate_id: str, record: Optional[dict] = None) -> str:
        parsed = parse_candidate_id(candidate_id)
        return (record or {}).get("condition") or (parsed[2] if parsed else "unconsumed")

    current_by = collections.Counter(_condition_of(c) for c in scored_ids)
    prior_by = collections.Counter(
        _condition_of(c, record) for c, record in prior_candidates.items()
    )
    has_prior = prior is not None
    for condition in sorted(set(current_by) | set(prior_by)):
        prior_count = prior_by.get(condition, 0)
        current_count = current_by.get(condition, 0)
        unavailable = condition in conditions_unavailable
        delta = current_count - prior_count
        entry = {
            "prior": prior_count if has_prior else None,
            "current": current_count,
            "delta": delta if has_prior else None,
            "delta_pct": (
                round(100.0 * abs(delta) / prior_count, 1)
                if has_prior and prior_count
                else None
            ),
            "condition_unavailable": unavailable,
            "material": False,
        }
        if (
            has_prior
            and not unavailable
            and is_material(
                prior_count, current_count, int(material["min_candidates"]), float(material["min_pct"])
            )
        ):
            entry["material"] = True
            result.notes.append(
                _shift_note(
                    condition,
                    result.prior_run_id,
                    prior_count,
                    current_count,
                    material,
                    input_movement,
                    versions,
                    {
                        "collector_version": prior_run.get("collector_version"),
                        "config_version": prior_run.get("config_version"),
                    },
                )
            )
        result.population[condition] = entry

    return result
