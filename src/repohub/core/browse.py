from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml

from repohub.core.models import SearchFilters


@dataclass(frozen=True)
class Shelf:
    name: str
    query: str = ""
    topic: str | None = None
    language: str | None = None
    min_stars: int = 100
    days: int = 365

    def filters(self) -> SearchFilters:
        return SearchFilters(language=self.language, min_stars=self.min_stars,
                             updated_within_days=self.days, topic=self.topic)


def load_shelves(path: Path | str | None = None) -> list[Shelf]:
    text = Path(path).read_text() if path else resources.files("repohub.core").joinpath("shelves.yaml").read_text()
    data = yaml.safe_load(text) or []
    if not isinstance(data, list):
        raise ValueError("shelves file must contain a list")
    shelves = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"invalid shelf #{i}: expected a mapping, got {type(item).__name__}")
        try:
            shelves.append(Shelf(**item))
        except TypeError as e:
            raise ValueError(f"invalid shelf #{i}: {e}") from e
    return shelves
