"""Focused behavior and mechanics tests for the isolated Compass subsystem."""

from __future__ import annotations

import json

import pytest

from parlay.ml import CompassService


def service(tmp_path):
    return CompassService(tmp_path / "compass.db", tmp_path / "models")


def test_contract_validation_and_opt_in(tmp_path) -> None:
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


def test_feedback_requires_exposure_and_is_idempotent(tmp_path) -> None:
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


def test_five_dislikes_pause_until_enable_or_reset(tmp_path) -> None:
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


def test_real_bpr_training_publishes_reloadable_artifact(tmp_path) -> None:
    compass = service(tmp_path)
    for number, tag in enumerate(("rock", "rock", "jazz", "jazz")):
        compass.ingest_track(f"t{number}", f"Track {number}", tags=[tag])
    for user, liked in (("u1", ("t0", "t1")), ("u2", ("t2", "t3"))):
        compass.set_preferences(user, True)
        for number, track in enumerate(liked):
            compass.record_event(user, track, "play", f"{user}-{number}")
    metrics = compass.train()
    assert metrics["training_pairs"] > 0
    assert 0 <= metrics["recall_at_10"] <= 1
    assert 0 <= metrics["ndcg_at_10"] <= 1
    pointer = json.loads((tmp_path / "models" / "current.json").read_text())
    assert (tmp_path / "models" / pointer["artifact"]).exists()
    reloaded = CompassService(tmp_path / "compass.db", tmp_path / "models")
    assert reloaded.ranker.version == metrics["model_version"]


def test_reset_and_privacy_deletion(tmp_path) -> None:
    compass = service(tmp_path)
    compass.ingest_track("a", "Alpha")
    compass.set_preferences("u", True)
    compass.recommend("u")
    compass.reset_user("u")
    assert compass.recommend("u")
    compass.delete_user("u")
    assert compass.recommend("u") == []


def test_ten_thousand_catalog_mechanics_not_quality_claim(tmp_path) -> None:
    compass = service(tmp_path)
    compass.set_preferences("synthetic", True)
    for number in range(10_000):
        compass.ingest_track(f"item-{number}", f"Synthetic {number}", tags=[f"bucket-{number % 20}"])
    result = compass.recommend("synthetic", 10)
    assert len(result) == 10
    assert len({item["id"] for item in result}) == 10
    assert compass.recommend("synthetic", 10)[0]["id"] not in {item["id"] for item in result}
