"""
Storage adapter that uses Replit DB on Replit, or local JSON file elsewhere.
Provides a dict-like interface that mirrors Replit DB API.
"""

import os
import json
import logging
from pathlib import Path
from typing import Any, Protocol, List, Iterable

logger = logging.getLogger(__name__)

DEFAULT_LOCAL_DB_PATH = "data/episodes.json"


def _is_replit_environment():
    """Check if running on Replit platform (dev or published)."""
    # Published apps store DB URL in /tmp/replitdb file
    if Path("/tmp/replitdb").exists():
        return True
    # Development environment uses environment variable
    return os.environ.get("REPLIT_DB_URL") is not None


class DatabaseAdapter(Protocol):
    """Protocol defining the interface for database adapters."""

    def __getitem__(self, key: str) -> Any: ...
    def __setitem__(self, key: str, value: Any) -> None: ...
    def __delitem__(self, key: str) -> None: ...
    def __contains__(self, key: str) -> bool: ...
    def get(self, key: str, default: Any = None) -> Any: ...
    def keys(self) -> Iterable[str]: ...
    def prefix(self, prefix_str: str) -> List[str]: ...


class LocalJSONStore:
    """Dict-like storage backed by a local JSON file."""

    def __init__(self, filepath: str = DEFAULT_LOCAL_DB_PATH) -> None:
        self.filepath = Path(filepath)
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        """Load data from JSON file."""
        if self.filepath.exists():
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Failed to load DB from {self.filepath}: {e}")
                self._data = {}
        else:
            self._data = {}

    def _save(self) -> None:
        """Save data to JSON file."""
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def __getitem__(self, key: str) -> Any:
        self._load()
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._load()
        self._data[key] = value
        self._save()

    def __delitem__(self, key: str) -> None:
        self._load()
        del self._data[key]
        self._save()

    def __contains__(self, key: str) -> bool:
        self._load()
        return key in self._data

    def get(self, key: str, default: Any = None) -> Any:
        self._load()
        return self._data.get(key, default)

    def keys(self) -> Iterable[str]:
        self._load()
        return self._data.keys()

    def prefix(self, prefix_str: str) -> List[str]:
        """Return keys that start with the given prefix (Replit DB API)."""
        self._load()
        return [k for k in self._data.keys() if k.startswith(prefix_str)]


class ReplitDBWrapper:
    """Wrapper around Replit DB that converts ObservedList/ObservedDict to native types."""

    def __init__(self, db: Any) -> None:
        self._db = db

    def _convert(self, value: Any) -> Any:
        """Recursively convert ObservedList/ObservedDict to native Python types."""
        if hasattr(value, "value"):
            value = value.value
        if isinstance(value, dict):
            return {k: self._convert(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._convert(item) for item in value]
        return value

    def __getitem__(self, key: str) -> Any:
        return self._convert(self._db[key])

    def __setitem__(self, key: str, value: Any) -> None:
        self._db[key] = value

    def __delitem__(self, key: str) -> None:
        del self._db[key]

    def __contains__(self, key: str) -> bool:
        return key in self._db

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self._convert(self._db[key])
        except KeyError:
            return default

    def keys(self) -> Iterable[str]:
        return self._db.keys()

    def prefix(self, prefix_str: str) -> List[str]:
        return self._db.prefix(prefix_str)


def get_db() -> DatabaseAdapter:
    """Get the appropriate database based on environment."""
    if _is_replit_environment():
        try:
            from replit import db  # type: ignore

            return ReplitDBWrapper(db)
        except ImportError:
            pass

    return LocalJSONStore()


db = get_db()


def get_all_episodes():
    """Get all episodes from the database."""
    episodes = []
    for key in db.prefix("episode:"):
        episode = db[key]
        episodes.append(episode)

    # Sort by created_at descending
    episodes.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return episodes
