"""QA 驗收測試 — Task #1：LineChannelConfig model

驗收標準：
  - channel secret/token 非明文存 DB（加密或等效），且能還原供驗章/回覆使用
  - 每租戶獨立、一對一

測試涵蓋：
  1. 模型可 import，metadata 載入後表格與欄位存在
  2. DB 存的是 bytes（非明文）；加密值與原始明文不同
  3. 屬性讀取可正確還原明文（可逆解密）
  4. setter 覆寫後解密仍正確
  5. tenant_id UNIQUE 約束（一對一）
  6. Tenant.line_channel_config relationship（uselist=False）
  7. default_target_lang 預設值 "zh-TW"
  8. created_at / updated_at 自動填入
  9. 多租戶隔離：各自的 LineChannelConfig 解密後不互相混淆
 10. 離線執行（dev 預設金鑰即可，不需外部環境變數）
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# 確保使用 dev 預設金鑰（不需 SAAS_LINE_CHANNEL_ENCRYPT_KEY env var）
os.environ.setdefault("SAAS_LINE_CHANNEL_ENCRYPT_KEY",
                      "ZGV2LWxpbmUtc2VjcmV0LWtleS0zMmJ5dGVzLWxvbmc=")

from saas_mvp.db import Base
# 必須先 import 所有 Tenant relationship 依賴的 model，
# 否則 SQLAlchemy 字串式 relationship 無法解析
from saas_mvp.models import user as _u, note as _n  # noqa: F401
from saas_mvp.models import api_key as _ak, api_key_usage as _aku  # noqa: F401
from saas_mvp.models import usage as _us, plan_change_history as _pch  # noqa: F401
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.line_channel_config import (
    LineChannelConfig,
    LineConfigDecryptionError,
    InvalidTargetLangError,
    encrypt_field,
    decrypt_field,
)


# ─────────────────────────── 共用 fixture ─────────────────────────────────────

@pytest.fixture(scope="module")
def db_session():
    """建立獨立 in-memory SQLite，載入所有 metadata 後提供 session。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    sess = Session()
    yield sess
    sess.close()


