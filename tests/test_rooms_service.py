import asyncio
import time

import pytest

from parlay.rooms.service import RoomError, RoomService

TRACK={"id":"x","title":"Track","source_url":"https://www.youtube.com/watch?v=abc","youtube_id":"abc","duration":60}

async def search(query): return [{**TRACK,"title":query}]

def user(uid): return {"id":uid,"first_name":f"U{uid}"}

@pytest.mark.asyncio
async def test_capacity_password_kick_reentry_and_replay(tmp_path):
    notices=[]
    service=RoomService(tmp_path/"rooms.db",search=search,on_reentry=lambda *x:notices.append(x))
    room=await service.create_personal(1,2)
    room=await service.action(room["id"],1,"settings",room["revision"],"settings",{"password":"secret"})
    with pytest.raises(RoomError,match="password"): await service.join(room["id"],user(2),"bad")
    room=await service.join(room["id"],user(2),"secret")
    with pytest.raises(RoomError,match="capacity"): await service.join(room["id"],user(3),"secret")
    room=await service.action(room["id"],1,"kick",room["revision"],"kick",{"user_id":2})
    with pytest.raises(RoomError,match="approve"): await service.join(room["id"],user(2),"secret")
    # Removed users can submit only the durable re-entry request through the action contract.
    with service._connect() as db:
        data=service._data(db.execute("SELECT * FROM rooms WHERE id=?",(room["id"],)).fetchone())
        data["members"].append({"user_id":2,"first_name":"U2","role":"participant"})
        db.execute("UPDATE rooms SET data_json=? WHERE id=?",(__import__('json').dumps(data),room["id"]))
    room=await service.action(room["id"],2,"request",room["revision"],"request_reentry",{})
    assert notices and room["pending_reentry"]==[]
    owner=await service.snapshot(room["id"],1)
    owner=await service.action(room["id"],1,"approve",owner["revision"],"approve_reentry",{"user_id":2})
    await service.join(room["id"],user(2),"secret")
    played=await service.action(room["id"],1,"play",owner["revision"],"force_play",{"query":"Hello"})
    replay=await service.action(room["id"],1,"play",owner["revision"],"force_play",{"query":"Hello"})
    assert replay==played
    with pytest.raises(RoomError,match="already used"): await service.action(room["id"],1,"play",owner["revision"],"force_play",{"query":"Other"})

@pytest.mark.asyncio
async def test_expiry_restart_and_room_isolation(tmp_path):
    path=tmp_path/"rooms.db"; service=RoomService(path,search=search)
    first=await service.create_personal(1,duration=300); second=await service.create_personal(2,duration=300)
    changed=await service.action(first["id"],1,"a",first["revision"],"force_play",{"query":"One"})
    assert (await service.snapshot(second["id"],2))["playback"]["track"] is None
    with service._connect() as db: db.execute("UPDATE rooms SET expires_at=? WHERE id=?",(time.time()-1,first["id"]))
    restarted=RoomService(path,search=search); await restarted.start()
    with pytest.raises(RoomError,match="expired"): await restarted.snapshot(first["id"],1)
    assert (await restarted.snapshot(second["id"],2))["state"]=="active"
    await restarted.stop()

@pytest.mark.asyncio
async def test_group_membership_authority_and_isolation(tmp_path):
    authority=lambda uid,chat: uid==9
    member=lambda uid,chat: uid in {9,10}
    service=RoomService(tmp_path/"rooms.db",authority=authority,member=member,search=search)
    a=await service.ensure_group(100,1,9); b=await service.ensure_group(200,1,9)
    a=await service.join(a["id"],user(10));
    with pytest.raises(RoomError,match="authority"): await service.action(a["id"],10,"close",a["revision"],"close",{})
    await service.publish_playback(100,{"track":TRACK,"status":"playing","position_seconds":0,"queue":[]})
    assert (await service.snapshot(b["id"],9))["playback"]["track"] is None
