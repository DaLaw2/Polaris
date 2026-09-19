"""The process-wide objects every router reads at call time."""

from __future__ import annotations

from typing import TYPE_CHECKING

from polaris.shared.db import Store

if TYPE_CHECKING:
    from polaris.search.engine import SearchEngine
    from polaris.vocabulary.service import EntityManager

store = Store()
engine: SearchEngine | None = None
entity_mgr: EntityManager | None = None
