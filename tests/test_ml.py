"""Behavior, learning, privacy, concurrency, and scale tests for Compass."""

from __future__ import annotations

import json
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest

from parlay.ml import CompassService


def service(tmp_path: Path) -> CompassService:
    return CompassService(tmp_path / "compass.db", tmp_path / "models")


def test_contract_validation_and_opt_in(tmp_path: Path) -> None:
    compass = service(tmp_path)
    compass.ingest_track("a", "Alpha", tags=["rock"])
    assert compass.recommend("u") == []
    with pytest.raises(ValueError):
        compass.set_preferences("u", True, count=11)
    with pytest.raises(ValueError):
        compass.set_preferences("u", True, quiet_start=22)
    compass.set_preferences("u", True, count=1)
    result = compass.recommend("u", 1)
    assert result[0].keys() == {"id", "title", "source_url", "score", "reason", "model_version"}
    assert result[0]["reason"] == "cold-start chart fallback"


def test_feedback_requires_exposure_and_is_idempotent(tmp_path: Path) -> None:
    compass = service(tmp_path)
    compass.ingest_track("a", "Alpha")
    compass.set_preferences("u", True)
    with pytest.raises(ValueError, match="exposure"):
        compass.feedback("u", "a", True, "feedback-0")
    compass.recommend("u")
    assert compass.feedback("u", "a", True, "feedback-1") is True
    assert compass.feedback("u", "a", True, "feedback-2") is False
    assert compass.record_event("u", "a", "play", "play-1") is True
    assert compass.record_event("u", "a", "play", "play-1") is False


def test_five_dislikes_pause_until_enable_or_reset(tmp_path: Path) -> None:
    compass = service(tmp_path)
    compass.set_preferences("u", True)
    for number in range(5):
        track = f"t{number}"
        compass.ingest_track(track, track)
        assert compass.recommend("u", 1)[0]["id"] == track
        assert compass.feedback("u", track, False, f"negative-{number}")
    assert compass.recommend("u") == []
    compass.set_preferences("u", True)
    compass.ingest_track("new", "New")
    assert compass.recommend("u", 1)


def test_bpr_learns_explicit_pairwise_preference(tmp_path: Path) -> None:
    compass = service(tmp_path)
    for track in ("liked", "disliked", "other"):
        compass.ingest_track(track, track)
    compass.set_preferences("learner", True)
    compass.record_event("learner", "liked", "play", "positive-1")
    compass.store.add_exposures("learner", ["disliked"], "seed")
    compass.feedback("learner", "disliked", False, "negative-1")
    metrics = compass.train()
    scores = compass.ranker.collaborative_scores("learner", ["liked", "disliked"])
    assert metrics["training_pairs"] == 40
    assert scores["liked"] > scores["disliked"]


def test_temporal_metrics_and_private_reloadable_artifact(tmp_path: Path) -> None:
    compass = service(tmp_path)
    for number, tag in enumerate(("rock", "rock", "jazz", "jazz")):
        compass.ingest_track(f"t{number}", f"Track {number}", tags=[tag])
    for user, liked in (("u1", ("t0", "t1")), ("u2", ("t2", "t3"))):
        compass.set_preferences(user, True)
        for number, track in enumerate(liked):
            compass.record_event(user, track, "play", f"{user}-{number}")
    metrics = compass.train()
    assert metrics["training_pairs"] > 0
    assert metrics["evaluated_users"] == 2
    assert 0 <= metrics["recall_at_10"] <= 1
    assert 0 <= metrics["ndcg_at_10"] <= 1
    pointer_path = tmp_path / "models" / "current.json"
    pointer = json.loads(pointer_path.read_text())
    artifact = tmp_path / "models" / pointer["artifact"]
    assert artifact.exists()
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert stat.S_IMODE(pointer_path.stat().st_mode) == 0o600
    reloaded = CompassService(tmp_path / "compass.db", tmp_path / "models")
    assert reloaded.ranker.version == metrics["model_version"]


def test_delete_retrains_empty_model_and_purges_user_from_artifacts(tmp_path: Path) -> None:
    compass = service(tmp_path)
    compass.ingest_track("a", "Alpha")
    compass.set_preferences("private-user", True)
    compass.record_event("private-user", "a", "play", "private-play")
    compass.train()
    assert "private-user" in compass.ranker.users
    old_artifact = next((tmp_path / "models").glob("*.npz"))

    compass.delete_user("private-user")

    assert compass.recommend("private-user") == []
    assert "private-user" not in compass.ranker.users
    artifacts = list((tmp_path / "models").glob("*.npz"))
    assert len(artifacts) == 1
    assert old_artifact not in artifacts
    with np.load(artifacts[0], allow_pickle=False) as model:
        assert "private-user" not in model["users"].tolist()


def test_model_operations_are_serialized_across_worker_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compass = service(tmp_path)
    for number in range(20):
        compass.ingest_track(f"t{number}", f"Track {number}")
    compass.set_preferences("u", True)
    compass.record_event("u", "t0", "play", "play")
    compass.train()
    active = 0
    maximum = 0
    original = compass.ranker.collaborative_scores

    def guarded_scores(user_id: str, item_ids: list[str]) -> dict[str, float]:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            return original(user_id, item_ids)
        finally:
            active -= 1

    monkeypatch.setattr(compass.ranker, "collaborative_scores", guarded_scores)
    with ThreadPoolExecutor(max_workers=4) as workers:
        futures = [workers.submit(compass.recommend, "u", 1) for _ in range(3)]
        futures.append(workers.submit(compass.reset_user, "u"))
        for future in futures:
            future.result()
    assert maximum == 1


def test_failed_publish_keeps_previous_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compass = service(tmp_path)
    compass.ingest_track("a", "Alpha")
    compass.set_preferences("u", True)
    compass.record_event("u", "a", "play", "play")
    first = compass.train()
    pointer_path = tmp_path / "models" / "current.json"
    previous_pointer = pointer_path.read_text()
    previous_version = compass.ranker.version

    def fail_replace(source: object, destination: object) -> None:
        if str(destination).endswith("current.json"):
            raise OSError("simulated pointer failure")
        original_replace(source, destination)

    original_replace = __import__("os").replace
    monkeypatch.setattr("parlay.ml.model.os.replace", fail_replace)
    with pytest.raises(OSError, match="pointer failure"):
        compass.train()
    assert pointer_path.read_text() == previous_pointer
    assert compass.ranker.version == previous_version == first["model_version"]


def test_ten_thousand_catalog_mechanics_not_quality_claim(tmp_path: Path) -> None:
    compass = service(tmp_path)
    compass.set_preferences("synthetic", True)
    for number in range(10_000):
        compass.ingest_track(
            f"item-{number}", f"Synthetic {number}", tags=[f"bucket-{number % 20}"]
        )
    result = compass.recommend("synthetic", 10)
    assert len(result) == 10
    assert len({item["id"] for item in result}) == 10
    second = compass.recommend("synthetic", 10)
    assert not {item["id"] for item in result} & {item["id"] for item in second}
