"""The reader's glossary, rendered at the end of every report.

`CONTEXT.md` (this project's, plus `shared/CONTEXT.md`) is the vocabulary and it never ships: it is written for the people who
build the tool, and it is long. A report reader — an administrator holding an owner supplement —
meets the same words with no definitions beside them, and the project's care over terms loses its
value at exactly the moment it matters (Dalton, 2026-09-03, after the Altimate report was
generated). So the report carries its own glossary, in reader language, one or two sentences per
term.

Two rules keep it honest, both enforced by `tests/test_glossary.py`:

* every entry names the `CONTEXT.md` term it derives from (`source`), and that term must still be
  defined there — this checks vocabulary, not semantic agreement;
* every word the report actually uses as a label — each condition, band, verdict, lifecycle value,
  change category and recommended action — has an entry, so a reader is never sent to look
  something up that is not here.

Order is the order a reader meets the words: what a finding is, the four conditions, the report's
sections, the bands, the decisions, then the figures.
"""

from __future__ import annotations

import json
import os
from typing import List, Tuple

from . import triage as triage_mod
from . import config as config_mod
from . import ledger as ledger_mod
from . import reconcile as reconcile_mod


def _candidate_properties() -> dict:
    """Read the candidate contract shipped with the report."""
    path = os.path.join(os.path.dirname(__file__), "schema", "findings.schema.json")
    with open(path, encoding="utf-8") as handle:
        schema = json.load(handle)
    return schema["properties"]["candidates"]["items"]["properties"]


def _evidence_description(field: str) -> str:
    return _candidate_properties()["evidence"]["properties"][field]["description"]