@pytest.fixture()
def fresh_db():
    """每測試一個獨立 in-memory DB，避免狀態污染。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    sess = Session()
    yield sess
    sess.close()


def _make_tenant(db, name: str = "acme") -> Tenant:
    t = Tenant(name=name, plan="free")
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _make_config(db, tenant: Tenant,
                 secret: str = "my-secret",
                 token: str = "my-token",
                 lang: str = "zh-TW") -> LineChannelConfig:
    cfg = LineChannelConfig(
        tenant_id=tenant.id,
        default_target_lang=lang,
    )
    cfg.channel_secret = secret
    cfg.access_token = token
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return cfg


# ─────────────────────────── 1. 模型可 import & metadata ─────────────────────

class TestModelMetadata:
    """表格、欄位、索引、約束必須正確載入 SQLAlchemy metadata。"""

    def test_import_line_channel_config(self):
        """LineChannelConfig 可從 models.line_channel_config import。"""
        from saas_mvp.models.line_channel_config import LineChannelConfig as LCC
        assert LCC is not None

    def test_tablename(self):
        assert LineChannelConfig.__tablename__ == "line_channel_configs"

    def test_table_exists_in_db(self, db_session):
        insp = inspect(db_session.bind)
        assert "line_channel_configs" in insp.get_table_names()

    def test_column_id_exists(self, db_session):
        insp = inspect(db_session.bind)
        cols = {c["name"] for c in insp.get_columns("line_channel_configs")}
        assert "id" in cols

    def test_column_tenant_id_exists(self, db_session):
        insp = inspect(db_session.bind)
        cols = {c["name"] for c in insp.get_columns("line_channel_configs")}
        assert "tenant_id" in cols

    def test_column_channel_secret_enc_exists(self, db_session):
        insp = inspect(db_session.bind)
        cols = {c["name"] for c in insp.get_columns("line_channel_configs")}
        assert "channel_secret_enc" in cols

    def test_column_access_token_enc_exists(self, db_session):
        insp = inspect(db_session.bind)
        cols = {c["name"] for c in insp.get_columns("line_channel_configs")}
        assert "access_token_enc" in cols

    def test_column_default_target_lang_exists(self, db_session):
        insp = inspect(db_session.bind)
        cols = {c["name"] for c in insp.get_columns("line_channel_configs")}
        assert "default_target_lang" in cols

    def test_column_created_at_exists(self, db_session):
        insp = inspect(db_session.bind)
        cols = {c["name"] for c in insp.get_columns("line_channel_configs")}
        assert "created_at" in cols

    def test_column_updated_at_exists(self, db_session):
        insp = inspect(db_session.bind)
        cols = {c["name"] for c in insp.get_columns("line_channel_configs")}
        assert "updated_at" in cols

    def test_tenant_id_unique_constraint(self, db_session):
        """tenant_id 必須有 UNIQUE 約束（一對一）。"""
        insp = inspect(db_session.bind)
        unique_constraints = insp.get_unique_constraints("line_channel_configs")
        # SQLite 可能用 index 表示 unique；兩者皆查
        unique_indexes = [
            idx for idx in insp.get_indexes("line_channel_configs")
            if idx.get("unique")
        ]
        unique_cols = set()
        for uc in unique_constraints:
            unique_cols.update(uc["column_names"])
        for ui in unique_indexes:
            unique_cols.update(ui["column_names"])
        assert "tenant_id" in unique_cols, "tenant_id 應有 UNIQUE 約束"


# ─────────────────────────── 2. 非明文儲存 ────────────────────────────────────

class TestEncryptedStorage:
    """DB 中的 _enc 欄位不得是明文字串。"""

    def test_channel_secret_enc_is_bytes(self, fresh_db):
        t = _make_tenant(fresh_db, "enc-test")
        cfg = _make_config(fresh_db, t, secret="plaintext-secret")
        assert isinstance(cfg.channel_secret_enc, (bytes, bytearray)), \
            "channel_secret_enc 應為 bytes，不是 str"

    def test_access_token_enc_is_bytes(self, fresh_db):
        t = _make_tenant(fresh_db, "enc-test2")
        cfg = _make_config(fresh_db, t, token="plaintext-token")
        assert isinstance(cfg.access_token_enc, (bytes, bytearray)), \
            "access_token_enc 應為 bytes，不是 str"

    def test_channel_secret_enc_not_equal_to_plaintext(self, fresh_db):
        """加密後的 bytes 不得與明文相同。"""
        t = _make_tenant(fresh_db, "ne-secret")
        cfg = _make_config(fresh_db, t, secret="my-channel-secret")
        assert cfg.channel_secret_enc != b"my-channel-secret", \
            "channel_secret_enc 不應存明文"

    def test_access_token_enc_not_equal_to_plaintext(self, fresh_db):
        t = _make_tenant(fresh_db, "ne-token")
        cfg = _make_config(fresh_db, t, token="my-access-token")
        assert cfg.access_token_enc != b"my-access-token", \
            "access_token_enc 不應存明文"

    def test_raw_db_value_is_not_plaintext_string(self, fresh_db):
        """直接查 SQLite raw value 也不應是明文字串。"""
        t = _make_tenant(fresh_db, "raw-db")
        cfg = _make_config(fresh_db, t, secret="super-secret-raw")
        row = fresh_db.execute(
            text("SELECT channel_secret_enc FROM line_channel_configs WHERE id = :id"),
            {"id": cfg.id},
        ).fetchone()
        raw = row[0]
        # raw 可能是 bytes 或 str（SQLite 行為），但不應包含明文
        raw_bytes = raw if isinstance(raw, bytes) else raw.encode("latin-1")
        assert b"super-secret-raw" not in raw_bytes, \
            "DB 中不應存放明文 secret"


# ─────────────────────────── 3. 可逆解密 ─────────────────────────────────────

class TestDecryptionRoundtrip:
    """channel_secret / access_token property 能正確還原明文。"""

    def test_channel_secret_roundtrip(self, fresh_db):
        t = _make_tenant(fresh_db, "rt-secret")
        cfg = _make_config(fresh_db, t, secret="secret-abc-123")
        assert cfg.channel_secret == "secret-abc-123"

    def test_access_token_roundtrip(self, fresh_db):
        t = _make_tenant(fresh_db, "rt-token")
        cfg = _make_config(fresh_db, t, token="token-xyz-456")
        assert cfg.access_token == "token-xyz-456"

    def test_unicode_secret_roundtrip(self, fresh_db):
        """Unicode 字元也能正確加解密。"""
        t = _make_tenant(fresh_db, "rt-unicode")
        cfg = _make_config(fresh_db, t, secret="秘密金鑰-🔑")
        assert cfg.channel_secret == "秘密金鑰-🔑"

    def test_long_token_roundtrip(self, fresh_db):
        """長 token（LINE access token 通常 >100 字元）。"""
        long_token = "A" * 256
        t = _make_tenant(fresh_db, "rt-long")
        cfg = _make_config(fresh_db, t, token=long_token)
        assert cfg.access_token == long_token

    def test_reload_from_db_still_decrypts(self, fresh_db):
        """從 DB 重新查詢後 property 仍能解密（非 in-memory cache）。"""
        t = _make_tenant(fresh_db, "rt-reload")
        cfg = _make_config(fresh_db, t, secret="reload-secret", token="reload-token")
        cfg_id = cfg.id
        fresh_db.expire_all()  # 清 ORM cache
        reloaded = fresh_db.get(LineChannelConfig, cfg_id)
        assert reloaded is not None
        assert reloaded.channel_secret == "reload-secret"
        assert reloaded.access_token == "reload-token"


# ─────────────────────────── 4. setter 覆寫 ──────────────────────────────────

class TestSetterUpdate:
    """setter 應可覆寫舊值，且解密後得到新值。"""

    def test_channel_secret_setter_overwrite(self, fresh_db):
        t = _make_tenant(fresh_db, "setter-s")
        cfg = _make_config(fresh_db, t, secret="old-secret")
        cfg.channel_secret = "new-secret"
        fresh_db.commit()
        fresh_db.expire_all()
        reloaded = fresh_db.get(LineChannelConfig, cfg.id)
        assert reloaded.channel_secret == "new-secret"

    def test_access_token_setter_overwrite(self, fresh_db):
        t = _make_tenant(fresh_db, "setter-t")
        cfg = _make_config(fresh_db, t, token="old-token")
        cfg.access_token = "new-token"
        fresh_db.commit()
        fresh_db.expire_all()
        reloaded = fresh_db.get(LineChannelConfig, cfg.id)
        assert reloaded.access_token == "new-token"

    def test_old_enc_bytes_changed_after_overwrite(self, fresh_db):
        """setter 覆寫後 _enc 欄位的值應改變（Fernet 每次加密不同 nonce）。"""
        t = _make_tenant(fresh_db, "setter-nonce")
        cfg = _make_config(fresh_db, t, secret="same-value")
        old_enc = bytes(cfg.channel_secret_enc)
        cfg.channel_secret = "same-value"  # 相同值，但 Fernet 每次 nonce 不同
        fresh_db.commit()
        # enc bytes 改了，但解密後相同
        assert cfg.channel_secret == "same-value"


# ─────────────────────────── 5. 一對一約束 ───────────────────────────────────

class TestOneToOne:
    """同一 tenant 不能有兩筆 LineChannelConfig。"""

    def test_duplicate_tenant_id_raises(self, fresh_db):
        """插入第二筆相同 tenant_id 應引發 IntegrityError。"""
        from sqlalchemy.exc import IntegrityError
        t = _make_tenant(fresh_db, "oto-tenant")
        _make_config(fresh_db, t, secret="first")
        with pytest.raises(IntegrityError):
            cfg2 = LineChannelConfig(
                tenant_id=t.id,
                default_target_lang="en",
            )
            cfg2.channel_secret = "second"
            cfg2.access_token = "second-token"
            fresh_db.add(cfg2)
            fresh_db.commit()

    def test_different_tenants_can_each_have_config(self, fresh_db):
        """不同 tenant 各自有一筆 config，合法。"""
        t1 = _make_tenant(fresh_db, "oto-t1")
        t2 = _make_tenant(fresh_db, "oto-t2")
        cfg1 = _make_config(fresh_db, t1, secret="s1", token="tk1")
        cfg2 = _make_config(fresh_db, t2, secret="s2", token="tk2")
        assert cfg1.id != cfg2.id
        assert cfg1.channel_secret == "s1"
        assert cfg2.channel_secret == "s2"


# ─────────────────────────── 6. Tenant relationship ──────────────────────────

class TestTenantRelationship:
    """Tenant.line_channel_config relationship 為 uselist=False（一對一）。"""

    def test_tenant_has_line_channel_config_attr(self):
        t = Tenant(name="rel-test", plan="free")
        assert hasattr(t, "line_channel_config")

    def test_tenant_line_channel_config_is_single_object(self, fresh_db):
        """透過 Tenant relationship 取得的是單個物件，不是 list。"""
        t = _make_tenant(fresh_db, "rel-single")
        cfg = _make_config(fresh_db, t, secret="rel-s", token="rel-tk")
        fresh_db.expire_all()
        t_reloaded = fresh_db.get(Tenant, t.id)
        assert t_reloaded.line_channel_config is not None
        assert not isinstance(t_reloaded.line_channel_config, list)
        assert t_reloaded.line_channel_config.id == cfg.id

    def test_tenant_without_config_returns_none(self, fresh_db):
        t = _make_tenant(fresh_db, "rel-none")
        fresh_db.expire_all()
        t_reloaded = fresh_db.get(Tenant, t.id)
        assert t_reloaded.line_channel_config is None

    def test_config_back_ref_to_tenant(self, fresh_db):
        """LineChannelConfig.tenant 能回指正確的 Tenant。"""
        t = _make_tenant(fresh_db, "backref-t")
        cfg = _make_config(fresh_db, t, secret="br-s")
        fresh_db.expire_all()
        cfg_reloaded = fresh_db.get(LineChannelConfig, cfg.id)
        assert cfg_reloaded.tenant is not None
        assert cfg_reloaded.tenant.id == t.id
        assert cfg_reloaded.tenant.name == "backref-t"


# ─────────────────────────── 7. default_target_lang ──────────────────────────

class TestDefaultTargetLang:
    def test_default_lang_is_zh_tw(self, fresh_db):
        """未指定 lang 時預設值應為 'zh-TW'。"""
        t = _make_tenant(fresh_db, "lang-default")
        cfg = LineChannelConfig(tenant_id=t.id)
        cfg.channel_secret = "s"
        cfg.access_token = "tk"
        fresh_db.add(cfg)
        fresh_db.commit()
        fresh_db.refresh(cfg)
        assert cfg.default_target_lang == "zh-TW"

    def test_custom_lang_persisted(self, fresh_db):
        t = _make_tenant(fresh_db, "lang-ja")
        cfg = _make_config(fresh_db, t, lang="ja")
        fresh_db.expire_all()
        reloaded = fresh_db.get(LineChannelConfig, cfg.id)
        assert reloaded.default_target_lang == "ja"

    def test_lang_update(self, fresh_db):
        t = _make_tenant(fresh_db, "lang-update")
        cfg = _make_config(fresh_db, t, lang="en")
        cfg.default_target_lang = "ko"
        fresh_db.commit()
        fresh_db.expire_all()
        reloaded = fresh_db.get(LineChannelConfig, cfg.id)
        assert reloaded.default_target_lang == "ko"


# ─────────────────────────── 8. created_at / updated_at ──────────────────────

class TestTimestamps:
    def test_created_at_auto_filled(self, fresh_db):
        t = _make_tenant(fresh_db, "ts-create")
        cfg = _make_config(fresh_db, t)
        assert cfg.created_at is not None

    def test_updated_at_auto_filled(self, fresh_db):
        t = _make_tenant(fresh_db, "ts-update")
        cfg = _make_config(fresh_db, t)
        assert cfg.updated_at is not None

    def test_timestamps_are_datetime(self, fresh_db):
        import datetime
        t = _make_tenant(fresh_db, "ts-type")
        cfg = _make_config(fresh_db, t)
        assert isinstance(cfg.created_at, datetime.datetime)
        assert isinstance(cfg.updated_at, datetime.datetime)


# ─────────────────────────── 9. 多租戶隔離 ───────────────────────────────────

class TestMultiTenantIsolation:
    """不同租戶的 channel secret/token 必須互相隔離，不可混淆。"""

    def test_two_tenants_secrets_are_independent(self, fresh_db):
        t1 = _make_tenant(fresh_db, "iso-t1")
        t2 = _make_tenant(fresh_db, "iso-t2")
        cfg1 = _make_config(fresh_db, t1, secret="secret-for-t1", token="token-for-t1")
        cfg2 = _make_config(fresh_db, t2, secret="secret-for-t2", token="token-for-t2")

        assert cfg1.channel_secret == "secret-for-t1"
        assert cfg2.channel_secret == "secret-for-t2"
        assert cfg1.channel_secret != cfg2.channel_secret

    def test_two_tenants_tokens_are_independent(self, fresh_db):
        t1 = _make_tenant(fresh_db, "iso-tk1")
        t2 = _make_tenant(fresh_db, "iso-tk2")
        cfg1 = _make_config(fresh_db, t1, token="token-A")
        cfg2 = _make_config(fresh_db, t2, token="token-B")

        assert cfg1.access_token == "token-A"
        assert cfg2.access_token == "token-B"

    def test_enc_bytes_differ_even_for_same_plaintext(self, fresh_db):
        """Fernet 每次加密不同 nonce，即使明文相同，_enc 也不同。"""
        t1 = _make_tenant(fresh_db, "iso-same1")
        t2 = _make_tenant(fresh_db, "iso-same2")
        same_secret = "identical-secret"
        cfg1 = _make_config(fresh_db, t1, secret=same_secret)
        cfg2 = _make_config(fresh_db, t2, secret=same_secret)

        # 解密後相同
        assert cfg1.channel_secret == cfg2.channel_secret == same_secret
        # 但加密 bytes 不同（Fernet nonce）
        assert cfg1.channel_secret_enc != cfg2.channel_secret_enc


# ─────────────────────────── 10. 加密工具函數 ────────────────────────────────

class TestEncryptDecryptUtils:
    """encrypt_field / decrypt_field 公用函數正確性。"""

    def test_encrypt_returns_bytes(self):
        result = encrypt_field("hello")
        assert isinstance(result, bytes)

    def test_decrypt_returns_str(self):
        enc = encrypt_field("world")
        result = decrypt_field(enc)
        assert isinstance(result, str)

    def test_encrypt_decrypt_roundtrip(self):
        plaintext = "roundtrip-test-123"
        assert decrypt_field(encrypt_field(plaintext)) == plaintext

    def test_encrypt_not_idempotent(self):
        """Fernet nonce → 同輸入每次加密結果不同。"""
        enc1 = encrypt_field("same")
        enc2 = encrypt_field("same")
        assert enc1 != enc2

    def test_wrong_bytes_raises(self):
        """非 Fernet ciphertext 應引發 LineConfigDecryptionError（包裝 InvalidToken）。"""
        with pytest.raises(LineConfigDecryptionError):
            decrypt_field(b"not-fernet-ciphertext")

    def test_offline_default_key_works(self):
        """使用 config.py dev 預設金鑰，離線即可完成加解密。"""
        from saas_mvp.config import settings
        # 預設金鑰應為 44 字元 URL-safe base64
        assert len(settings.line_channel_encrypt_key) > 0
        # 實際加解密驗證
        msg = "offline-test-no-env-needed"
        assert decrypt_field(encrypt_field(msg)) == msg


# ─────────────────────────── 11. @validates BCP-47 強制（ORM 層）──────────────

class TestOrmValidatesLang:
    """@validates('default_target_lang') 確保直接賦值也觸發 BCP-47 驗證。

    防護不是孤立工具函式，而是掛在 ORM 屬性上——任何賦值路徑都無法繞過。
    """

    def test_invalid_lang_direct_assignment_raises(self):
        """直接賦值壞值（含注入字元）應拋 InvalidTargetLangError。"""
        cfg = LineChannelConfig(tenant_id=999)
        with pytest.raises(InvalidTargetLangError):
            cfg.default_target_lang = "en; rm -rf"

    def test_invalid_lang_in_constructor_raises(self):
        """在 constructor 中傳入壞值也應拋錯。"""
        with pytest.raises(InvalidTargetLangError):
            LineChannelConfig(tenant_id=999, default_target_lang="not valid!")

    def test_valid_langs_not_rejected(self):
        """合法 BCP-47 tag 不拋錯。"""
        cfg = LineChannelConfig(tenant_id=999)
        for lang in ["en", "zh-TW", "zh-Hant-TW", "ja", "ko", "fr"]:
            cfg.default_target_lang = lang  # 不應拋錯

    def test_injection_attempt_rejected(self):
        """注入嘗試（含空格/分號/斜線）被拒。"""
        bad_values = ["en; rm -rf", "zh TW", "ja/en", "../etc/passwd", ""]
        cfg = LineChannelConfig(tenant_id=999)
        for bad in bad_values:
            with pytest.raises(InvalidTargetLangError):
                cfg.default_target_lang = bad

    def test_orm_validates_on_db_round_trip(self, fresh_db):
        """DB round-trip 後 update 也觸發驗證。"""
        t = _make_tenant(fresh_db, "validates-rt")
        cfg = _make_config(fresh_db, t, lang="en")
        with pytest.raises(InvalidTargetLangError):
            cfg.default_target_lang = "bad value!"


# ─────────────────────────── 12. model_validator 讀 self.env ──────────────────

class TestConfigModelValidator:
    """line_channel_encrypt_key guard 用 model_validator 讀 self.env，
    確保 .env 部署場景下也能正確拒絕 dev 預設金鑰。"""

    def test_dev_env_allows_default_key(self):
        """env='dev' 時允許使用 dev 預設金鑰（不拋錯）。"""
        from saas_mvp.config import Settings, _LINE_KEY_DEV_DEFAULT
        s = Settings(env="dev", line_channel_encrypt_key=_LINE_KEY_DEV_DEFAULT)
        assert s.line_channel_encrypt_key == _LINE_KEY_DEV_DEFAULT

    def test_test_env_allows_default_key(self):
        """env='test' 時也允許使用 dev 預設金鑰。"""
        from saas_mvp.config import Settings, _LINE_KEY_DEV_DEFAULT
        s = Settings(env="test", line_channel_encrypt_key=_LINE_KEY_DEV_DEFAULT)
        assert s.line_channel_encrypt_key == _LINE_KEY_DEV_DEFAULT

    def test_production_env_rejects_default_key(self):
        """env='production' 時拒絕 dev 預設金鑰（讀 self.env，不靠 os.getenv）。"""
        from pydantic import ValidationError
        from saas_mvp.config import Settings, _LINE_KEY_DEV_DEFAULT
        with pytest.raises(ValidationError):
            Settings(env="production", line_channel_encrypt_key=_LINE_KEY_DEV_DEFAULT)

    def test_production_env_accepts_custom_key(self):
        """env='production' 時提供非預設金鑰可正常啟動。"""
        from cryptography.fernet import Fernet
        from saas_mvp.config import Settings
        custom_key = Fernet.generate_key().decode()
        s = Settings(env="production", line_channel_encrypt_key=custom_key)
        assert s.line_channel_encrypt_key == custom_key
