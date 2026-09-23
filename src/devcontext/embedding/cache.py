from __future__ import annotations

import json
from pathlib import Path


class EmbeddingCache:
    def __init__(self, path: Path, model: str, dimensions: int) -> None:
        self.path = path
        self.model = model
        self.dimensions = dimensions
        self.values: dict[str, list[float]] = {}
        if path.is_file():
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                        vector = item["vector"]
                        if len(vector) == dimensions:
                            self.values[item["key"]] = vector
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        continue

    def key(self, content_hash: str) -> str:
        return f"{self.model}:{self.dimensions}:{content_hash}"

    def get(self, content_hash: str) -> list[float] | None:
        return self.values.get(self.key(content_hash))

    def append(self, content_hash: str, vector: list[float]) -> None:
        key = self.key(content_hash)
        self.values[key] = vector
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"key": key, "vector": vector}, separators=(",", ":")))
            handle.write("\n")
