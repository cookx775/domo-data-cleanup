"""The typed read layer. ADR-0003: absent is not zero.

Every value the collector reads from a Domo governance report passes through here. No
condition ever sees a raw column value. When a condition asks an absent value for a number
without declaring what absence means for that test, this module RAISES. It never defaults
to zero.

Three values are distinct and no code path may conflate any two of them:

    absent      the column is SQL NULL — the field does not apply to this object
    ""          the column is present and empty
    0           the column is present and genuinely zero

`Dataflow Details` delivers every numeric as a STRING, and `CAST('' AS DOUBLE)` yields NULL
rather than 0, so an unguarded cast turns absence into a silent null that then compares
false against every threshold. That is the failure this module exists to make impossible.

## The transport inverts ADR-0003's test, so absence is resolved in SQL

ADR-0003 says absence is testable only with `IS NULL`, and that `= ''` matches nothing.
Both are true **in SQL** and neither is true **in the JSON that comes back**: verified
2026-08-28, `query/v1/execute` serializes SQL NULL as the empty string in its `rows`
payload. Server-side, `Status IS NULL` matches 597 rows of `GP |Domostats | DataSets` while
`Status = ''` matches 0. Client-side, those same 597 rows arrive carrying `""`.

So a reader testing `raw is None` never sees absence at all, and a reader testing `raw == ""`
cannot tell absence from a genuinely empty string. Absence is therefore resolved **in the
query**, by reading nullable columns through `{?Column}`, which renders
`COALESCE(col, '__ABSENT__')` — see `shared.sql`. The sentinel is unambiguous: it appears
in zero rows of every column tested on granite.

A bare `""` reaching this module is consequently neither present nor absent but
**ambiguous** — the mark of a column read without the absence-preserving form. Asking an
ambiguous value for anything RAISES and names the fix, rather than picking a meaning.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Optional, Sequence

# The string a nullable column is COALESCEd to so that SQL NULL survives a transport that
# would otherwise flatten it to "". Defined here rather than in `shared.sql` because the
# reader is the module that must recognise it, and `sql` imports this module.
ABSENT_SENTINEL = "__ABSENT__"


class AbsentValueError(Exception):
    """A condition asked an absent value for a typed value without declaring what
    absence means for its test.

    This is a collector error, never a zero. The message names the column and the
    caller so the first run on a new instance says exactly which condition has not
    thought about absence.
    """


class AmbiguousValueError(Exception):
    """A column came back as a bare empty string, which this transport uses for BOTH SQL
    NULL and a genuinely empty string.

    The value is unreadable and no default is safe. The fix is always the same: read the
    column through the `{?Column}` placeholder so absence arrives as the sentinel.
    """


class CoverageError(Exception):
    """A structural disagreement in the source data that must be reported rather than
    resolved silently in favour of either side — e.g. a declared count that does not
    match the edge rows underneath it, or a population filter tripping on an object
    class this instance has never seen.
    """


class _Required:
    """Sentinel meaning 'absence is not declared for this read'."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<undeclared>"


REQUIRED = _Required()


