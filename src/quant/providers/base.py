from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol


@dataclass(frozen=True)
class ChainPage:
    records: Sequence[Mapping[str, Any]]
    next_cursor: str | None


class ChainHistoryProvider(Protocol):
    name: str

    def fetch_chain_page(
        self,
        underlying: str,
        start_date: date,
        end_date: date,
        interval: str,
        cursor: str | None,
    ) -> ChainPage: ...