#: (section, [(term as printed, CONTEXT.md source term, definition)]).
GLOSSARY: List[Tuple[str, List[Tuple[str, str, str]]]] = [
    (
        "What a finding is",
        [
            ("candidate", "Candidate",
             "One asset flagged against one condition. An asset that is both stale and unconsumed is "
             "two candidates, because keeping something that is merely stale is a different question "
             "from keeping something nothing reads. A candidate is a recommendation, never an action."),
            ("finding", "Findings",
             "Anything the report asserts as fact. Computed by versioned code from your instance's own "
             "governance data; two runs over identical data produce identical findings."),
            ("recommend-only", "Recommend-only",
             "The tool reads, scores and recommends. It never changes candidate datasets or dataflows. "
             "It stores findings and attributed decisions in AppDB; people perform asset changes."),
            ("absent", "Absent",
             "A value that does not apply to this object, as distinct from one that applies and is zero. "
             "The report never treats a missing number as 0: a dataflow that has never run has no age, "
             "not an infinite one."),
            ("unknown", "unknown",
             "The tool could not answer the question for this object, and says so rather than guessing. "
             "Unknown is never the same as clean."),
            # Ticket 86 deliberately derives this one entry from the shipped artifact contract.
            # Ticket 68 owns the broader decision about other rule-bearing glossary entries.
            ("run_state", "Findings", _evidence_description("run_state")),
        ],
    ),
    (
        "Conditions",
        [
            ("unconsumed", "Unconsumed",
             "Nothing reads this object today: no card, no downstream dataflow, no view or blend, no "
             "alert, and no subscribing instance. A property of the present, not of history, so an "
             "object that never ran and one that lost its last reader last month are both unconsumed. "
             "Every member of a dead chain of dataflows is independently unconsumed."),
            ("published", "Publication",
             "The dataset is copied into another Domo instance through Domo Publish. A publication with "
             "at least one subscribing instance is a reader, even when nothing on this instance opens "
             "the dataset, so such a dataset is never unconsumed. A publication nobody subscribes to is "
             "noted on the row and is a second thing to retire."),
            ("stale", "Stale",
             "The object exists but has not had a run attempt in the configured window. An "
             "object that has never run has no age and is not stale; it is reported as never-run instead."),
            ("failing", "Failing",
             "The most recent run attempt ended in error. A statement about the last attempt, not about "
             "the object's history."),
            ("duplicated", "Duplicated",
             "Two different dataflows read exactly the same input datasets, run on the same engine, and "
             "produce outputs with identical column sets: one job done twice. Detected from ids and "
             "types alone, never from names. Both members are candidates; the recommendation is "
             "decide which survives, because nothing in the data can pick one."),
            ("duplicate group", "Duplicate group",
             "The set of twins one redundancy makes. Cost is billed per output, so the group's saving is "
             "counted once in every total even though each member shows its own true metered cost."),
            ("never-landed", "Never landed",
             "The dataset has never held a row. Distinct from never-run: a connector that runs on schedule "
             "and returns nothing has run attempts, and successful ones, while never landing data - so a "
             "run history is not a delivery record. It is the first band tested, ahead of every age and of "
             "duplication, because an asset with no data has no freshness, no age and no shape worth "
             "comparing. One word covers both routes in: never attempted a run, or attempted and delivered "
             "nothing. Datasets only - a dataflow's landing is its outputs'."),
            ("never-run", "Non-executing object",
             "An object with no run history at all. It has no age, so it is outside the stale test, but it "
             "can still be unconsumed. Domo DataFusions never execute by design and are removed from the "
             "population before any condition runs. Since never-landed is tested first and covers every "
             "dataset that has never held a row, this band is in practice the DATAFLOW-grain statement: a "
             "dataflow reports no landing of its own, so a dataflow that never executed lands here."),
        ],
    ),
    (
        "How the report is organised",
        [
            ("change since the prior run", "Reconciliation",
             "Every candidate compared with the previous run, current presence sources and the ledger, in "
             "categories that "
             "always print, including the zeros. On a first run everything is new."),
            ("new", "Baseline run",
             "Seen for the first time in this run. On the baseline run that is every candidate: years of "
             "accumulation surfaced at once, which is what the first run is for."),
            ("still open", "Reconciliation",
             "Was a candidate last run and still is."),
            ("returned", "Returned",
             "Was off the list last run because of a recorded decision, and is back because that decision "
             "stopped applying: a keep-for-now lapsed, a quarantine was restored, a verdict was revoked. "
             "The most informative line in any run after the first."),
            ("resolved", "resolved",
             "The asset was positively observed this run and no longer meets the condition. Never inferred "
             "from absence."),
            ("removed", "Removed",
             "The asset is absent from the complete current presence source. This says what the tool "
             "observed; it does not claim the tool witnessed a delete action."),
            ("retired", "Retired",
             "An approved, quarantined or deleted candidate is now observed removed. The tool closes this "
             "loop automatically, so nobody must return merely to mark the action complete."),
            ("retirement register", "Retired",
             "The compact list of completed, ledger-backed removals carried into later runs. A retired "
             "candidate does not become an unmatched warning merely because it remains absent."),
            ("disappeared", "disappeared",
             "The asset is missing from a source that was healthy this run. Reported, not celebrated: a "
             "missing asset is not proof it was cleaned up."),
            ("unverifiable", "unverifiable",
             "The asset is missing, or the condition could not be tested, and the source was degraded. A "
             "governance report that failed to refresh looks exactly like a cleanup that worked."),
            ("suppressed", "Suppression",
             "Computed and shown as normal, but carrying no recommendation and no rank, because a recorded "
             "decision covers it. Suppressed is not hidden: you can always see what was withheld, why, "
             "and what it still costs."),
            ("coverage digest", "Coverage exception",
             "What the run could and could not see: which reports were read, which conditions ran, how many "
             "objects came back unknown. Read this before the findings; it is the list of things the run "
             "declined to conclude."),
            ("individual decisions", "Individual decisions",
             "Candidates that need one-at-a-time attention: every asset with positive recurring usage, "
             "plus every duplicated candidate. This says how to work them; their recommended action says "
             "what the evidence advises doing."),
            ("watch list", "Watch list",
             "Assets already quarantined and waiting out the period before deletion can be considered. "
             "The tool reports elapsed time only; it has no way to know whether anyone complained."),
            ("lifecycle exception", "Lifecycle exception",
             "The ledger and current presence disagree: deleted but still present, retired then present "
             "again, or removed despite a keep/restoration decision. It remains visible until corrected."),
            ("unmatched verdict", "Unmatched verdict",
             "An active ledger record whose candidate identity no findings artifact established. Investigate "
             "an identity change; withdraw it only when the recorded id or condition was never valid."),
            ("decisions already recorded", "Decisions already recorded",
             "Suppressed candidates remain visible here with their decision, reason and cost. No current "
             "action is due, but each decision can be revisited explicitly."),
            ("owner-grouped review", "Owner-grouped review",
             "Everything the individual-decision set leaves, grouped by owner and then by band. These are conversations, not "
             "rows: one owner usually holds most of it, and a bulk decision over a band is a different ask "
             "from a decision about one asset."),
            ("owner supplement", "Owner-grouped review",
             "The same report filtered to one person: their individual decisions, recorded decisions, then grouped review. The "
             "document to hand someone."),
        ],
    ),
    (
        "Bands",
        [
            ("band", "Band",
             "A named group for backlog candidates. The first matching rule in the configured ladder wins. "
             "Age is measured from the last run attempt to the run's date."),
            ("fresh-but-unread", "Fresh but unread",
             "Refreshed inside the stale window and still read by nothing. Someone is actively maintaining "
             "something nobody uses. The most interesting band, and invisible in any age bucket."),
            ("unbanded", "Band",
             "A candidate the band ladder could not place, reported by name rather than forced into a "
             "bucket. Rare; worth a look."),
        ],
    ),
    (
        "Decisions",
        [
            ("decision", "Decision",
             "One candidate, one verdict or lifecycle value, one reason, and the reviewer who made "
             "the call. A backlog choice expands to one ordinary decision per candidate."),
            ("export", "Export",
             "A deterministic, portable HTML copy of the decision surface. It can be read anywhere "
             "but cannot submit decisions."),
            ("import", "Import",
             "Migrating existing local findings and decision history into shared AppDB storage while "
             "preserving the original files."),
            ("dry run", "Dry run",
             "The exact ledger lines a decision would append, shown before confirmation. Previewing "
             "does not change the ledger."),
            ("ledger", "Ledger",
             "Your instance's append-only record of decisions: every verdict and every lifecycle event, "
             "with who, when and why. Nothing is ever edited out; a mistake is corrected by a later line. "
             "It is what makes the second run shorter than the first."),
            ("verdict", "Verdict",
             "A human judgement on whether a candidate should keep being raised. It says nothing about "
             "whether anything was done; that is the lifecycle event."),
            ("keep-permanent", "keep-permanent",
             "A deliberate decision to keep this. Stop asking."),
            ("keep-for-now", "Cycle",
             "Not this quarter. Suppressed for a fixed number of days, then raised again. Measured in "
             "days, not in runs, so running three times in an afternoon does not use it up."),
            ("revoked", "Verdict",
             "An earlier verdict no longer applies. The candidate is scored as if never judged."),
            ("withdrawn", "Withdrawal",
             "A reasoned correction saying a candidate identity was recorded by mistake. It clears both "
             "active axes without deleting any historical ledger line."),
            ("lifecycle event", "Lifecycle state",
             "Where an asset sits in the quarantine-to-delete progression, recorded separately from the "
             "verdict."),
            ("approved", "Lifecycle state",
             "The action is agreed but has not happened. Deliberately does not suppress: the finding stands "
             "until the asset actually changes."),
            ("quarantined", "Quarantine",
             "Consumption stopped, definition kept, nothing lost: unschedule the dataflow, disable the "
             "output, unshare the card. Reversible. Most recoverable cost sits here."),
            ("restored", "Restore",
             "A quarantine undone because someone objected. Recording it matters: an asset restored in "
             "Domo but not here stays hidden from every future run while fully alive."),
            ("deleted", "Delete",
             "Irreversible removal, eligible only after a quarantine period has elapsed. Never the first "
             "recommendation. The tool never asserts that nobody complained; a person does, by recording "
             "this."),
            ("review", "Recommended action", triage_mod.ACTION_METADATA["review"]["definition"]),
            ("quarantine", "Recommended action", triage_mod.ACTION_METADATA["quarantine"]["definition"]),
            ("decide-which-survives", "Recommended action",
             triage_mod.ACTION_METADATA["decide-which-survives"]["definition"]),
            ("none", "Recommended action", triage_mod.ACTION_METADATA["none"]["definition"]),
        ],
    ),
    (
        "Figures",
        [
            ("metered", "Metered usage",
             "Every cost figure is read back from what Domo actually billed your instance, per object, per "
             "day. Nothing is modelled or estimated."),
            ("credits and executions", "The two meters",
             "Two meters for the same usage. Credit Usage is customer-level and held by the instance that "
             "holds the contract; Usage Metrics is per instance and reports executions. Figures are "
             "reported in whichever unit your instance can meter and are never converted between the two. "
             "Executions rank your own assets exactly as credits would, but they are not money and do not "
             "compare to another instance."),
            ("recurring", "Recurring saving",
             "Usage the recommended action would actually stop, per month over the metered window. "
             "Hand-triggered runs are excluded because they do not recur. A zero is shown as zero."),
            ("if deleted", "Flow and stock",
             "Storage that quarantine cannot recover, because a quarantined dataset's rows stay "
             "materialised. Reported as rows under management, or as a monthly storage charge where the "
             "meter carries one."),
            ("coverage", "Coverage against opportunity",
             "How much of your instance's spend the tool could see and attribute, counting every billed "
             "object it examined, candidate or not. It is not a saving."),
            ("opportunity", "Coverage against opportunity",
             "What acting on every candidate would recover: billed objects under at least one candidate, "
             "each counted once, each duplicate group counted once. The honest headline."),
            ("displayed against realisable", "Displayed against realisable",
             "For duplicates, adding up the candidate rows gives the displayed figure; keeping the most "
             "expensive twin of each group and retiring the rest gives the realisable one. The gap is the "
             "group structure, not an error."),
            ("as-of date", "As-of date",
             "The single date every age and window in this run is measured from. Two runs on the same "
             "date over an unchanged instance agree exactly."),
        ],
    ),
]