class Value:
    """One column of one row, carrying its own provenance.

    Construct through `Row.get`, never directly, so the column name and report key are
    always available for the raise message.
    """

    __slots__ = ("_raw", "column", "report", "_absent", "_ambiguous")

    def __init__(self, raw: Any, column: str, report: str) -> None:
        self._raw = raw
        self.column = column
        self.report = report
        # Absence arrives one of two ways: as a real None (fixtures, and any transport that
        # preserves nulls) or as the sentinel this query surface needs in order to make SQL
        # NULL visible at all. A bare empty string is neither — it is ambiguous.
        self._absent = raw is None or raw == ABSENT_SENTINEL
        self._ambiguous = (not self._absent) and isinstance(raw, str) and raw == ""

    # -- interrogating presence is always allowed -------------------------------------
    # Asking WHETHER a value is absent is not asking it for a number, so it needs no
    # declaration. This is the escape hatch that lets a condition branch on absence
    # instead of guessing at it.

    @property
    def is_absent(self) -> bool:
        self._guard_ambiguous("is_absent")
        return self._absent

    @property
    def is_present(self) -> bool:
        self._guard_ambiguous("is_present")
        return not self._absent

    @property
    def is_ambiguous(self) -> bool:
        """True when the value is unreadable because the query did not preserve absence.
        The only accessor that does not raise on an ambiguous value."""
        return self._ambiguous

    def _guard_ambiguous(self, operation: str) -> None:
        if not self._ambiguous:
            return
        raise AmbiguousValueError(
            f"{self.report}.{self.column} came back as a bare empty string. This query "
            f'surface serializes SQL NULL as "", so absence and a genuinely empty string '
            f"are indistinguishable here ({operation} cannot be answered). Read the column "
            f"through the {{?{self.column}}} placeholder, which renders "
            f"COALESCE(col, '{ABSENT_SENTINEL}') so absence arrives distinguishable. "
            f"ADR-0003."
        )

    def raw(self) -> Any:
        """The value exactly as the API delivered it. For evidence and error text only —
        never for comparison."""
        return self._raw

    # -- typed reads -------------------------------------------------------------------

    def _absent_value(self, declared: Any, kind: str) -> Any:
        if isinstance(declared, _Required):
            raise AbsentValueError(
                f"{self.report}.{self.column} is absent and this read did not declare what "
                f"absence means for it (as_{kind}). Absent is not zero — see ADR-0003. "
                f"Pass absent=<value> to state the intent, or branch on .is_absent."
            )
        return declared

    def as_str(self, absent: Any = REQUIRED) -> Any:
        self._guard_ambiguous("as_str")
        if self._absent:
            return self._absent_value(absent, "str")
        return str(self._raw)

    def as_int(self, absent: Any = REQUIRED) -> Any:
        self._guard_ambiguous("as_int")
        if self._absent:
            return self._absent_value(absent, "int")
        return self._to_int(self._raw)

    def as_float(self, absent: Any = REQUIRED) -> Any:
        self._guard_ambiguous("as_float")
        if self._absent:
            return self._absent_value(absent, "float")
        return self._to_float(self._raw)

    def as_bool(self, absent: Any = REQUIRED) -> Any:
        """Domo delivers booleans as real booleans in some reports and as the strings
        'true'/'false'/'1'/'0' in others. Anything unrecognised raises rather than
        collapsing to False, which would read as 'not enabled' for a dataflow that is."""
        self._guard_ambiguous("as_bool")
        if self._absent:
            return self._absent_value(absent, "bool")
        raw = self._raw
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("true", "t", "1", "yes"):
            return True
        if text in ("false", "f", "0", "no"):
            return False
        raise ValueError(
            f"{self.report}.{self.column}: {raw!r} is not a boolean. Refusing to guess — a "
            f"wrong guess here reads as 'disabled' for a dataflow that is running."
        )

    def as_datetime(self, absent: Any = REQUIRED) -> Any:
        self._guard_ambiguous("as_datetime")
        if self._absent:
            return self._absent_value(absent, "datetime")
        return self._to_datetime(self._raw, self.report, self.column)

    # -- casts --------------------------------------------------------------------------

    def _to_int(self, raw: Any) -> int:
        if isinstance(raw, bool):
            raise ValueError(f"{self.report}.{self.column}: refusing to read a bool as an int")
        if isinstance(raw, int):
            return raw
        if isinstance(raw, float):
            if raw != int(raw):
                raise ValueError(f"{self.report}.{self.column}: {raw!r} is not an integer")
            return int(raw)
        text = str(raw).strip()
        if text == "":
            # PRESENT and empty. Not absent, and emphatically not zero. A report that
            # delivers '' where it means 0 is a report whose semantics we do not know,
            # so this raises rather than picking one.
            raise ValueError(
                f"{self.report}.{self.column}: present-but-empty string is neither absent "
                f"nor zero (ADR-0003). Refusing to cast."
            )
        return int(float(text))

    def _to_float(self, raw: Any) -> float:
        if isinstance(raw, bool):
            raise ValueError(f"{self.report}.{self.column}: refusing to read a bool as a float")
        if isinstance(raw, (int, float)):
            return float(raw)
        text = str(raw).strip()
        if text == "":
            raise ValueError(
                f"{self.report}.{self.column}: present-but-empty string is neither absent "
                f"nor zero (ADR-0003). Refusing to cast."
            )
        return float(text)

    @staticmethod
    def _to_datetime(raw: Any, report: str, column: str) -> _dt.datetime:
        if isinstance(raw, _dt.datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=_dt.timezone.utc)
        if isinstance(raw, (int, float)):
            # Domo hands epoch milliseconds in some reports and seconds in others. The
            # boundary is unambiguous: anything past 1e11 cannot be seconds in this era.
            seconds = float(raw) / 1000.0 if float(raw) > 1e11 else float(raw)
            return _dt.datetime.fromtimestamp(seconds, tz=_dt.timezone.utc)
        text = str(raw).strip()
        if text == "":
            raise ValueError(
                f"{report}.{column}: present-but-empty string is not a timestamp and is not "
                f"absent (ADR-0003). Refusing to cast."
            )
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                parsed = _dt.datetime.strptime(text, fmt)
            except ValueError:
                continue
            return parsed.replace(tzinfo=_dt.timezone.utc)
        try:
            parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"{report}.{column}: cannot parse {raw!r} as a timestamp") from None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.timezone.utc)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        if self._ambiguous:
            state = "ambiguous"
        elif self._absent:
            state = "absent"
        else:
            state = repr(self._raw)
        return f"Value({self.report}.{self.column}={state})"


