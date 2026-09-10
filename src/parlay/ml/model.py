"""NumPy pairwise matrix factorization with metadata-assisted ranking."""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

POSITIVE = {"play", "listen", "like", "positive", "click"}
NEGATIVE = {"dislike", "negative", "skip"}
FloatArray = npt.NDArray[np.float64]


class HybridRanker:
    """BPR embeddings combined with a learned-history tag profile."""

    def __init__(self, model_dir: str | Path, dimensions: int = 16) -> None:
        self.directory = Path(model_dir)
        self.dimensions = dimensions
        self.version = "cold-start"
        self.users: list[str] = []
        self.items: list[str] = []
        self.user_factors: FloatArray = np.empty((0, dimensions), dtype=np.float64)
        self.item_factors: FloatArray = np.empty((0, dimensions), dtype=np.float64)
        self._load()

    def _load(self) -> None:
        pointer = self.directory / "current.json"
        if not pointer.exists():
            return
        try:
            metadata = json.loads(pointer.read_text(encoding="utf-8"))
            with np.load(self.directory / metadata["artifact"], allow_pickle=False) as data:
                users = [str(value) for value in data["users"].tolist()]
                items = [str(value) for value in data["items"].tolist()]
                user_factors = np.asarray(data["user_factors"], dtype=np.float64)
                item_factors = np.asarray(data["item_factors"], dtype=np.float64)
            if user_factors.shape != (len(users), self.dimensions):
                raise ValueError("invalid user factor shape")
            if item_factors.shape != (len(items), self.dimensions):
                raise ValueError("invalid item factor shape")
            self.users, self.items = users, items
            self.user_factors, self.item_factors = user_factors, item_factors
            self.version = str(metadata["version"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return

    def train(self, tracks: list[Any], events: list[Any]) -> dict[str, Any]:
        """Fit regularized BPR factors and publish one immutable snapshot."""
        started = time.perf_counter()
        item_ids = [str(row["track_id"]) for row in tracks]
        positives: dict[str, list[tuple[str, float]]] = {}
        negatives: dict[str, set[str]] = {}
        for event in events:
            kind = str(event["event_type"])
            user_id = str(event["user_id"])
            track_id = str(event["track_id"])
            if kind in POSITIVE:
                positives.setdefault(user_id, []).append((track_id, float(event["created_at"])))
            elif kind in NEGATIVE:
                negatives.setdefault(user_id, set()).add(track_id)

        users = sorted(positives)
        item_index = {item: number for number, item in enumerate(item_ids)}
        user_index = {user: number for number, user in enumerate(users)}
        rng = np.random.default_rng(42)
        user_factors = rng.normal(0, 0.08, (len(users), self.dimensions))
        item_factors = rng.normal(0, 0.08, (len(item_ids), self.dimensions))
        held_out: dict[str, str] = {}
        train_positive: dict[str, set[str]] = {}
        for user, history in positives.items():
            ordered = sorted(history, key=lambda pair: pair[1])
            if len(ordered) >= 2:
                held_out[user] = ordered[-1][0]
                ordered = ordered[:-1]
            train_positive[user] = {item for item, _created_at in ordered}

        loss = 0.0
        steps = 0
        learning_rate = 0.04
        regularization = 0.01
        for _epoch in range(40):
            triples: list[tuple[str, str, str]] = []
            for user, seen in train_positive.items():
                available = [
                    item for item in item_ids if item not in seen and item != held_out.get(user)
                ]
                if not seen or not available:
                    continue
                explicit = sorted(negatives.get(user, set()) & set(available))
                for positive in sorted(seen):
                    negative = (
                        explicit[steps % len(explicit)]
                        if explicit
                        else available[int(rng.integers(len(available)))]
                    )
                    triples.append((user, positive, negative))
            rng.shuffle(triples)
            for user, positive, negative in triples:
                user_number = user_index[user]
                positive_number = item_index[positive]
                negative_number = item_index[negative]
                user_vector = user_factors[user_number].copy()
                positive_vector = item_factors[positive_number].copy()
                negative_vector = item_factors[negative_number].copy()
                margin = float(user_vector @ (positive_vector - negative_vector))
                gradient = 1.0 / (1.0 + math.exp(max(-30.0, min(30.0, margin))))
                user_factors[user_number] += learning_rate * (
                    gradient * (positive_vector - negative_vector) - regularization * user_vector
                )
                item_factors[positive_number] += learning_rate * (
                    gradient * user_vector - regularization * positive_vector
                )
                item_factors[negative_number] += learning_rate * (
                    -gradient * user_vector - regularization * negative_vector
                )
                loss += math.log1p(math.exp(-margin))
                steps += 1

        metrics = self._evaluate(
            users,
            item_ids,
            user_factors,
            item_factors,
            user_index,
            item_index,
            train_positive,
            held_out,
            tracks,
        )
        metrics.update({"training_pairs": steps, "bpr_loss": loss / max(1, steps)})
        version = f"bpr-{time.time_ns()}"
        self._publish(version, users, item_ids, user_factors, item_factors, metrics)
        self.users, self.items = users, item_ids
        self.user_factors, self.item_factors = user_factors, item_factors
        self.version = version
        metrics.update(
            {"model_version": version, "duration_seconds": time.perf_counter() - started}
        )
        return metrics

    def _evaluate(
        self,
        users: list[str],
        items: list[str],
        user_factors: FloatArray,
        item_factors: FloatArray,
        user_index: dict[str, int],
        item_index: dict[str, int],
        seen: dict[str, set[str]],
        held_out: dict[str, str],
        tracks: list[Any],
    ) -> dict[str, float]:
        recalls: list[float] = []
        ndcgs: list[float] = []
        recommendation_tags: set[str] = set()
        all_tags: set[str] = set()
        tags = {str(row["track_id"]): set(json.loads(row["tags_json"])) for row in tracks}
        for values in tags.values():
            all_tags.update(values)
        for user, target in held_out.items():
            candidates = [item for item in items if item not in seen[user]]
            ranked = sorted(
                candidates,
                key=lambda item: float(
                    user_factors[user_index[user]] @ item_factors[item_index[item]]
                ),
                reverse=True,
            )[:10]
            if target in ranked:
                rank = ranked.index(target) + 1
                recalls.append(1.0)
                ndcgs.append(1.0 / math.log2(rank + 1))
            else:
                recalls.append(0.0)
                ndcgs.append(0.0)
            for item in ranked:
                recommendation_tags.update(tags.get(item, set()))
        return {
            "recall_at_10": float(np.mean(recalls)) if recalls else 0.0,
            "ndcg_at_10": float(np.mean(ndcgs)) if ndcgs else 0.0,
            "tag_diversity": len(recommendation_tags) / max(1, len(all_tags)),
            "evaluated_users": float(len(recalls)),
        }

    def _publish(
        self,
        version: str,
        users: list[str],
        items: list[str],
        user_factors: FloatArray,
        item_factors: FloatArray,
        metrics: dict[str, Any],
    ) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        artifact = f"{version}.npz"
        temporary_artifact = self.directory / f".{artifact}.tmp"
        final_artifact = self.directory / artifact
        with temporary_artifact.open("wb") as stream:
            np.savez_compressed(
                stream,
                users=np.asarray(users, dtype=str),
                items=np.asarray(items, dtype=str),
                user_factors=user_factors,
                item_factors=item_factors,
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_artifact, 0o600)
        os.replace(temporary_artifact, final_artifact)
        pointer = {"version": version, "artifact": artifact, "metrics": metrics}
        temporary_pointer = self.directory / ".current.json.tmp"
        with temporary_pointer.open("w", encoding="utf-8") as stream:
            json.dump(pointer, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_pointer, 0o600)
        os.replace(temporary_pointer, self.directory / "current.json")
        for old_artifact in self.directory.glob("*.npz"):
            if old_artifact != final_artifact:
                old_artifact.unlink(missing_ok=True)

    def collaborative_scores(self, user_id: str, item_ids: list[str]) -> dict[str, float]:
        """Return a stable score snapshot from the currently published in-memory model."""
        try:
            user_number = self.users.index(user_id)
        except ValueError:
            return {}
        user_vector = self.user_factors[user_number]
        item_index = {item: number for number, item in enumerate(self.items)}
        return {
            item: float(user_vector @ self.item_factors[item_index[item]])
            for item in item_ids
            if item in item_index
        }
