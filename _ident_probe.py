"""預驗證：Starlette TestClient 同步模式下，BackgroundTasks 的 sync 函式
是否真的跑在與主測試 thread 不同的 ident？"""
import os, sys, json, base64, hmac, hashlib, threading, uuid
os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")
sys.path.insert(0, "src")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient
from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db
from saas_mvp.line_client import FakeLineReplyClient, get_line_client
from saas_mvp.translation import StubTranslator, get_translator

_engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
Base.metadata.create_all(bind=_engine)

app = create_app()
def _override_db():
    db = _Session()
    try: yield db
    finally: db.close()
app.dependency_overrides[get_db] = _override_db
app.dependency_overrides[get_translator] = lambda: StubTranslator()
fake = FakeLineReplyClient()
app.dependency_overrides[get_line_client] = lambda: fake

CHAN = "test-channel-secret-32-bytes-x!!"
TOKEN = "test-access-token-abc"

main_tid = threading.get_ident()
print(f"[probe] main thread ident = {main_tid}")

captured = {}
class _Probe(StubTranslator):
    def translate(self, text, target_lang):
        captured["ident"] = threading.get_ident()
        captured["main_tid"] = main_tid
        return f"[{target_lang.upper()}] {text}"

app.dependency_overrides[get_translator] = lambda: _Probe()

with TestClient(app) as c:
    r = c.post("/auth/register", json={"email": f"p_{uuid.uuid4().hex[:6]}@x.com", "password": "Test1234!", "tenant_name": f"pt_{uuid.uuid4().hex[:6]}"})
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]
    me = c.get("/tenants/me", headers={"Authorization": f"Bearer {token}"})
    tid = me.json()["id"]
    from saas_mvp.auth.security import decode_access_token
    from saas_mvp.models.user import User
    p = decode_access_token(token)
    db = _Session()
    try:
        u = db.get(User, int(p["sub"]))
        u.is_admin = True
        db.commit()
    finally:
        db.close()
    c.put(f"/admin/line-configs/{tid}", headers={"Authorization": f"Bearer {token}"},
          json={"channel_secret": CHAN, "access_token": TOKEN, "default_target_lang": "zh-TW"})

    body_dict = {"events": [{"type": "message", "replyToken": "rt",
                              "source": {"type": "user", "userId": "U1"},
                              "message": {"type": "text", "text": "hello"}}]}
    body = json.dumps(body_dict).encode()
    sig = base64.b64encode(hmac.new(CHAN.encode(), body, hashlib.sha256).digest()).decode()
    rr = c.post(f"/line/webhook/{tid}", content=body, headers={"X-Line-Signature": sig})
    print(f"[probe] webhook response = {rr.status_code}")

print(f"[probe] translate ident = {captured.get('ident')}")
print(f"[probe] same thread?    = {captured.get('ident') == main_tid}")
print(f"[probe] reply sent count = {fake.call_count}")