class Row:
    """One row of a query result, addressed by column name.

    Asking for a column the query did not select is a KeyError, not an absent value —
    a typo in a column name must never look like a null in the data.
    """

    __slots__ = ("_values", "_index", "report")

    def __init__(self, values: Sequence[Any], index: dict, report: str) -> None:
        self._values = values
        self._index = index
        self.report = report

    def get(self, column: str) -> Value:
        try:
            position = self._index[column]
        except KeyError:
            raise KeyError(
                f"{self.report}: no column {column!r} in this result "
                f"(selected: {sorted(self._index)}). A missing column is a query bug, not "
                f"an absent value."
            ) from None
        return Value(self._values[position], column, self.report)

    def __contains__(self, column: str) -> bool:
        return column in self._index

    @property
    def columns(self) -> list:
        return sorted(self._index, key=lambda c: self._index[c])

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Row({self.report}: {dict(zip(self.columns, self._values))})"


class Result:
    """A typed query result: column names plus rows, iterable as `Row`s."""

    __slots__ = ("report", "columns", "_rows", "sql", "limit", "duration_ms", "_index")

    def __init__(
        self,
        report: str,
        columns: Sequence[str],
        rows: Sequence[Sequence[Any]],
        sql: str = "",
        limit: Optional[int] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        self.report = report
        self.columns = list(columns)
        self._rows = [list(r) for r in rows]
        self.sql = sql
        self.limit = limit
        self.duration_ms = duration_ms
        self._index = {name: i for i, name in enumerate(self.columns)}

    def __len__(self) -> int:
        return len(self._rows)

    def __iter__(self):
        for raw in self._rows:
            yield Row(raw, self._index, self.report)

    def one(self) -> Row:
        if len(self._rows) != 1:
            raise ValueError(
                f"{self.report}: expected exactly one row, got {len(self._rows)}"
            )
        return Row(self._rows[0], self._index, self.report)

    def scalar(self, absent: Any = REQUIRED) -> Any:
        """The single value of a single-row single-column result, e.g. a COUNT(*)."""
        if len(self.columns) != 1:
            raise ValueError(f"{self.report}: scalar() needs one column, got {self.columns}")
        if not self._rows:
            return Value(None, self.columns[0], self.report).as_int(absent=absent)
        return Value(self._rows[0][0], self.columns[0], self.report)

    @property
    def raw_rows(self) -> list:
        """Untyped rows. For fixture capture and evidence dumps only."""
        return [list(r) for r in self._rows]
