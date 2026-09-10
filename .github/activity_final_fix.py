from pathlib import Path

p = Path('src/parlay/activity.py')
text = p.read_text()
text = text.replace('''    _RECONCILE_SECONDS = 60.0
    _DISCOVERY_CONCURRENCY = 4
''', '''    _RECONCILE_SECONDS = 60.0
    _DISCOVERY_RETRY_SECONDS = 300.0
    _DISCOVERY_CONCURRENCY = 4
''')
text = text.replace('''        self._discovery_task: asyncio.Task[None] | None = None
        self._last_discovery_at = 0.0
''', '''        self._discovery_task: asyncio.Task[None] | None = None
        self._last_discovery_at = 0.0
        self._discovery_failed = False
''')
old = '''        await self._discover_dialogs()
        for chat_id in await self._known_active_chats():
            await self._reconcile_safely(chat_id)
        if self._started and not self._stopping:
            self._periodic_task = asyncio.create_task(
                self._periodic_reconcile(), name="parlay-activity-periodic"
            )
'''
new = '''        if self._started and not self._stopping:
            self._schedule_discovery(force=True)
            self._periodic_task = asyncio.create_task(
                self._periodic_reconcile(), name="parlay-activity-periodic"
            )
'''
assert old in text
text = text.replace(old, new)
old = '''        else:
            update_version = getattr(update.call, "version", None)
'''
new = '''        else:
            if previous is not None and previous.call_id not in (None, call_id):
                self._schedule_reconcile(chat_id)
                return
            update_version = getattr(update.call, "version", None)
'''
assert old in text
text = text.replace(old, new, 1)
old = '''        own = next((item for item in update.participants if self._is_self(item)), None)
        if own is not None and not bool(getattr(own, "versioned", False)):
            await self._apply_self(state.chat_id, own, update.version, state)
            return
'''
new = '''        own = next((item for item in update.participants if self._is_self(item)), None)
        version_triggered = any(
            bool(getattr(item, flag, False))
            for item in update.participants
            for flag in ("versioned", "left", "just_joined")
        )
        if own is not None and not version_triggered:
            await self._apply_self(state.chat_id, own, update.version, state)
            return
'''
assert old in text
text = text.replace(old, new)
old = '''    async def _discover_dialogs(self) -> None:
        await asyncio.sleep(0)
        semaphore = asyncio.Semaphore(self._DISCOVERY_CONCURRENCY)
        jobs: list[Awaitable[None]] = []
        try:
'''
new = '''    async def _discover_dialogs(self) -> None:
        await asyncio.sleep(0)
        semaphore = asyncio.Semaphore(self._DISCOVERY_CONCURRENCY)
        jobs: list[Awaitable[None]] = []
        try:
'''
assert old in text
# Keep header but replace exception tail to record success/failure.
old_tail = '''            if jobs:
                await asyncio.gather(*jobs)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("activity dialog discovery failed", exc_info=True)

    def _schedule_discovery(self) -> None:
        if self._stopping or not self._started:
            return
        now = time.monotonic()
        if now - self._last_discovery_at < 0.1:
            return
        self._last_discovery_at = now
        if self._discovery_task is not None and not self._discovery_task.done():
            return
        self._discovery_task = asyncio.create_task(
            self._discover_dialogs(), name="parlay-activity-discovery"
        )
'''
new_tail = '''            if jobs:
                await asyncio.gather(*jobs)
            self._discovery_failed = False
        except asyncio.CancelledError:
            raise
        except Exception:
            self._discovery_failed = True
            log.warning("activity dialog discovery failed", exc_info=True)

    def _schedule_discovery(self, *, force: bool = False) -> None:
        if self._stopping or not self._started:
            return
        if self._discovery_task is not None and not self._discovery_task.done():
            return
        now = time.monotonic()
        cooldown = 0.1 if not force else 0.0
        if now - self._last_discovery_at < cooldown:
            return
        self._last_discovery_at = now
        self._discovery_task = asyncio.create_task(
            self._discover_dialogs(), name="parlay-activity-discovery"
        )
'''
assert old_tail in text
text = text.replace(old_tail, new_tail)
old = '''    async def _periodic_reconcile(self) -> None:
        while True:
            await asyncio.sleep(self._RECONCILE_SECONDS)
            for chat_id in await self._known_active_chats():
                self._schedule_reconcile(chat_id)
'''
new = '''    async def _periodic_reconcile(self) -> None:
        while True:
            await asyncio.sleep(self._RECONCILE_SECONDS)
            for chat_id in await self._known_active_chats():
                self._schedule_reconcile(chat_id)
            if (
                self._discovery_failed
                and time.monotonic() - self._last_discovery_at
                >= self._DISCOVERY_RETRY_SECONDS
            ):
                self._schedule_discovery(force=True)
'''
assert old in text
text = text.replace(old, new)
p.write_text(text)

