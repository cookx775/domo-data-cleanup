"""Typed column sets, read off the dataset enumeration. Ticket 32.

The `duplicated` condition needs each output dataset's **typed** column set — `name:TYPE`
pairs — and `datasources/search` already returns every dataset's full schema inline, in the
same response ticket 15 pays 12 calls for. So this adds **no new query, no new report and no
new governance column**: it is a second reader over a call the collector was already
entitled to make (the same shape ADR-0004 settled for the consumption edge).

**Types are read, not just names.** A name-only set would call two datasets twins when one
holds `Amount` as a STRING and the other as a DOUBLE — different objects with the same
column list, and on this surface a type change is exactly how a rebuilt dataflow differs
from the original it replaced.

**Absence is not an empty set** (ADR-0003). 43 of granite's 2,205 datasets report no schema
at all, 8 of them dataflow outputs. `typed_columns` returns **None** for those, and the
duplication rule treats None as `unknown` rather than as "matches the other unknown". Two
schemaless datasets are not twins; they are two datasets nobody can compare.

The reader is deliberately separate from `collector.discover`, which reads the same
enumeration for a different purpose and only needs column *names*. One function serving both
would have to return the looser shape.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Iterable, List, Optional

#: How a column becomes one element of the comparable set. A separator that cannot occur in
#: a Domo column name would be better, but ':' is what a reviewer reads in an evidence line,
#: and the pair is never parsed back apart.
def _typed(name: str, column_type: Optional[str]) -> str:
    return f"{name}:{column_type or '__UNTYPED__'}"


class DatasetSchemas:
    """Typed column sets for every dataset the enumeration described.

    `unschematized` is a list rather than a count: an admin looking at a coverage note needs
    to know *which* datasets could not be compared, because whether it matters depends
    entirely on whether any of them is a dataflow output.
    """

    __slots__ = ("_columns", "unschematized", "datasets_seen", "untyped_columns")

    def __init__(self) -> None:
        self._columns: Dict[str, FrozenSet[str]] = {}
        self.unschematized: List[str] = []
        self.datasets_seen = 0
        #: Columns the enumeration described with a name but no type. Recorded because a
        #: silent `__UNTYPED__` on both sides of a comparison would restore the name-only
        #: behaviour this module exists to avoid.
        self.untyped_columns: List[str] = []

    @classmethod
    def from_search(cls, items: Iterable[dict]) -> "DatasetSchemas":
        self = cls()
        for item in items:
            dataset_id = item.get("id") or ""
            if not dataset_id:
                continue
            self.datasets_seen += 1
            columns: List[str] = []
            for table in (item.get("schema") or {}).get("tables") or []:
                for column in table.get("columns") or []:
                    name = column.get("name")
                    if not name:
                        continue
                    column_type = column.get("type")
                    if not column_type:
                        self.untyped_columns.append(f"{dataset_id}.{name}")
                    columns.append(_typed(name, column_type))
            if columns:
                # A frozenset, not a list: column ORDER is not part of a dataset's identity
                # (the enumeration reports every column at `order: 0` anyway), and two
                # datasets differing only in column order are the same shape.
                self._columns[dataset_id] = frozenset(columns)
            else:
                self.unschematized.append(dataset_id)
        self.unschematized.sort()
        self.untyped_columns.sort()
        return self

    def typed_columns(self, dataset_id: str) -> Optional[FrozenSet[str]]:
        """`name:TYPE` pairs, or **None** when the instance reported no schema.

        None is not `frozenset()`. A caller that treats them alike would pair every
        schemaless dataset with every other one.
        """
        return self._columns.get(dataset_id)

    def __len__(self) -> int:
        return len(self._columns)

    def stats(self) -> dict:
        return {
            "datasets_seen": self.datasets_seen,
            "datasets_with_schema": len(self._columns),
            "datasets_without_schema": len(self.unschematized),
            "columns_without_a_type": len(self.untyped_columns),
        }
