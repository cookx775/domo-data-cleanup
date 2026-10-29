"""Deterministic presentation fields shared by findings, reports, and review.

Triage changes how a finding is presented; it never changes whether the finding exists.
Keeping the band calculation in the shipped collector matters because the report renderer and
the artifact of record must use one definition. The non-shipping sample-review harness imports
this module rather than maintaining a second ladder.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List


ATTENTION_INDIVIDUAL = "individual"
ATTENTION_GROUPED = "grouped"
ATTENTION_NONE = "none"
ATTENTION_VALUES = (ATTENTION_INDIVIDUAL, ATTENTION_GROUPED, ATTENTION_NONE)

ACTION_REVIEW = "review"
ACTION_QUARANTINE = "quarantine"
ACTION_DECIDE_SURVIVOR = "decide-which-survives"
ACTION_NONE = "none"
ACTION_VALUES = (ACTION_REVIEW, ACTION_QUARANTINE, ACTION_DECIDE_SURVIVOR, ACTION_NONE)

BASIS_SUPPRESSED = "suppressed-by-recorded-decision"
BASIS_DUPLICATED = "duplicate-survivor-needs-judgment"
BASIS_RECURRING_POSITIVE = "positive-recurring-saving"
BASIS_RECURRING_ZERO = "no-recurring-saving"
BASIS_RECURRING_UNKNOWN = "recurring-cost-not-measured"
BASIS_CONDITION_REVIEW = "condition-needs-review"
BASIS_VALUES = (
    BASIS_SUPPRESSED,
    BASIS_DUPLICATED,
    BASIS_RECURRING_POSITIVE,
    BASIS_RECURRING_ZERO,
    BASIS_RECURRING_UNKNOWN,
    BASIS_CONDITION_REVIEW,
)

# One copy source for the stored values, report labels and shipped glossary. The decision
# rule below chooses an action; every human surface gets its meaning from this table.
ACTION_METADATA: Dict[str, dict] = {
    ACTION_REVIEW: {
        "label": "Review first",
        "definition": (
            "Inspect the candidate before choosing remediation. This is the recommendation "
            "when quarantine would recover no measured recurring usage, when recurring cost "
            "was not measured, or when the condition itself needs human judgment."
        ),
    },
    ACTION_QUARANTINE: {
        "label": "Quarantine",
        "definition": (
            "Stop consumption while preserving the definition. Recommended only for an "
            "unconsumed candidate with a positive recurring saving."
        ),
    },
    ACTION_DECIDE_SURVIVOR: {
        "label": "Decide which survives",
        "definition": (
            "Compare the members of a duplicate set and choose which one remains. The tool "
            "identifies the twins but never selects a survivor."
        ),
    },
    ACTION_NONE: {
        "label": "No action",
        "definition": (
            "A recorded verdict or lifecycle state currently covers this candidate. It stays "
            "visible with its cost and can be revisited explicitly."
        ),
    },
}


@dataclass(frozen=True)
class Guidance:
    attention: str
    recommended_action: str
    recommended_action_basis: str

    def as_dict(self) -> dict:
        return {
            "attention": self.attention,
            "recommended_action": self.recommended_action,
            "recommended_action_basis": self.recommended_action_basis,
        }


def guidance(condition: str, recurring, suppressed: bool = False) -> Guidance:
    """Return the one policy's priority and remediation outputs.

    Attention and action overlap because recurring usage matters to both, but they answer
    different questions. A positive-cost failing candidate gets individual attention while
    still requiring review; an unconsumed one can safely be told to quarantine. Exact zero
    is the boundary. ``None`` is unknown and never collapses into measured zero.
    """
    if suppressed:
        return Guidance(ATTENTION_NONE, ACTION_NONE, BASIS_SUPPRESSED)
    if condition == "duplicated":
        return Guidance(ATTENTION_INDIVIDUAL, ACTION_DECIDE_SURVIVOR, BASIS_DUPLICATED)

    positive = recurring is not None and recurring > 0
    attention = ATTENTION_INDIVIDUAL if positive else ATTENTION_GROUPED
    if condition == "unconsumed":
        if positive:
            return Guidance(attention, ACTION_QUARANTINE, BASIS_RECURRING_POSITIVE)
        if recurring is None:
            return Guidance(attention, ACTION_REVIEW, BASIS_RECURRING_UNKNOWN)
        return Guidance(attention, ACTION_REVIEW, BASIS_RECURRING_ZERO)
    return Guidance(attention, ACTION_REVIEW, BASIS_CONDITION_REVIEW)


def guidance_of(candidate: dict, revisit: bool = False) -> Guidance:
    """Read recorded guidance, or derive it for an older findings artifact.

    ``revisit`` deliberately ignores suppression through the same policy so an administrator
    can put a recorded decision back into its original individual/grouped work mode.
    """
    if revisit:
        cost = candidate.get("cost") or {}
        return guidance(candidate.get("condition") or "", cost.get("recurring"), False)
    recorded = (
        candidate.get("attention"),
        candidate.get("recommended_action"),
        candidate.get("recommended_action_basis"),
    )
    if all(recorded):
        return Guidance(*recorded)
    cost = candidate.get("cost") or {}
    suppressed = bool((candidate.get("ledger") or {}).get("suppressed"))
    return guidance(candidate.get("condition") or "", cost.get("recurring"), suppressed)


def _days(candidate: dict):
    return (candidate.get("evidence") or {}).get("days_stale")


def band_of(candidate: dict, bands: List[dict], stale_window_days: int) -> str:
    """Return the config-declared band for one candidate.

    ``fresh-but-unread`` reads the *condition*. It deliberately does not derive consumption
    again from evidence counts: a downstream dataflow may itself be part of a dead chain, and
    ``unconsumed`` has already adjudicated that graph. Re-deriving the word here was how 49
    fresh granite findings fell into ``unbanded`` despite being unconsumed.

    ``never-landed`` is tested FIRST, and that position is the whole point of ticket 54. A
    dataset that has never held a row makes every later band's premise false: there is no
    freshness to read, no age to bucket, and no shape worth comparing. Left later in the
    ladder, ``fresh-but-unread`` claimed granite's
    ``RAW | GTK Opportunity > Quote | Cycle Time`` — a netsuite connector that succeeds on a
    DAY schedule, has never landed a row, and burns 30 credits/month doing it — because that
    band reads the run ATTEMPT (``days_stale``) and the attempt succeeds. Ticket 43 split
    attempt from landing in the evidence line and left the band reading the attempt; this is
    the same defect one surface further out.

    ``landing_history`` is a dataset-grain field, so dataflows carry ``None`` here, match no
    landing band, and reach ``never-run`` as before — a dataflow's landing is its outputs'.
    ``unknown`` matches nothing either: not-knowing is never reported as never-landed.
    """
    days = _days(candidate)
    history = (candidate.get("evidence") or {}).get("run_history")
    landing = (candidate.get("evidence") or {}).get("landing_history")
    condition = candidate.get("condition")

    for band in bands:
        key = band["key"]
        if "condition" in band:
            if condition == band["condition"]:
                return key
            continue
        if "landing_history" in band:
            if landing == band["landing_history"]:
                return key
            continue
        if "run_history" in band:
            expected = band["run_history"]
            if history == expected or (expected == "absent" and history == "never"):
                return key
            continue
        if band.get("refreshed_within_stale_window"):
            if condition == "unconsumed" and days is not None and days < stale_window_days:
                return key
            continue
        if days is None:
            continue
        low, high = band.get("from_days"), band.get("to_days")
        if days >= (low or 0) and (high is None or days < high):
            return key
    return "unbanded"


def asset_band(candidates: Iterable[dict], bands: List[dict]) -> str:
    """Choose one band for an asset carrying several candidate conditions.

    Asset entries appear once in the rendered report. When their conditions produce more than
    one band, the first band in the configured ladder wins, preserving the deliberate priority
    of contradiction bands over age bands. ``unbanded`` is always last.
    """
    order = {band["key"]: index for index, band in enumerate(bands)}
    values = {candidate.get("band") or "unbanded" for candidate in candidates}
    return min(values or {"unbanded"}, key=lambda value: (order.get(value, len(order)), value))
