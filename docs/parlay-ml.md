# Parlay Compass ML subsystem

`parlay.ml` is an isolated, opt-in recommendation service. It stores only caller-supplied Parlay events and metadata returned by the official YouTube Data API. It does not use Spotify, record audio, inspect voice calls, or ship a dataset.

## Public contract

```python
from parlay.ml import CompassService

compass = CompassService(db_path, model_dir, youtube_api_key=None)
await compass.start(deliver=None)
await compass.stop()
compass.set_preferences(
    user_id, enabled, count=10, quiet_start=None, quiet_end=None, timezone="UTC"
)
compass.ingest_track(track_id, title, artist="", source_url="", tags=None)
compass.record_event(user_id, track_id, event_type, event_id, context=None)
items = compass.recommend(user_id, limit=10)
compass.feedback(user_id, track_id, positive, event_id)
metrics = compass.train()
compass.reset_user(user_id)
compass.delete_user(user_id)
```

Arguments use non-empty string identifiers. `count` and `limit` are integers from 1 through 10. Quiet hours must be supplied as a pair of integer hours from 0 through 23. They use an IANA timezone. Equal quiet-hour endpoints mean quiet all day. `tags` is an optional list of strings. `context` is an optional dictionary and must not contain data the caller is not authorized to retain.

`recommend` returns dictionaries with exactly `id`, `title`, `source_url`, `score`, `reason`, and `model_version`. It records an exposure for each returned item. Repeats and disliked items are then suppressed. Personalization and delivery are disabled unless the user opts in. Five distinct, exposure-bound dislikes pause the user. Calling `set_preferences(..., enabled=True)` after a pause or calling `reset_user` clears the pause.

Feedback types `positive`, `like`, `click`, `dislike`, and `negative` must bind to a recommendation exposure. One exposure accepts one feedback event. Duplicate `event_id` values and repeat feedback do not inflate training data. Playback events such as `play` and `listen` can be recorded without an exposure because they describe Parlay-native listening.

The optional delivery callback receives `(user_id, text, items)`. It can be synchronous or asynchronous. Return `False` or `"blocked"` when a private delivery is blocked. Compass then disables that user's delivery. The integration owns message transport and consent presentation.

## Learning and ranking

Training uses NumPy matrix factorization with the Bayesian Personalized Ranking pairwise logistic objective, SGD, L2 regularization, deterministic uniform sampling of unobserved items, and explicit dislikes as preferred negatives. The latest positive event per eligible user is held out by timestamp. Metrics include Recall@10, NDCG@10, metadata-tag diversity, pair count, and mean BPR loss. A metadata Jaccard term from positively observed tracks makes ranking hybrid. When no learned user vector exists, the reason is explicitly labeled `cold-start chart fallback` or `cold-start metadata fit`.

`train()` is synchronous. The hourly scheduler runs it with `asyncio.to_thread`, so training does not block the event loop. Direct async integrations must do the same. One `CompassService` instance serializes recommendation, reset, deletion, model loading, training, publication, and in-memory replacement with a reentrant process lock. Run only one service instance for each database and model directory. The lock is not a cross-process file lock.

Publication first writes and fsyncs a versioned `.npz`, then atomically replaces `current.json`. A failed pointer replacement leaves the previous pointer and in-memory model usable. A successful publication deletes superseded `.npz` files. Model files contain pseudonymous user identifiers and learned vectors. They are private user data. The model directory is mode `0700`, and current model files are mode `0600`. Keep the directory on operator-private storage and do not publish or upload its contents.

`delete_user` removes preferences, events, and exposures while holding the model lock, then retrains before it returns. This removes the user from the live ranker and current artifact, even when that was the only trained user. Superseded artifacts are deleted after the new pointer is published. `reset_user` clears pause and exposure suppression but intentionally preserves events and the learned vector. Deletion can raise if private artifact publication fails. The operator must retry it before treating erasure as complete.

## Candidate discovery and scheduling

When an API key is present, Compass calls the official YouTube Data API v3 `videos.list` endpoint with `chart=mostPopular`, music category `10`, region `US`, and pages of at most 50. It uses `urllib.request` in a worker thread and collects at most 100 unique videos per run. Stored provenance identifies the API source and states that playback and reuse remain subject to YouTube and rightsholder terms. No key means discovery is a no-op.

Discovery, training, and delivery run at most once per UTC hour. SQLite job claims survive restarts. The scheduler checks the current hour and does not replay missed hours, which prevents backlog spam. Quiet hours are checked in the user's timezone.

## Limits and operations

The model is useful for small implicit-feedback workloads, but sparse or changing tastes can produce weak rankings. Uniform unobserved sampling can select unknown positives. Chart position is only a small fallback signal. Tags are unverified source metadata and are not audio features. Offline temporal metrics do not establish live quality, fairness, safety, or causal lift. The 10,000-item test is synthetic and verifies catalog serving mechanics only. A separate learned test verifies that BPR scores an observed positive above an explicit negative. It is a mechanics assertion, not a production quality claim.

YouTube API availability, quotas, metadata rights, and rightsholder terms remain operational and legal constraints. Do not treat metadata access as a license to download, train on, or redistribute media. Operators must review applicable terms before production use.

Run focused checks with:

```bash
pytest -q tests/test_ml.py
ruff check src/parlay/ml tests/test_ml.py
ruff format --check src/parlay/ml tests/test_ml.py docs/parlay-ml.md
mypy src/parlay/ml
```
