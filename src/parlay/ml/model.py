"""NumPy pairwise matrix factorization with a metadata-content ranking term."""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any

import numpy as np

POSITIVE = {"play", "listen", "like", "positive", "click"}
NEGATIVE = {"dislike", "negative", "skip"}


class HybridRanker:
    """BPR embeddings combined with a learned-history tag profile."""

    def __init__(self, model_dir: str | Path, dimensions: int = 16) -> None:
        self.directory = Path(model_dir)
        self.dimensions = dimensions
        self.version = "cold-start"
        self.users: list[str] = []
        self.items: list[str] = []
        self.user_factors = np.empty((0, dimensions))
        self.item_factors = np.empty((0, dimensions))
        self._load()

    def _load(self) -> None:
        pointer = self.directory / "current.json"
        if not pointer.exists():
            return
        try:
            metadata = json.loads(pointer.read_text(encoding="utf-8"))
            data = np.load(self.directory / metadata["artifact"], allow_pickle=False)
            self.users = data["users"].tolist()
            self.items = data["items"].tolist()
            self.user_factors = data["user_factors"]
            self.item_factors = data["item_factors"]
            self.version = metadata["version"]
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return

    def train(self, tracks: list[Any], events: list[Any]) -> dict[str, Any]:
        started = time.perf_counter()
        item_ids = [row["track_id"] for row in tracks]
        positives: dict[str, list[tuple[str, float]]] = {}
        negatives: dict[str, set[str]] = {}
        for event in events:
            kind = event["event_type"]
            if kind in POSITIVE:
                positives.setdefault(event["user_id"], []).append(
                    (event["track_id"], float(event["created_at"]))
                )
            elif kind in NEGATIVE:
                negatives.setdefault(event["user_id"], set()).add(event["track_id"])
        users = sorted(positives)
        item_index = {item: n for n, item in enumerate(item_ids)}
        user_index = {user: n for n, user in enumerate(users)}
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
            train_positive[user] = {item for item, _ in ordered}
        loss = 0.0
        steps = 0
        learning_rate = 0.04
        regularization = 0.01
        for _epoch in range(25):
            triples: list[tuple[str, str, str]] = []
            for user, seen in train_positive.items():
                available = [
                    item for item in item_ids if item not in seen and item != held_out.get(user)
                ]
                if not seen or not available:
                    continue
                explicit = list(negatives.get(user, set()) & set(available))
                for positive in seen:
                    negative = (
                        explicit[steps % len(explicit)]
                        if explicit
                        else available[int(rng.integers(len(available)))]
                    )
                    triples.append((user, positive, negative))
            rng.shuffle(triples)
            for user, positive, negative in triples:
                u = user_index[user]
                i, j = item_index[positive], item_index[negative]
                user_vector = user_factors[u].copy()
                pos_vector = item_factors[i].copy()
                neg_vector = item_factors[j].copy()
                margin = float(user_vector @ (pos_vector - neg_vector))
                gradient = 1.0 / (1.0 + math.exp(max(-30.0, min(30.0, margin))))
                user_factors[u] += learning_rate * (
                    gradient * (pos_vector - neg_vector) - regularization * user_vector
                )
                item_factors[i] += learning_rate * (
                    gradient * user_vector - regularization * pos_vector
                )
                item_factors[j] += learning_rate * (
                    -gradient * user_vector - regularization * neg_vector
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
        version = f"bpr-{int(time.time() * 1000)}"
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
        uf: np.ndarray,
        itf: np.ndarray,
        ui: dict[str, int],
        ii: dict[str, int],
        seen: dict[str, set[str]],
        held_out: dict[str, str],
        tracks: list[Any],
    ) -> dict[str, float]:
        recalls: list[float] = []
        ndcgs: list[float] = []
        recommendation_tags: set[str] = set()
        all_tags: set[str] = set()
        tags = {row["track_id"]: set(json.loads(row["tags_json"])) for row in tracks}
        for values in tags.values():
            all_tags.update(values)
        for user, target in held_out.items():
            candidates = [item for item in items if item not in seen[user]]
            ranked = sorted(
                candidates, key=lambda item: float(uf[ui[user]] @ itf[ii[item]]), reverse=True
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
        uf: np.ndarray,
        itf: np.ndarray,
        metrics: dict[str, Any],
    ) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        artifact = f"{version}.npz"
        temporary_artifact = self.directory / f".{artifact}.tmp"
        with temporary_artifact.open("wb") as stream:
            np.savez_compressed(
                stream,
                users=np.asarray(users, dtype=str),
                items=np.asarray(items, dtype=str),
                user_factors=uf,
                item_factors=itf,
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_artifact, self.directory / artifact)
        pointer = {"version": version, "artifact": artifact, "metrics": metrics}
        temporary_pointer = self.directory / ".current.json.tmp"
        temporary_pointer.write_text(json.dumps(pointer, sort_keys=True), encoding="utf-8")
        os.replace(temporary_pointer, self.directory / "current.json")

    def collaborative_scores(self, user_id: str, item_ids: list[str]) -> dict[str, float]:
        if user_id not in self.users:
            return {}
        user = self.user_factors[self.users.index(user_id)]
        return {
            item: float(user @ self.item_factors[self.items.index(item)])
            for item in item_ids
            if item in self.items
        }
