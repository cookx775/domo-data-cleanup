"""The collector's only path to Domo: plain HTTPS with a developer access token.

Ticket 04. Three private endpoints, all proven to work with `X-DOMO-Developer-Token`
alone. The public API (`api.domo.com`, OAuth client-credentials) is out of scope; wrapper
clients — the local MCP server, the `domo` CLI, `community-domo-cli` — stay available for
ad-hoc human investigation but never enter the executed path, and any fact discovered
through one of them is re-verified here before the design relies on it.

Transport posture, all of it deliberate:

* **60s timeout.** A 1.2M-row pull took 18.4s; a COUNT(*) over 1.3M took 0.25s.
* **Three attempts, exponential backoff, on 5xx and timeouts ONLY.** 401/404/410 are
  deterministic answers and retrying them just delays the coverage exception.
* **Serial. No concurrency.** No rate-limit headers are returned on any response, so there
  is nothing to tune against, and a run is dozens of aggregate queries.
* **Raise when `numRows` equals the query's own LIMIT.** The API truncates unbounded
  queries at exactly 1,000,000 rows with HTTP 200 and no flag; a result sitting exactly on
  its ceiling is indistinguishable from a truncated one, so it is never trusted.
* **Every request is timed and attributed IN `_request`** (ticket 41), by path family —
  query API, dataset enumeration, product lineage, other — so no caller can spend time on the
  wire without it showing in the run summary. The bounded lineage pass calls `_request`
  directly; the 2026-09-01 dry run spent 846 calls and about two and a half minutes there
  while the summary reported `4.0s`, because the only counter was on `execute`.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import requests

from .sql import Query
from .typed import Result

log = logging.getLogger("shared.domo")

QUERY_PATH = "/api/query/v1/execute/{dataset_id}"
SCHEMA_PATH = "/api/query/v1/datasources/{dataset_id}/schema/indexed?includeHidden=false"
DATASET_PATH = "/api/data/v3/datasources/{dataset_id}"
SEARCH_PATH = "/api/data/ui/v3/datasources/search"
WHOAMI_PATH = "/api/content/v2/users/me"

DEFAULT_TIMEOUT_S = 60
DEFAULT_ATTEMPTS = 3
SEARCH_PAGE_SIZE = 200
SEARCH_WARN_ABOVE = 10_000
#: Reads unioned before giving up on a complete enumeration. Granite needs one or two: a
#: single read failed 2 of 3 probes on 2026-09-01, losing 15-17 ids to overlapping pages,
#: while the union of two reads was complete every time. Five leaves room for an instance
#: that paginates worse without letting a broken one spin.
SEARCH_ATTEMPTS = 5

#: The path families every request is attributed to (ticket 41). Four of them are the
#: four costs a run has — the fourth, publish, added by ticket 65 and bounded by the number
#: of publications — and the fifth is what preflight spends on shape checks.
QUERY = "query"                 # `/api/query/v1/execute/...` — governance report SQL
ENUMERATION = "enumeration"     # `datasources/search` — the paged dataset list
LINEAGE = "lineage"             # `/api/data/v1/lineage/...` — the bounded per-asset pass
PUBLISH = "publish"             # `/api/publish/v2/...` — what this instance publishes (ticket 65)
OTHER = "other"                 # schema/indexed, users/me
FAMILIES = (QUERY, ENUMERATION, LINEAGE, PUBLISH, OTHER)

_LINEAGE_PREFIX = "/api/data/v1/lineage/"
_QUERY_PREFIX = "/api/query/v1/execute/"
_PUBLISH_PREFIX = "/api/publish/"


def classify_path(path: str) -> str:
    """Which cost family a request path belongs to. Decided from the PATH rather than from
    the calling method, so a caller reaching `_request` directly — as the lineage pass does,
    on purpose — is attributed the same as one going through `execute`."""
    if path.startswith(_QUERY_PREFIX):
        return QUERY
    if path.startswith(SEARCH_PATH):
        return ENUMERATION
    if path.startswith(_LINEAGE_PREFIX):
        return LINEAGE
    if path.startswith(_PUBLISH_PREFIX):
        return PUBLISH
    return OTHER


class RequestTiming:
    """Request count and in-flight seconds per path family.

    Counts HTTP ATTEMPTS, so a request that needed a retry counts twice — that is what the
    instance saw, and what an administrator reading their own audit log will count. Backoff
    sleeps are not in any family's seconds; they are in the run's wall time, which is the
    figure the summary leads with.
    """

    __slots__ = ("_requests", "_seconds")

    def __init__(self) -> None:
        self._requests = {family: 0 for family in FAMILIES}
        self._seconds = {family: 0.0 for family in FAMILIES}

    def record(self, family: str, seconds: float) -> None:
        self._requests[family] += 1
        self._seconds[family] += seconds

    def requests(self, family: str) -> int:
        return self._requests[family]

    def seconds(self, family: str) -> float:
        return self._seconds[family]

    @property
    def total_requests(self) -> int:
        return sum(self._requests.values())

    @property
    def total_seconds(self) -> float:
        return sum(self._seconds.values())

    def as_dict(self) -> dict:
        return {
            family: {"requests": self._requests[family], "seconds": self._seconds[family]}
            for family in FAMILIES
        }


class DomoError(Exception):
    """Base for every failure reaching or reading Domo."""


class AuthError(DomoError):
    """401. The token is bad, expired, or not for this instance.

    Nothing maps a token in hand back to its expiry — `/api/data/v1/accesstokens` lists
    tokens with an `expires` epoch but offers no way to identify which row is yours — so
    'does it work right now' is the only honest check.
    """


class NotFoundError(DomoError):
    """404. No dataset with that id on this instance."""


class ArchivedError(DomoError):
    """410. The dataset is archived. It can still report a fresh `lastUpdated` and a
    plausible `rowCount` in search metadata, which is exactly why preflight queries."""


class TruncationError(DomoError):
    """A result returned exactly as many rows as its own LIMIT allowed.

    Same family as `rowCount` reporting 0 for a populated dataset: the API answers
    confidently and wrongly. Never silently accepted.
    """


class EnumerationError(DomoError):
    """The UNION of several `datasources/search` reads still disagrees with its own
    `totalCount`.

    Same family as the 1,000,000-row truncation, and the nastiest member: the enumeration is
    **non-deterministically incomplete**, and every bad read is an HTTP 200 with no flag.
    Ticket 15 saw three of eight consecutive reads of an unchanged granite come back short
    (2,204 against a `totalCount` of 2,205, a *different* dataset missing each time); ticket
    28 saw two reads return 2,205 rows carrying **2,204 distinct ids**.

    **Ticket 32 found the mechanism, and it is worse than a lost row.** Probing granite three
    times on 2026-09-01: 2,188 distinct, then 2,205, then 2,190. The duplicated rows arrive in
    **whole pages** rather than at page boundaries — one read had pages 2 and 3 carrying 15 of
    the same ids — which is an unstable sort order under offset pagination serving one block
    twice and skipping another of the same size. So the loss is a **pagination artifact**, not
    random server dropout, and it is 15-17 ids rather than one.

    That is why `search_datasets` unions reads rather than retrying for a clean one: a
    different block moves each time, so the union converges in two reads while any single read
    keeps failing. This error means even the union did not get there.

    It is raised rather than tolerated because the collector now reads this enumeration for a
    CONDITION (`duplicated`), not just for discovery's proposal. A missing dataset silently
    drops a twin, and two runs over an unchanged instance would then disagree about what is
    duplicated — against exactly the determinism ADR-0001 claims and the second-run diff
    depends on.
    """


class TransportError(DomoError):
    """5xx or a timeout that survived every retry."""


class ProbeResult:
    """The four states a configured dataset id can be in, per ticket 04 decision 6.

    Distinct from an exception because discovery and preflight both need to RECORD the
    state of a dozen candidates rather than abort on the first bad one.
    """

    __slots__ = ("dataset_id", "status", "rows", "detail")

    def __init__(self, dataset_id: str, status: int, rows: Optional[int], detail: str = ""):
        self.dataset_id = dataset_id
        self.status = status
        self.rows = rows
        self.detail = detail

    @property
    def ok(self) -> bool:
        """200 with at least one row. A valid-but-empty dataset is NOT ok — an existence
        check passes on Altimate's People replicas and the pillar then produces silently
        wrong output."""
        return self.status == 200 and (self.rows or 0) > 0

    @property
    def reason(self) -> Optional[str]:
        """Mapped to `coverage.pillars[].exception.reason` in the findings schema."""
        if self.ok:
            return None
        if self.status == 200:
            return "dataset-empty"
        if self.status == 404:
            return "dataset-missing"
        if self.status in (401, 403):
            return "connection-unavailable"
        return "dataset-error"

    def as_dict(self) -> dict:
        out = {"status": self.status, "rows": self.rows}
        if self.detail:
            out["detail"] = self.detail
        return out

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ProbeResult({self.dataset_id}, {self.status}, rows={self.rows})"


def load_token(env_path: Optional[str] = None, var: str = "DOMO_API_TOKEN") -> str:
    """Resolve the developer token.

    Precedence is the exported environment variable first, then a `.env` beside the
    config. Granite runs from the exported variable; the handoff package ships a
    gitignored `.env` because an admin running this across a review cycle routes around
    an interactive prompt by hardcoding the token, which is the worst outcome.
    """
    token = os.environ.get(var, "").strip()
    if token:
        return token
    if env_path and os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == var:
                    return value.strip().strip("'\"")
    raise AuthError(
        f"No {var}. Export it, or put it in a gitignored .env beside the config. "
        f"There is no interactive prompt on purpose."
    )


class DomoClient:
    def __init__(
        self,
        instance: str,
        token: str,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        attempts: int = DEFAULT_ATTEMPTS,
        session: Optional["requests.Session"] = None,
        clock=time.monotonic,
    ) -> None:
        # Accept 'granite', 'granite.domo.com' or a full URL; normalise to one base.
        host = instance.strip().rstrip("/")
        for prefix in ("https://", "http://"):
            if host.startswith(prefix):
                host = host[len(prefix) :]
        if "." not in host:
            host = f"{host}.domo.com"
        self.host = host
        self.base = f"https://{host}"
        self.timeout_s = timeout_s
        self.attempts = attempts
        #: Injectable so an offline test can prove attribution with a fake clock. Monotonic
        #: because these are durations, never timestamps.
        self._clock = clock
        #: Every HTTP attempt, by path family. Recorded IN `_request`, so a caller reaching
        #: the transport directly is counted like one going through `execute`.
        self.timing = RequestTiming()
        #: What the last dataset enumeration saw, for the caller's coverage block.
        self.last_enumeration: dict = {}
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "X-DOMO-Developer-Token": token,
                "Content-Type": "application/json",
                "Accept": "application/json",
                # Named so an instance owner reading their own audit log can tell what
                # this was. ADR-0002: it reads and never mutates.
                "User-Agent": "granite-instance-cleanup-collector/0.1 (read-only)",
            }
        )

    # -- transport ----------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> "requests.Response":
        url = self.base + path
        kwargs.setdefault("timeout", self.timeout_s)
        family = classify_path(path)
        last_error: Optional[Exception] = None
        for attempt in range(1, self.attempts + 1):
            started = self._clock()
            try:
                response = self._session.request(method, url, **kwargs)
            except (requests.Timeout, requests.ConnectionError) as exc:
                # A timed-out attempt still cost its timeout; it is not free.
                self.timing.record(family, self._clock() - started)
                last_error = exc
                if attempt == self.attempts:
                    break
                self._backoff(attempt, f"{type(exc).__name__} on {path}")
                continue
            self.timing.record(family, self._clock() - started)

            if response.status_code == 401:
                raise AuthError(f"401 on {path}: token rejected by {self.host}")
            if response.status_code == 403:
                raise AuthError(
                    f"403 on {path}: the token authenticates but is not permitted this "
                    f"resource on {self.host}"
                )
            if response.status_code == 404:
                raise NotFoundError(f"404 on {path}")
            if response.status_code == 410:
                raise ArchivedError(f"410 on {path}: datasource has been archived")
            if 500 <= response.status_code < 600:
                last_error = TransportError(f"{response.status_code} on {path}")
                if attempt == self.attempts:
                    break
                self._backoff(attempt, f"{response.status_code} on {path}")
                continue
            if not response.ok:
                raise DomoError(
                    f"{response.status_code} on {path}: {response.text[:300]}"
                )
            return response

        raise TransportError(
            f"{path} failed after {self.attempts} attempts: {last_error}"
        ) from last_error

    @staticmethod
    def _backoff(attempt: int, why: str) -> None:
        delay = 2.0 ** (attempt - 1)
        log.warning("retrying in %.0fs (attempt %d): %s", delay, attempt, why)
        time.sleep(delay)

    @property
    def calls(self) -> int:
        """Every HTTP attempt this client has made, across all path families."""
        return self.timing.total_requests

    @property
    def query_seconds(self) -> float:
        """In-flight seconds on the **query API only** — governance report SQL through
        `execute`. This is NOT the run's time: it excludes the dataset enumeration and the
        product-lineage pass, which on the 2026-09-01 dry run were ~97% of it. Anything that
        prints this must label it as the query API figure; the run's figure is wall time."""
        return self.timing.seconds(QUERY)

    # -- query --------------------------------------------------------------------------

    def execute(self, dataset_id: str, query: Query) -> Result:
        """Run a built query and return a typed Result.

        Raises TruncationError when the row count equals the query's LIMIT.
        """
        started = self._clock()
        response = self._request(
            "POST",
            QUERY_PATH.format(dataset_id=dataset_id),
            json={"sql": query.sql},
        )
        elapsed = self._clock() - started
        payload = response.json()

        columns = [c["name"] if isinstance(c, dict) else str(c) for c in payload.get("columns", [])]
        rows = payload.get("rows", []) or []
        num_rows = payload.get("numRows", len(rows))

        if num_rows >= query.limit:
            raise TruncationError(
                f"{query.report}/{query.name} on {dataset_id} returned {num_rows} rows against "
                f"LIMIT {query.limit}. A result sitting exactly on its own ceiling is "
                f"indistinguishable from a truncated one — the API returns HTTP 200 with no "
                f"truncation flag. Raise the limit and re-run, or aggregate in SQL."
            )

        return Result(
            report=query.report,
            columns=columns,
            rows=rows,
            sql=query.sql,
            limit=query.limit,
            duration_ms=int(elapsed * 1000),
        )

    def probe(self, dataset_id: str, query: Query) -> ProbeResult:
        """Query for state rather than for data. Never raises on 404/410/401 — the caller
        is recording the state of many candidates and must not abort on the first bad one."""
        try:
            result = self.execute(dataset_id, query)
        except NotFoundError:
            return ProbeResult(dataset_id, 404, None, "no dataset with this id")
        except ArchivedError:
            return ProbeResult(dataset_id, 410, None, "datasource has been archived")
        except AuthError as exc:
            return ProbeResult(dataset_id, 403, None, str(exc))
        except TruncationError:
            # A LIMIT 1 probe returning 1 row is the healthy case, not a truncation. The
            # generic check cannot know that, so the probe interprets it.
            return ProbeResult(dataset_id, 200, 1, "")
        except DomoError as exc:
            return ProbeResult(dataset_id, 0, None, str(exc))
        return ProbeResult(dataset_id, 200, len(result), "")

    # -- discovery ----------------------------------------------------------------------

    def search_datasets(
        self,
        page_size: int = SEARCH_PAGE_SIZE,
        warn_above: int = SEARCH_WARN_ABOVE,
        assert_complete: bool = True,
        attempts: int = SEARCH_ATTEMPTS,
    ) -> list:
        """Enumerate every dataset on the instance, with column schemas INLINE, and
        **assert the enumeration is complete**.

        Completeness is checked as `distinct ids == totalCount`, which catches both observed
        failure modes — a short page, and a full-length page carrying a duplicate that
        displaced a real dataset. A count-only check catches only the first, and comparing
        against `COUNT(*)` on the DomoStats dataset inventory catches neither, because both
        sides report 2,205.

        A bad read is not raised on: the reads are **unioned by id** until the union matches
        `totalCount`. That is the mitigation ticket 15's own evidence points at — its first
        batch of four reads gave a union of 2,205 against an intersection of 2,203, so no
        single read was reliable and the union was already complete. Probing the mechanism
        (ticket 32, 2026-09-01) says why: duplicate rows arrive in **whole pages** rather than
        at page boundaries — one read had pages 2 and 3 carrying 15 of the same ids — which is
        an unstable sort order under offset pagination serving one block twice and skipping
        another of the same size. A different block moves each time, so the union converges
        in a couple of reads while any single read keeps failing. Retrying for one clean read
        would have failed granite twice in three tries.

        A union is sound in the one direction that matters: every id in it was returned by
        the server, so nothing is invented. What it can carry is a dataset deleted between
        reads, which is why `attempts` is small and the count it needed is recorded.

        After `attempts` incomplete unions it raises `EnumerationError` — a run that cannot
        enumerate deterministically must not produce a findings file that looks deterministic.

        `assert_complete=False` is for **discovery only**, which must never fail and always
        exit zero (its whole job is to report every problem on a stranger's instance in one
        pass). It records the shortfall in its proposal instead. Nothing on the collector's
        scoring path may pass it.

        `last_enumeration` carries what the final read saw, for the caller's coverage block.

        `datasources/search` returns each dataset's full column schema in the same
        response, so nothing here needs a `schema/indexed` call at all — granite's 2,205
        datasets are 12 calls and ~18s.

        Two things about the request body, both verified 2026-08-28 and neither optional:

        * **`"filters": []` is REQUIRED.** Omitting the key returns HTTP 500 with
          `Cannot read the array length because the return value of
          QueryRequest.getFilters() is null` — a server-side NPE, not a validation error.
        * **The total lives at `_metaData.totalCount`,** not at the top level. The
          response envelope is `{dataSources, _metaData}` and carries no top-level count.

        This is the undocumented private surface changing shape under us, which ticket 04
        accepted as a risk rather than hedging against. It is also why the count is read
        defensively below: pagination terminates on a short page regardless.

        Warns above `warn_above` datasets and NEVER caps. Cost is linear, 10,000 is ~5x
        granite, and a silent cap is the 1,000,000-row truncation wearing our own name.
        """
        union: dict = {}
        total: Optional[int] = None
        first_read = 0
        for attempt in range(1, max(1, attempts) + 1):
            datasets, reported = self._enumerate(page_size, warn_above)
            if reported is not None:
                # The MAX, not the latest: a dataset created mid-enumeration raises the
                # server's own count, and reading it downward would declare the union
                # complete while it was one short.
                total = reported if total is None else max(total, reported)
            for item in datasets:
                dataset_id = item.get("id")
                if dataset_id and dataset_id not in union:
                    union[dataset_id] = item
            if attempt == 1:
                first_read = len({item.get("id") for item in datasets})

            complete = total is None or len(union) >= total
            self.last_enumeration = {
                "rows": len(datasets),
                "distinct_ids": len(union),
                "distinct_ids_first_read": first_read,
                "total_count": total,
                "complete": complete,
                "attempts": attempt,
                # The reads are unioned, so a caller reading `complete: true` should know
                # whether one read was enough or three were needed.
                "unioned": attempt > 1,
            }
            if complete:
                if attempt > 1:
                    log.info(
                        "%s enumeration completed by UNION over %d reads (%d distinct on the "
                        "first read, %d after): offset pagination served overlapping pages.",
                        self.host,
                        attempt,
                        first_read,
                        len(union),
                    )
                return list(union.values())
            if not assert_complete:
                log.warning(
                    "%s enumeration is INCOMPLETE (%d distinct ids against a totalCount of "
                    "%s) and completeness was not asserted — the caller must treat an absent "
                    "dataset as unknown, never as scored.",
                    self.host,
                    len(union),
                    total,
                )
                return list(union.values())
            log.warning(
                "%s enumeration is incomplete after %d/%d read(s): %d distinct ids against a "
                "totalCount of %s. Re-reading and unioning.",
                self.host,
                attempt,
                attempts,
                len(union),
                total,
            )

        raise EnumerationError(
            f"datasources/search on {self.host} never reached a complete enumeration in "
            f"{attempts} reads: the union carries {len(union)} distinct ids against the "
            f"server's own totalCount of {total}. This surface serves overlapping pages under "
            f"offset pagination — one block twice, another skipped — with HTTP 200 and no "
            f"flag. Scoring `duplicated` off an incomplete enumeration would silently drop a "
            f"twin and make two runs over an unchanged instance disagree, so the run stops "
            f"instead."
        )

    def _enumerate(self, page_size: int, warn_above: int) -> tuple:
        """One full pass of the paged enumeration. Returns (items, totalCount-or-None)."""
        datasets: list = []
        offset = 0
        total: Optional[int] = None
        while True:
            response = self._request(
                "POST",
                SEARCH_PATH,
                json={
                    "entities": ["DATASET"],
                    "filters": [],
                    "query": "*",
                    "count": page_size,
                    "offset": offset,
                },
            )
            payload = response.json()
            if total is None:
                total = (payload.get("_metaData") or {}).get("totalCount")
                if total and total > warn_above:
                    log.warning(
                        "%s reports %d datasets, above the %d warning threshold. Enumerating "
                        "all of them — cost is linear and a cap would be a silent truncation.",
                        self.host,
                        total,
                        warn_above,
                    )
            page = payload.get("dataSources") or payload.get("datasources") or []
            if not page:
                break
            datasets.extend(page)
            offset += len(page)
            if total is not None and offset >= total:
                break
            if len(page) < page_size:
                break
        return datasets, total

    def columns(self, dataset_id: str) -> list:
        """Column names of one CONFIGURED dataset, without a full enumeration.

        Discovery needs no call here — `datasources/search` carries schemas inline — but
        preflight verifies the shape of an id a human already put in the config, and paying
        12 enumeration calls to check five known ids would be absurd.
        """
        payload = self._request("GET", SCHEMA_PATH.format(dataset_id=dataset_id)).json()
        names: list = []
        for table in payload.get("tables") or []:
            for column in table.get("columns") or []:
                name = column.get("name")
                if name:
                    names.append(name)
        return names

    def dataset_name(self, dataset_id: str) -> str:
        """Read the name of one ID, including IDs omitted by dataset enumeration."""
        payload = self._request("GET", DATASET_PATH.format(dataset_id=dataset_id)).json()
        return str(payload.get("name") or "")

    def whoami(self) -> dict:
        return self._request("GET", WHOAMI_PATH).json()
