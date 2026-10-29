"""Query construction. One place, so three rules cannot be forgotten anywhere.

1. **Every query carries an explicit LIMIT.** An unbounded `SELECT` against a Domo dataset
   silently truncates at exactly 1,000,000 rows with HTTP 200 and no truncation flag
   anywhere in the response envelope. Verified on a 1,298,371-row table 2026-08-27. The
   client then raises when `numRows` equals the limit, because a result that exactly hits
   its own ceiling is indistinguishable from a truncated one.

2. **ADR-0003's banned idioms are rejected before the query is ever sent.** Absence is SQL
   NULL: `= ''` and `LENGTH(x) = 0` match nothing, and a bare `!=` silently DROPS absent
   rows rather than passing them — on `GP |Domostats | DataSets`, `Status != 'DELETED'`
   discards 597 of 2,205 rows without saying so.

3. **Nullable columns are read through `{?Column}`.** This surface serializes SQL NULL as
   the empty string in its `rows` payload (verified 2026-08-28), so absence is invisible to
   the reader unless the query preserves it. `{?Column}` renders
   `COALESCE(col, '__ABSENT__') AS col`, which the typed reader recognises as absence. A
   bare `{Column}` read that comes back empty is reported as AMBIGUOUS rather than guessed
   at. See `shared.typed`.

4. **Every column a query references is recorded by name.** Columns are written as
   `{Column Name}` placeholders and rendered to quoted identifiers here, so the set of
   referenced columns is exact rather than regex-guessed. `collector.tests.test_drift`
   asserts that set against `reports.yaml`, which is what keeps the manifest honest once
   the queries start changing.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence

from .typed import ABSENT_SENTINEL


class SqlGuardError(Exception):
    """A query violated one of the three rules above. Raised at build time, never at
    run time — a banned idiom must fail before it can produce a plausible wrong answer."""


# {Column} plain · {?Column} absence-preserving read (SELECT list) · {~Column} absence-
# preserving expression (WHERE / GROUP BY, where an alias is illegal)
_PLACEHOLDER = re.compile(r"\{([?~]?)([^{}]+)\}")
_QUOTED_IDENT = re.compile(r"`(?:[^`]|``)*`\s*$")
_BANNED_EQ_EMPTY = re.compile(r"(?:!=|=|<>)\s*(''|\"\")")
_BANNED_LENGTH_ZERO = re.compile(r"\bLENGTH\s*\([^()]*\)\s*(?:=|!=|<>|<|>)\s*0", re.IGNORECASE)
_INEQUALITY = re.compile(r"(!=|<>|\bNOT\s+IN\b)", re.IGNORECASE)
_NULL_SAFE_WRAPPERS = ("coalesce", "ifnull")
_FROM = re.compile(r"\bFROM\b", re.IGNORECASE)


def ident(column: str) -> str:
    """Quote a column name as a Domo SQL identifier.

    Governance report column names carry spaces, pipes, slashes and mixed case
    (`Database ProcessingType`, `Last Modified Date/Time`), so every reference is quoted.
    """
    return "`" + column.replace("`", "``") + "`"


def literal(value) -> str:
    """A SQL string literal. Dataset IDs and SKU patterns arrive from config and from the
    instance itself, so they are escaped rather than interpolated raw."""
    if value is None:
        raise ValueError("literal(None): use IS NULL, never a NULL literal — see ADR-0003")
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return "'" + text + "'"


def in_list(values: Iterable) -> str:
    """`IN (...)` over string values. Empty input raises: `IN ()` is a syntax error on some
    engines and silently matches nothing on others, and 'matches nothing' is exactly the
    class of quiet wrong answer this module exists to prevent."""
    rendered = [literal(v) for v in values]
    if not rendered:
        raise ValueError("in_list(): empty value list. Guard the call site instead.")
    return "(" + ", ".join(rendered) + ")"


def not_equal(column: str, value) -> str:
    """Exclude one value from a NULLABLE column, keeping the absent rows.

    ADR-0003 decision 3: the correct spelling is
    `COALESCE(col, '<sentinel>') != 'VALUE'`. A bare `!=` drops the NULLs.
    """
    return f"COALESCE({ident(column)}, {literal(ABSENT_SENTINEL)}) != {literal(value)}"


def not_in(column: str, values: Iterable) -> str:
    """Exclude several values from a NULLABLE column, keeping the absent rows."""
    return (
        f"COALESCE({ident(column)}, {literal(ABSENT_SENTINEL)}) "
        f"NOT IN {in_list(values)}"
    )


class Query:
    """A built, validated query. Immutable; carries the columns it referenced."""

    __slots__ = (
        "report",
        "sql",
        "limit",
        "columns",
        "name",
        "modes",
        "bare_selects",
        "guaranteed",
    )

    def __init__(
        self,
        report: str,
        sql: str,
        limit: int,
        columns: Sequence[str],
        name: str,
        modes: Optional[dict] = None,
        bare_selects: Optional[Iterable[str]] = None,
        guaranteed: Optional[Iterable[str]] = None,
    ):
        self.report = report
        self.sql = sql
        self.limit = limit
        self.columns = tuple(columns)
        self.name = name
        # column -> set of placeholder modes used ("" plain, "?" read, "~" expression).
        # The drift test reads this to assert every nullable column is read absence-
        # preserving; guessing it back out of the rendered SQL would be a parser.
        self.modes = modes or {}
        # Columns returned as a BARE value in the select list — outside any function call,
        # so the raw column value reaches the reader. A nullable column here must either be
        # read through {?} or be declared `guaranteed`. A nullable column used only inside
        # COUNT(DISTINCT ...) or an IS NOT NULL predicate is not in this set, because SQL
        # handles NULL correctly server-side and no value reaches the reader.
        self.bare_selects = frozenset(bare_selects or ())
        # Columns the query author states cannot be absent in THIS result, because the
        # WHERE clause excludes absence. An explicit declaration rather than an inference,
        # in the same spirit as declaring what absence means for a typed read.
        self.guaranteed = frozenset(guaranteed or ())

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Query({self.report}/{self.name}, limit={self.limit}, cols={list(self.columns)})"


def build(
    report: str,
    template: str,
    limit: int,
    name: str = "",
    guaranteed: Optional[Iterable[str]] = None,
) -> Query:
    """Render `{Column Name}` placeholders, append the LIMIT, and lint the result.

    `limit` is mandatory and has no default. A default limit is a silent cap, which is the
    1,000,000-row truncation wearing our own name.

    `guaranteed` names columns that are read as bare select-list values but cannot be absent
    in this result because the WHERE clause excludes absence. Declaring it is what lets the
    drift test tell a genuine unguarded nullable read from a guarded one.
    """
    if not isinstance(limit, int) or limit <= 0:
        raise SqlGuardError(
            f"{report}/{name}: limit must be a positive int. Every query carries an "
            f"explicit LIMIT — there is no default, because a default is a silent cap."
        )

    referenced: list = []
    modes: dict = {}
    select_list_end = _FROM.search(template)
    select_list_end = select_list_end.start() if select_list_end else len(template)
    misplaced: list = []

    def _render(match: "re.Match") -> str:
        mode, column = match.group(1), match.group(2)
        if column not in referenced:
            referenced.append(column)
        modes.setdefault(column, set()).add(mode)
        if mode == "?":
            if match.start() > select_list_end:
                misplaced.append(column)
            return f"COALESCE({ident(column)}, {literal(ABSENT_SENTINEL)}) AS {ident(column)}"
        if mode == "~":
            return f"COALESCE({ident(column)}, {literal(ABSENT_SENTINEL)})"
        return ident(column)

    sql = _PLACEHOLDER.sub(_render, template).strip()

    if misplaced:
        raise SqlGuardError(
            f"{report}/{name}: {{?{misplaced[0]}}} appears outside the SELECT list, where its "
            f"alias is illegal. Use {{~{misplaced[0]}}} for an absence-preserving expression "
            f"in a WHERE or GROUP BY."
        )

    if not referenced:
        raise SqlGuardError(
            f"{report}/{name}: no {{Column}} placeholders. Columns must be written as "
            f"placeholders so the drift test can see them; a bare backticked name is "
            f"invisible to it."
        )

    _lint(report, name, sql)

    if re.search(r"\bLIMIT\b", sql, re.IGNORECASE):
        raise SqlGuardError(
            f"{report}/{name}: LIMIT is appended by the builder and must not appear in the "
            f"template — two limits mean the truncation check guards the wrong number."
        )

    sql = f"{sql} LIMIT {limit}"
    bare = _bare_select_columns(template, select_list_end)
    unknown = [c for c in (guaranteed or ()) if c not in referenced]
    if unknown:
        raise SqlGuardError(
            f"{report}/{name}: guaranteed={unknown} names columns this query does not "
            f"reference. A stale guarantee is worse than none — it silences the drift test "
            f"for a column that is no longer read."
        )
    return Query(
        report=report,
        sql=sql,
        limit=limit,
        columns=referenced,
        name=name or "query",
        modes=modes,
        bare_selects=bare,
        guaranteed=guaranteed,
    )


def _bare_select_columns(template: str, select_list_end: int) -> set:
    """Columns written as a plain `{Column}` in the select list, outside any function call.

    Walks the template rather than the rendered SQL: the placeholder syntax makes the
    intent unambiguous, whereas the rendered SQL would need a parser to tell a column
    reference from an alias.
    """
    bare: set = set()
    depth = 0
    position = 0
    text = template[:select_list_end]
    while position < len(text):
        char = text[position]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "{":
            closing = text.find("}", position)
            if closing == -1:
                break
            body = text[position + 1 : closing]
            mode = body[0] if body[:1] in ("?", "~") else ""
            if mode == "" and depth == 0:
                bare.add(body)
            position = closing
        position += 1
    return bare


def _lint(report: str, name: str, sql: str) -> None:
    where = f"{report}/{name or 'query'}"

    match = _BANNED_EQ_EMPTY.search(sql)
    if match:
        raise SqlGuardError(
            f"{where}: banned idiom {match.group(0)!r}. Absence is SQL NULL — an "
            f"empty-string comparison matches NOTHING on a Domo governance column "
            f"(measured: 0 of 7 absent rows). Use IS NULL / IS NOT NULL. ADR-0003."
        )

    match = _BANNED_LENGTH_ZERO.search(sql)
    if match:
        raise SqlGuardError(
            f"{where}: banned idiom {match.group(0)!r}. LENGTH()=0 matches nothing on an "
            f"absent column (measured: 0 of 7). Use IS NULL. ADR-0003."
        )

    for match in _INEQUALITY.finditer(sql):
        head = sql[: match.start()].rstrip()
        if head.endswith(")") and _closes_null_safe_wrapper(head):
            continue
        raise SqlGuardError(
            f"{where}: bare {match.group(0)!r} DROPS absent rows rather than passing them. "
            f"On `GP |Domostats | DataSets`, `Status != 'DELETED'` silently discards 597 of "
            f"2,205 rows. Use sql.not_equal()/sql.not_in(), which wrap the column in "
            f"COALESCE. ADR-0003 decision 3."
        )


def _closes_null_safe_wrapper(head: str) -> bool:
    """True when `head` ends with a balanced call to COALESCE/IFNULL.

    Walks back from the final `)` to its matching `(` and checks the function name, so a
    nested expression inside the wrapper does not fool it.
    """
    depth = 0
    for position in range(len(head) - 1, -1, -1):
        char = head[position]
        if char == ")":
            depth += 1
        elif char == "(":
            depth -= 1
            if depth == 0:
                prefix = head[:position].rstrip().lower()
                return any(prefix.endswith(w) for w in _NULL_SAFE_WRAPPERS)
    return False


def count_query(report: str, column: str, limit: int = 1, name: str = "count") -> Query:
    """`SELECT COUNT(*)` referencing one column, so the report's identity is still asserted
    by the query rather than by the dataset id alone."""
    return build(
        report,
        f"SELECT COUNT(*) AS n, COUNT({{{column}}}) AS n_present FROM table",
        limit=limit,
        name=name,
    )


def probe_query(report: str, column: str, name: str = "probe") -> Query:
    """The `SELECT ... LIMIT 1` discovery and preflight use to separate the four states a
    dataset can be in: 200+rows, 200+zero rows, 410 archived, 404 absent.

    Deliberately NOT a `COUNT(*)`: a count returns one row for an empty dataset too, so it
    cannot distinguish 200+rows from 200+zero rows — the failure mode preflight exists for.
    """
    return build(report, f"SELECT {{?{column}}} FROM table", limit=1, name=name)
