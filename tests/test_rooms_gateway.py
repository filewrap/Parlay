import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from parlay.rooms.gateway import create_app, validate_init_data
from parlay.rooms.service import RoomService

TOKEN="123:secret"; ORIGIN="https://rooms.example"

def signed(uid=1,when=None,extra=None):
    values={"auth_date":str(int(time.time() if when is None else when)),"query_id":"q","user":json.dumps({"id":uid,"first_name":f"U{uid}"},separators=(",",":"))}
    if extra: values.update(extra)
    check="\n".join(f"{k}={v}" for k,v in sorted(values.items()))
    secret=hmac.new(b"WebAppData",TOKEN.encode(),hashlib.sha256).digest()
    values["hash"]=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
    return urlencode(values)

def test_official_init_data_signature_and_time_checks():
    assert validate_init_data(signed(),TOKEN)["user"]["id"]==1
    tampered=signed().replace("U1","bad")
    with pytest.raises(ValueError,match="signature"): validate_init_data(tampered,TOKEN)
    with pytest.raises(ValueError,match="expired"): validate_init_data(signed(when=time.time()-301),TOKEN)
    with pytest.raises(ValueError,match="future"): validate_init_data(signed(when=time.time()+31),TOKEN)

def test_http_auth_join_and_action_replay(tmp_path):
    async def search(q): return [{"id":"x","title":q,"source_url":"https://www.youtube.com/watch?v=x"}]
    service=RoomService(tmp_path/"r.db",search=search)
    import asyncio
    room=asyncio.run(service.create_personal(1,2))
    app=create_app(service,TOKEN,[ORIGIN],search=search); client=TestClient(app)
    auth=client.post("/api/auth",json={"init_data":signed()},headers={"origin":ORIGIN}).json()
    headers={"Authorization":f"Bearer {auth['token']}","origin":ORIGIN}
    assert client.get(f"/api/rooms/{room['id']}",headers=headers).status_code==200
    second=client.post("/api/auth",json={"init_data":signed(2)},headers={"origin":ORIGIN}).json()
    h2={"Authorization":f"Bearer {second['token']}","origin":ORIGIN}
    joined=client.post(f"/api/rooms/{room['id']}/join",json={},headers=h2).json()
    body={"action_id":"appearance-1","expected_revision":joined["revision"],"action":"appearance","payload":{"avatar":"cat"}}
    first=client.post(f"/api/rooms/{room['id']}/actions",json=body,headers=h2)
    replay=client.post(f"/api/rooms/{room['id']}/actions",json=body,headers=h2)
    assert first.status_code==200 and replay.json()==first.json()

def test_two_websocket_clients_presence_and_one_time_ticket(tmp_path):
    service=RoomService(tmp_path/"r.db"); import asyncio
    room=asyncio.run(service.create_personal(1,2)); asyncio.run(service.join(room["id"],{"id":2,"first_name":"U2"}))
    client=TestClient(create_app(service,TOKEN,[ORIGIN]))
    def session(uid):
        token=client.post("/api/auth",json={"init_data":signed(uid)},headers={"origin":ORIGIN}).json()["token"]
        return {"Authorization":f"Bearer {token}","origin":ORIGIN}
    one,two=session(1),session(2)
    t1=client.post("/api/ws-ticket",json={"room_id":room["id"]},headers=one).json()["ticket"]
    t2=client.post("/api/ws-ticket",json={"room_id":room["id"]},headers=two).json()["ticket"]
    with client.websocket_connect(f"/api/rooms/{room['id']}/ws?ticket={t1}",headers={"origin":ORIGIN}) as ws1:
      assert ws1.receive_json()["type"]=="snapshot"; ws1.receive_json()
      with client.websocket_connect(f"/api/rooms/{room['id']}/ws?ticket={t2}",headers={"origin":ORIGIN}) as ws2:
        assert ws2.receive_json()["type"]=="snapshot"
        ws2.receive_json(); assert len(ws1.receive_json()["players"])==2
        time.sleep(0.11)
        ws2.send_json({"type":"move","x":1,"z":0,"rotation":0,"seq":1})
        assert ws1.receive_json()["type"]=="presence"
    with pytest.raises(Exception):
      with client.websocket_connect(f"/api/rooms/{room['id']}/ws?ticket={t1}",headers={"origin":ORIGIN}): pass
