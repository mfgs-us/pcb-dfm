from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Callable, Dict, Generic, Hashable, Optional, TypeVar

T = TypeVar("T")


@dataclass
class GeometryCache:
    """
    Per-run cache for derived geometry artifacts.

    Intended usage:
      - Construct once per ingest+geometry build.
      - Passed on the CheckContext so checks can memoize expensive derived objects.

    Thread-safety:
      - Safe for concurrent reads/writes (simple lock).
      - Cache entries are computed once per key.
    """
    _store: Dict[Hashable, Any] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock)

    def has(self, key: Hashable) -> bool:
        with self._lock:
            return key in self._store

    def get(self, key: Hashable, default: Optional[T] = None) -> Optional[T]:
        with self._lock:
            return self._store.get(key, default)  # type: ignore[return-value]

    def set(self, key: Hashable, value: T) -> T:
        with self._lock:
            self._store[key] = value
        return value

    def get_or_compute(self, key: Hashable, fn: Callable[[], T]) -> T:
        with self._lock:
            if key in self._store:
                return self._store[key]  # type: ignore[return-value]

        # Compute outside the lock to avoid holding lock during heavy work.
        value = fn()

        with self._lock:
            # Double-check in case another thread computed it.
            if key in self._store:
                return self._store[key]  # type: ignore[return-value]
            self._store[key] = value
            return value

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    @staticmethod
    def key(*parts: Any) -> tuple[Any, ...]:
        """
        Helper to build structured cache keys without string-concatenation bugs.

        Example:
          key = GeometryCache.key("silk", "copper_grids", cell_mm, focus_exposed_only)
        """
        return tuple(parts)
