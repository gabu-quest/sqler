"""Document generators with configurable profiles for benchmark data."""

from __future__ import annotations

import random
import string


class DocumentGenerator:
    """Generates documents of varying sizes for insertion benchmarks.

    Profiles control the shape and size of generated JSON documents:
    - tiny:   ~50 bytes — 3 fields, short strings
    - small:  ~200 bytes — 5 fields, tags array
    - medium: ~500 bytes — 8 fields, nested object, tags
    - large:  ~2KB — 12 fields, nested objects, large text
    - huge:   ~10KB — deep nesting, arrays of objects, long text
    """

    WORDS = [
        "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
        "hotel", "india", "juliet", "kilo", "lima", "mike", "november",
        "oscar", "papa", "quebec", "romeo", "sierra", "tango", "uniform",
        "victor", "whiskey", "xray", "yankee", "zulu",
    ]

    CATEGORIES = ["tech", "science", "art", "sports", "music", "food", "travel", "health"]
    STATUSES = ["active", "inactive", "pending", "archived"]

    def __init__(self, seed: int = 42):
        self.rng = random.Random(seed)

    def _word(self) -> str:
        return self.rng.choice(self.WORDS)

    def _sentence(self, n: int = 8) -> str:
        return " ".join(self._word() for _ in range(n))

    def _paragraph(self, sentences: int = 5) -> str:
        return ". ".join(self._sentence(self.rng.randint(6, 12)) for _ in range(sentences)) + "."

    def _tags(self, n: int) -> list[str]:
        return self.rng.sample(self.WORDS, min(n, len(self.WORDS)))

    def generate(self, profile: str, count: int) -> list[dict]:
        """Generate `count` documents of the given profile."""
        gen = getattr(self, f"_profile_{profile}", None)
        if gen is None:
            raise ValueError(f"Unknown profile: {profile!r}")
        return [gen(i) for i in range(count)]

    def _profile_tiny(self, i: int) -> dict:
        return {
            "name": f"{self._word()}-{i}",
            "value": self.rng.randint(0, 10000),
            "active": self.rng.choice([True, False]),
        }

    def _profile_small(self, i: int) -> dict:
        return {
            "name": f"{self._word()}-{i}",
            "value": self.rng.randint(0, 10000),
            "category": self.rng.choice(self.CATEGORIES),
            "tags": self._tags(3),
            "score": round(self.rng.uniform(0, 100), 2),
        }

    def _profile_medium(self, i: int) -> dict:
        return {
            "name": f"{self._word()}-{i}",
            "value": self.rng.randint(0, 100000),
            "category": self.rng.choice(self.CATEGORIES),
            "status": self.rng.choice(self.STATUSES),
            "tags": self._tags(5),
            "score": round(self.rng.uniform(0, 100), 2),
            "description": self._sentence(12),
            "meta": {
                "source": self._word(),
                "priority": self.rng.randint(1, 5),
                "verified": self.rng.choice([True, False]),
            },
        }

    def _profile_large(self, i: int) -> dict:
        return {
            "name": f"{self._word()}-{i}",
            "value": self.rng.randint(0, 1000000),
            "category": self.rng.choice(self.CATEGORIES),
            "status": self.rng.choice(self.STATUSES),
            "tags": self._tags(8),
            "score": round(self.rng.uniform(0, 100), 2),
            "description": self._paragraph(3),
            "body": self._paragraph(5),
            "meta": {
                "source": self._word(),
                "priority": self.rng.randint(1, 10),
                "verified": self.rng.choice([True, False]),
                "region": self._word(),
            },
            "author": {"name": self._sentence(2), "email": f"{self._word()}@example.com"},
            "views": self.rng.randint(0, 100000),
            "rating": round(self.rng.uniform(1, 5), 1),
        }

    def _profile_huge(self, i: int) -> dict:
        return {
            "name": f"{self._word()}-{i}",
            "value": self.rng.randint(0, 10000000),
            "category": self.rng.choice(self.CATEGORIES),
            "status": self.rng.choice(self.STATUSES),
            "tags": self._tags(12),
            "score": round(self.rng.uniform(0, 100), 2),
            "description": self._paragraph(10),
            "body": self._paragraph(20),
            "meta": {
                "source": self._word(),
                "priority": self.rng.randint(1, 10),
                "verified": self.rng.choice([True, False]),
                "region": self._word(),
                "extra": {
                    "flag_a": True,
                    "flag_b": self.rng.randint(0, 999),
                    "notes": self._sentence(15),
                },
            },
            "author": {"name": self._sentence(2), "email": f"{self._word()}@example.com"},
            "events": [
                {
                    "type": self.rng.choice(["view", "click", "purchase", "share"]),
                    "amount": round(self.rng.uniform(1, 500), 2),
                    "active": self.rng.choice([True, False]),
                    "ts": f"2025-01-{self.rng.randint(1,28):02d}",
                }
                for _ in range(self.rng.randint(5, 15))
            ],
            "comments": [
                {"user": self._word(), "text": self._sentence(10)}
                for _ in range(self.rng.randint(3, 8))
            ],
            "views": self.rng.randint(0, 1000000),
            "rating": round(self.rng.uniform(1, 5), 1),
        }

    def generate_with_events(self, count: int, events_per_doc: int = 5) -> list[dict]:
        """Generate docs with predictable event arrays for any().where() benchmarks."""
        docs = []
        event_types = ["view", "click", "purchase", "share", "signup"]
        for i in range(count):
            events = []
            for j in range(events_per_doc):
                events.append({
                    "type": event_types[j % len(event_types)],
                    "amount": round(self.rng.uniform(1, 500), 2),
                    "active": j % 2 == 0,
                    "ts": f"2025-01-{self.rng.randint(1,28):02d}",
                })
            docs.append({
                "name": f"doc-{i}",
                "value": self.rng.randint(0, 10000),
                "tags": self._tags(self.rng.randint(2, 8)),
                "events": events,
            })
        return docs

    def generate_nested(self, count: int, depth: int = 3) -> list[dict]:
        """Generate docs with nested fields at specified depth for JSON path benchmarks."""
        docs = []
        for i in range(count):
            doc = {"name": f"nested-{i}", "value": self.rng.randint(0, 10000)}
            # Build nested structure: level_0.level_1.level_2...target = value
            current = doc
            for d in range(depth - 1):
                child = {}
                current[f"level_{d}"] = child
                current = child
            current["target"] = self.rng.randint(0, 10000)
            docs.append(doc)
        return docs
