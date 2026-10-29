"""Deterministic collector for Domo instance cleanup.

Two runs over identical data produce an identical findings document apart from run
metadata. Agent annotations live in a separate file keyed by `candidate_id` and never
appear in the findings. See docs/adr/0001.

`__version__` is the scoring contract (ticket 51, enforced by ticket 59). It is the one source
of truth: the repo's `pyproject.toml` must carry the same string, and the handoff package
carries it plus a `+build.YYYYMMDD` local segment that is provenance, never a substitute for
the bump. A SCORING CHANGE — anything that can alter the semantic findings document for
identical accepted inputs and configuration: candidate membership or condition, computed
evidence or cost, coverage, reconciliation, ranking — takes the next PATCH version, named in
its ticket as prior -> next. Refactors, tests, docs and package-only changes do not move it;
configuration-only changes move `config_version` instead. Minor and major are reserved for a
separately declared scope or compatibility change.

    0.1.0  runs granite-2026-08-28-001 .. granite-2026-09-02-002. The 008 -> 009 shift
           (+194 `unconsumed`) moved under this stamp, which is the defect the rule exists
           to prevent; those artifacts are not rewritten.
    0.1.1  the baseline the contract is enforced from (ticket 59). No scoring change of its
           own; it is the first stamp that means "scoring behaviour has not moved since".
    0.1.2  dataset tag evidence and calibrated agreement/contradiction annotations (ticket 64).
    0.1.3  publication to another instance is a consumer (ticket 65). A dataset in a
           publication with a live subscriber is no longer `unconsumed`; on Altimate that
           removes 24 of 61 head rows and 51% of the opportunity figure. Publication with no
           subscriber is evidence on the row. `coverage.consumption.publications` and the
           `publications` input fingerprint are new.
    0.1.4  current live presence is separated from the scheduled scoring grain. Confirmed
           removal is `removed` without a recorded decision and terminal `retired` with an
           approved/quarantined/deleted lifecycle; candidate-level unknown is never resolved.
           Lifecycle contradictions and reasoned identity withdrawals are explicit.
    0.1.5  `never-landed` is the first candidate band whenever dataset landing history is
           explicitly `never`, ahead of attempt age, run history and duplication (ticket 54).
    0.1.6  one evidence policy records attention, recommended action and its basis. Only an
           unconsumed candidate with positive recurring usage recommends quarantine; measured
           zero and unmeasured cost recommend Review first. Suppressed candidates recommend no
           action and appear in Decisions already recorded (ticket 67).
    0.1.7  the card placement floor counts cards with no POSITIVE `Page ID`, so an
           all-sentinel card joins the absent ones (ticket 58, deciding ticket 48). Evidence
           only — no candidate, condition, cost or ranking moves — but
           `consumers.cards_with_no_page_recorded` is computed evidence and it changes for
           identical inputs, which is a scoring change by the rule above. On granite the
           dataset-facing floor goes 2,034 -> 2,409 cards and the datasets with every card
           in the floor 160 -> 277.
    0.1.8  `reconciliation.material_shift` records the two floors the run measured every
           `population[*].material` against, so a stored findings file no longer quotes a
           verdict whose basis lives only in config (ticket 72, the species tickets 51 and 59
           named). One block for the whole comparison, not a `threshold` per population
           entry. No candidate, condition, cost, ranking or `material` verdict moves — the
           same runs score identically — but the document gains a required key for identical
           inputs, which is a scoring change by the rule above. `format_population_shift`
           lost its `material` argument and reads the line off the document it describes.
    0.1.11 records relative lineage workload comparisons and replaces the fixed warning
           threshold; candidate scoring and the uncapped adjudication pass are unchanged.
"""

# Stamped by build_package.py. The repo carries the bare version; the
# build appends its date so a findings file names the package that made it.
__version__ = "0.1.11+build.20261016"
