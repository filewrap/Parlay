from pathlib import Path

p = Path('src/parlay/activity.py')
text = p.read_text()
old = '''        previous = await self._get(chat_id)

        def value(field: str, supplied: Any, default: Any) -> Any:
            if supplied is not _UNSET:
                return supplied
            return getattr(previous, field) if previous is not None else default

        values = (
            self._account_id,
            chat_id,
            value("call_id", call_id, None),
            value("access_hash", access_hash, None),
            value("call_state", call_state, "unknown"),
            value("membership", membership, "unknown"),
            int(bool(value("transport", transport, False))),
            value("version", version, None),
            value("unavailable_reason", unavailable_reason, None),
            int(time.time()),
        )
        await self._execute(
            """
            INSERT INTO activity (
                account_id, chat_id, call_id, access_hash, call_state, membership,
                transport, version, unavailable_reason, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, chat_id) DO UPDATE SET
                call_id=excluded.call_id,
                access_hash=excluded.access_hash,
                call_state=excluded.call_state,
                membership=excluded.membership,
                transport=excluded.transport,
                version=excluded.version,
                unavailable_reason=excluded.unavailable_reason,
                updated_at=excluded.updated_at
            """,
            values,
        )
'''
new = '''        def operation(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                """
                SELECT chat_id, call_id, access_hash, call_state, membership,
                       transport, version, unavailable_reason
                FROM activity WHERE account_id=? AND chat_id=?
                """,
                (self._account_id, chat_id),
            ).fetchone()
            previous = self._row(row)

            def value(field: str, supplied: Any, default: Any) -> Any:
                if supplied is not _UNSET:
                    return supplied
                return getattr(previous, field) if previous is not None else default

            values = (
                self._account_id,
                chat_id,
                value("call_id", call_id, None),
                value("access_hash", access_hash, None),
                value("call_state", call_state, "unknown"),
                value("membership", membership, "unknown"),
                int(bool(value("transport", transport, False))),
                value("version", version, None),
                value("unavailable_reason", unavailable_reason, None),
                int(time.time()),
            )
            with connection:
                connection.execute(
                    """
                    INSERT INTO activity (
                        account_id, chat_id, call_id, access_hash, call_state, membership,
                        transport, version, unavailable_reason, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, chat_id) DO UPDATE SET
                        call_id=excluded.call_id,
                        access_hash=excluded.access_hash,
                        call_state=excluded.call_state,
                        membership=excluded.membership,
                        transport=excluded.transport,
                        version=excluded.version,
                        unavailable_reason=excluded.unavailable_reason,
                        updated_at=excluded.updated_at
                    """,
                    values,
                )

        await self._db(operation)
'''
assert old in text
text = text.replace(old, new)
text = text.replace('''        if isinstance(update.call, types.GroupCallDiscarded):
            await self._save(
''', '''        if isinstance(update.call, types.GroupCallDiscarded):
            if previous is not None and previous.call_id not in (None, call_id):
                return
            await self._save(
''', 1)
old = '''        else:
            await self._save(
                chat_id,
                call_id=call_id,
                access_hash=getattr(update.call, "access_hash", None),
                call_state="active",
                version=getattr(update.call, "version", None),
            )
'''
new = '''        else:
            update_version = getattr(update.call, "version", None)
            if (
                previous is not None
                and previous.call_id == call_id
                and previous.version is not None
                and update_version is not None
                and update_version < previous.version
            ):
                return
            call_changed = previous is not None and previous.call_id not in (None, call_id)
            await self._save(
                chat_id,
                call_id=call_id,
                access_hash=getattr(update.call, "access_hash", None),
                call_state="active",
                membership="unknown" if call_changed else _UNSET,
                transport=False if call_changed else _UNSET,
                unavailable_reason=None if call_changed else _UNSET,
                version=update_version,
            )
'''
assert old in text
text = text.replace(old, new)
old = '''        if state.version is not None and update.version != state.version + 1:
            self._schedule_reconcile(state.chat_id)
            return
        own = next((item for item in update.participants if self._is_self(item)), None)
        if own is None:
            await self._save(state.chat_id, version=update.version)
            return
        await self._apply_self(state.chat_id, own, update.version, state)
'''
new = '''        own = next((item for item in update.participants if self._is_self(item)), None)
        if own is not None and not bool(getattr(own, "versioned", False)):
            await self._apply_self(state.chat_id, own, update.version, state)
            return
        if state.version is None:
            self._schedule_reconcile(state.chat_id)
            return
        if update.version < state.version:
            return
        if update.version == state.version:
            if own is not None:
                self._schedule_reconcile(state.chat_id)
            return
        if update.version > state.version + 1:
            self._schedule_reconcile(state.chat_id)
            return
        if own is None:
            await self._save(state.chat_id, version=update.version)
            return
        await self._apply_self(state.chat_id, own, update.version, state)
'''
assert old in text
text = text.replace(old, new)
old = '''        participant = getattr(update, "new_participant", None)
        removed = participant is None or isinstance(participant, types.ChannelParticipantBanned)
'''
new = '''        participant = getattr(update, "new_participant", None)
        banned_and_removed = isinstance(
            participant, types.ChannelParticipantBanned
        ) and (
            bool(getattr(participant, "left", False))
            or bool(getattr(participant.banned_rights, "view_messages", False))
        )
        removed = (
            participant is None
            or isinstance(participant, types.ChannelParticipantLeft)
            or banned_and_removed
        )
'''
assert old in text
p.write_text(text.replace(old, new))

