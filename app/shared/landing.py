"""The landing: when data last arrived on a dataset, read off the dataset enumeration.

Split out of the consumption reader (`shared/consumption.py`, formerly cleanup's) when
instance-documentation's recency signal became its second reader (D2 ticket 09). instance-cleanup ticket 43: DomoStats `Last_Run_Date` is a run
ATTEMPT; this is the landing, and a connector that runs on schedule returning nothing has the
first and not the second. The two are never merged.
"""

from __future__ import annotations

from typing import Any

#: The `datasources/search` field that carries the landing, as epoch milliseconds.
LANDING_FIELD = "lastUpdated"
SEARCH_REPORT = "datasources/search"


def landing_raw(raw: Any) -> Any:
    """The enumeration's landing value with its spelling of "never landed" resolved to None.

    **The surface spells "never landed" as `0`, and that spelling is resolved HERE, at the
    boundary, into `absent`.** Measured on granite 2026-09-02 over all 2,205 datasets:
    `lastUpdated` is an epoch-milliseconds integer on every item, positive on 2,157 and exactly
    `0` on 48, never null and never missing. The 48 are precisely the 45 datasets DomoStats
    reports with `Last_Run_Date` absent (never attempted) plus the 3 that attempted and never
    landed (`last_run_without_data.json`), so the zero is a sentinel rather than a timestamp —
    no dataset landed data in 1970. Letting it reach a date conversion would render "data last
    landed 1970-01-01", the confident-zero family's newest member wearing a date. ADR-0003 says
    no code path may conflate `absent` and `0`; this one *translates* a surface's spelling of
    absence, and does it in one place with the measurement beside it.

    `dataLastUpdated` sits next to it in the payload and is `0` on all 2,205 — an unpopulated
    field, not a second opinion. Every other value passes through for the caller to type.
    """
    return None if raw == 0 else raw