p = Path('tests/test_activity.py')
text = p.read_text()
text = text.replace('''def participant(
    *, left: bool = False, muted: bool = False, can_self_unmute: bool = True
) -> types.GroupCallParticipant:
''', '''def participant(
    *,
    left: bool = False,
    muted: bool = False,
    can_self_unmute: bool = True,
    just_joined: bool = False,
) -> types.GroupCallParticipant:
''')
text = text.replace('''        can_self_unmute=can_self_unmute,
        is_self=True,
''', '''        can_self_unmute=can_self_unmute,
        just_joined=just_joined,
        is_self=True,
''', 1)
# Existing startup-dependent tests now wait for the scheduled discovery.
text = text.replace('''    await tracker.start()
    try:
''', '''    await tracker.start()
    if tracker._discovery_task is not None:
        await tracker._discovery_task
    try:
''')
addition = '''\n\nasync def test_start_returns_before_discovery_rpc(tmp_path) -> None:
    client = FakeClient()
    setup_chat(client, 101, 11)
    gate = asyncio.Event()

    async def blocked_dialogs():
        client.dialog_iterations += 1
        await gate.wait()
        if False:
            yield None

    client.iter_dialogs = blocked_dialogs
    _seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    await asyncio.wait_for(tracker.start(), timeout=1)
    try:
        assert tracker._discovery_task is not None
        assert not tracker._discovery_task.done()
    finally:
        gate.set()
        await tracker.stop()


async def test_start_discovery_does_not_duplicate_reconcile(tmp_path) -> None:
    client = FakeClient()
    setup_chat(client, 101, 11)
    _seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    await tracker.start()
    try:
        assert tracker._discovery_task is not None
        await tracker._discovery_task
        full = sum(
            isinstance(item, functions.messages.GetFullChatRequest)
            for item in client.requests
        )
        participants = sum(
            isinstance(item, functions.phone.GetGroupParticipantsRequest)
            for item in client.requests
        )
        assert full == 1
        assert participants == 1
    finally:
        await tracker.stop()


async def test_failed_discovery_retries_only_after_retry_interval(tmp_path) -> None:
    client = FakeClient()

    async def failed_dialogs():
        client.dialog_iterations += 1
        raise RuntimeError("dialogs unavailable")
        yield

    client.iter_dialogs = failed_dialogs
    _seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    tracker._DISCOVERY_RETRY_SECONDS = 0
    tracker._RECONCILE_SECONDS = 0.01
    await tracker.start()
    try:
        assert tracker._discovery_task is not None
        await tracker._discovery_task
        assert tracker._discovery_failed is True
        await asyncio.sleep(0.03)
        assert client.dialog_iterations >= 2
    finally:
        await tracker.stop()


async def test_left_and_just_joined_use_version_rules(tmp_path) -> None:
    client = FakeClient()
    chat_id = setup_chat(client, 101, 11)
    client.participants[11] = [participant()]
    _seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    await tracker.start()
    if tracker._discovery_task is not None:
        await tracker._discovery_task
    try:
        await tracker._save(chat_id, version=5, membership="joined")
        await tracker.handle_update(
            types.UpdateGroupCallParticipants(
                call=types.InputGroupCall(id=11, access_hash=110),
                participants=[participant(left=True)],
                version=4,
            )
        )
        state = await tracker._get(chat_id)
        assert state is not None and state.membership == "joined"
        await tracker.handle_update(
            types.UpdateGroupCallParticipants(
                call=types.InputGroupCall(id=11, access_hash=110),
                participants=[participant(just_joined=True)],
                version=7,
            )
        )
        assert chat_id in tracker._reconcile_tasks
    finally:
        await tracker.stop()


async def test_old_active_call_schedules_reconcile_without_replacing(tmp_path) -> None:
    client = FakeClient()
    chat_id = setup_chat(client, 101, 11)
    _seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    await tracker.start()
    if tracker._discovery_task is not None:
        await tracker._discovery_task
    scheduled: list[int] = []
    tracker._schedule_reconcile = scheduled.append
    try:
        await tracker._save(chat_id, call_id=22, access_hash=220, version=5)
        stale = SimpleNamespace(
            call=SimpleNamespace(id=11, access_hash=110, version=6),
            peer=types.PeerChat(chat_id=101),
        )
        await tracker._handle_group_call(stale)
        state = await tracker._get(chat_id)
        assert state is not None and state.call_id == 22
        assert scheduled == [chat_id]
    finally:
        await tracker.stop()
'''
p.write_text(text + addition)