p = Path('tests/test_activity.py')
text = p.read_text()
addition = '''\n\nasync def test_concurrent_transport_and_participant_save_merge_fields(tmp_path) -> None:
    client = FakeClient()
    chat_id = setup_chat(client, 101, 11)
    _seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    await tracker.start()
    try:
        previous = await tracker._get(chat_id)
        await asyncio.gather(
            tracker.set_transport(chat_id, True),
            tracker._apply_self(chat_id, participant(), 2, previous),
        )
        state = await tracker._get(chat_id)
        assert state is not None
        assert state.transport is True
        assert state.membership == "joined"
        assert state.version == 2
    finally:
        await tracker.stop()


async def test_send_only_restriction_is_not_membership_removal(tmp_path) -> None:
    client = FakeClient()
    _seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    await tracker.start()
    chat_id = get_peer_id(types.PeerChannel(101))
    scheduled: list[int] = []
    tracker._schedule_reconcile = scheduled.append
    try:
        await tracker._save(chat_id, call_state="active", membership="joined", transport=True)
        restricted = types.ChannelParticipantBanned(
            peer=types.PeerUser(7), kicked_by=1, date=None,
            banned_rights=types.ChatBannedRights(until_date=None, send_messages=True),
        )
        update = SimpleNamespace(channel_id=101, user_id=7, new_participant=restricted)
        await tracker._handle_channel_participant(update)
        state = await tracker._get(chat_id)
        assert state is not None
        assert state.membership == "joined"
        assert state.transport is True
        assert scheduled == [chat_id]
    finally:
        await tracker.stop()


async def test_stale_discarded_call_does_not_overwrite_new_call(tmp_path) -> None:
    client = FakeClient()
    chat_id = setup_chat(client, 101, 11)
    client.participants[11] = [participant()]
    seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    await tracker.start()
    try:
        await tracker._save(chat_id, call_id=22, access_hash=220, call_state="active", membership="joined", transport=True, version=5)
        stale = types.UpdateGroupCall(
            call=types.GroupCallDiscarded(id=11, access_hash=110, duration=8),
            peer=types.PeerChat(chat_id=101),
        )
        await tracker.handle_update(stale)
        state = await tracker._get(chat_id)
        assert state is not None
        assert state.call_id == 22
        assert state.call_state == "active"
        assert state.membership == "joined"
        assert state.transport is True
        assert seen == []
    finally:
        await tracker.stop()


async def test_stale_group_call_version_does_not_regress_state(tmp_path) -> None:
    client = FakeClient()
    chat_id = setup_chat(client, 101, 11)
    _seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    await tracker.start()
    try:
        await tracker._save(chat_id, version=5)
        stale = SimpleNamespace(call=SimpleNamespace(id=11, access_hash=110, version=4), peer=types.PeerChat(chat_id=101))
        await tracker._handle_group_call(stale)
        state = await tracker._get(chat_id)
        assert state is not None
        assert state.version == 5
    finally:
        await tracker.stop()
'''
p.write_text(text + addition)