def sections(cfg=None, document=None):
    """Project authored concepts plus values from the rendering configuration and runtime.

    Configuration is deliberately labelled: old findings do not contain a full settings
    snapshot. Recorded cost windows take precedence over the current configuration.
    This changes reading copies only, never a findings field or scoring rule.
    """
    if cfg is None:
        import yaml
        with open(config_mod.DEFAULTS_PATH, encoding="utf-8") as handle:
            cfg = config_mod.Config(yaml.safe_load(handle), "", "", [])
    bands = cfg.require("triage", "bands")
    categories = [value.replace("_", " ") for value in reconcile_mod.CATEGORIES]
    def values(items):
        return ", ".join(items)
    suffixes = {
        "candidate": " Conditions: " + values(_candidate_properties()["condition"]["enum"]) + ".",
        "stale": (
            " Rendering configuration: datasets "
            f"{cfg.require('conditions', 'stale', 'dataset_days')} days; dataflows "
            f"{cfg.require('conditions', 'stale', 'dataflow_days')} days."
        ),
        "band": " Rendering configuration, in priority order: " + values(b["key"] for b in bands) + ".",
        "change since the prior run": f" Categories ({len(categories)}): {values(categories)}.",
        "verdict": " Allowed values: " + values(ledger_mod.VERDICT_VALUES) + ".",
        "lifecycle event": " Allowed values: " + values(ledger_mod.LIFECYCLE_VALUES) + ".",
        "decision": (" Record corrections: " + values(ledger_mod.RECORD_VALUES) +
                     ". Recommended actions: " + values(triage_mod.ACTION_METADATA) +
                     ". Attention modes: " + values(triage_mod.ATTENTION_VALUES) + "."),
    }
    cost_window = ((document or {}).get("cost_model") or {}).get("window_days")
    if cost_window is not None:
        suffixes["recurring"] = f" Recorded metered window: {cost_window} days."
    else:
        suffixes["recurring"] = (
            f" Rendering configuration: {cfg.require('cost_model', 'window_days')} days; "
            "this report does not supply a recorded metered window."
        )
    age_entries = []
    special = {"condition", "landing_history", "run_history", "refreshed_within_stale_window"}
    for band in bands:
        if any(key in band for key in special):
            if band.get("refreshed_within_stale_window"):
                rule = "unconsumed, with age below the asset's configured stale window"
            elif "condition" in band:
                rule = "condition " + str(band["condition"])
            elif "landing_history" in band:
                rule = "landing history " + str(band["landing_history"])
            else:
                rule = "run history " + str(band["run_history"])
            suffixes[band["key"]] = " Rendering configuration: " + rule + "; earlier band rules take precedence."
            continue
        low, high = band.get("from_days") or 0, band.get("to_days")
        interval = f"at least {low} days" + (f" and less than {high} days" if high is not None else "")
        age_entries.append((band["key"], "Band",
                            f"Rendering configuration: last run attempt {interval} ago, "
                            "unless an earlier band rule matches."))
    result = []
    for heading, entries in GLOSSARY:
        rendered = [(term, source, definition + suffixes.get(term, ""))
                    for term, source, definition in entries]
        if heading == "Bands":
            rendered.extend(age_entries)
        result.append((heading, rendered))
    return result


def terms(cfg=None) -> List[str]:
    return [term for _, entries in sections(cfg) for term, _, _ in entries]


def lines(cfg=None, document=None) -> List[str]:
    """The glossary as Markdown, for the end of every report."""
    out = [
        "## Glossary",
        "",
        "Every term this report uses, in reader language. Configured values below describe "
        "the settings used to render this report, not a historical settings snapshot.",
    ]
    for section, entries in sections(cfg, document):
        out.extend(["", f"### {section}", ""])
        for term, _, definition in entries:
            out.append(f"- **{term}** — {definition}")
    return out
