"""FAQ 模糊比對（字元 bigram）測試 — 中文加綴詞問法、排序、context 上限。

針對舊版「整句子字串」比對漏掉「請問營業時間？」對上長題目「營業時間？幾點…」
的問題，驗證新版字元 bigram 重疊能命中，且不誤判無關 FAQ。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.models import tenant as _t  # noqa: F401
from saas_mvp.models import faq_entry as _f  # noqa: F401

from saas_mvp.db import Base
from saas_mvp.models.tenant import Tenant
from saas_mvp.services import faq as faq_svc

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    s = _Session()
    try:
        yield s
    finally:
        s.close()


def _tenant(db) -> int:
    t = Tenant(name="faq_test", plan="free")
    db.add(t)
    db.flush()
    return t.id


def _seed(db, tid):
    faq_svc.create_faq(
        db, tenant_id=tid,
        question="營業時間？幾點開到幾點？",
        answer="週一至週六 11:00–21:00，週日公休。", sort_order=1,
    )
    faq_svc.create_faq(
        db, tenant_id=tid,
        question="有停車位嗎？", answer="店門口有路邊停車格。", sort_order=2,
    )
    faq_svc.create_faq(
        db, tenant_id=tid,
        question="可以刷卡嗎？", answer="接受現金、信用卡與 LINE Pay。", sort_order=3,
    )


class TestFuzzyMatch:
    def test_long_question_matched_by_short_fuzzy_query(self, db):
        """舊版會漏：加綴詞的「請問營業時間？」對上長題目。"""
        tid = _tenant(db)
        _seed(db, tid)
        matched = faq_svc.match(db, tid, "請問營業時間？")
        assert matched, "模糊問法應命中營業時間 FAQ"
        assert "11:00" in matched[0].answer

    def test_no_false_positive_for_unrelated(self, db):
        tid = _tenant(db)
        _seed(db, tid)
        # 與任何 FAQ 都無字元重疊的問題。
        assert faq_svc.match(db, tid, "完全無關ABCXYZ") == []

    def test_ranking_exact_substring_first(self, db):
        """精準包含的 FAQ 應排在純 bigram 部分重疊者之前。"""
        tid = _tenant(db)
        _seed(db, tid)
        matched = faq_svc.match(db, tid, "停車")
        assert matched[0].question == "有停車位嗎？"

    def test_payment_variant_phrasing(self, db):
        tid = _tenant(db)
        _seed(db, tid)
        matched = faq_svc.match(db, tid, "請問可以刷卡付款嗎")
        assert matched
        assert "信用卡" in matched[0].answer

    def test_top_k_limits_results(self, db):
        tid = _tenant(db)
        _seed(db, tid)
        # 三筆都含「嗎/？」類字元時，仍只回前 1。
        matched = faq_svc.match(db, tid, "營業時間 停車 刷卡", top_k=1)
        assert len(matched) == 1

    def test_empty_question_returns_empty(self, db):
        tid = _tenant(db)
        _seed(db, tid)
        assert faq_svc.match(db, tid, "   ") == []


class TestBuildContext:
    def test_context_includes_matched_qa(self, db):
        tid = _tenant(db)
        _seed(db, tid)
        ctx = faq_svc.build_context(db, tid, "請問營業時間？")
        assert "營業時間" in ctx
        assert "11:00" in ctx
        assert ctx.startswith("Q: ")

    def test_context_caps_entries(self, db):
        tid = _tenant(db)
        # 塞 10 筆都含「預約」的 FAQ，context 應被 max_entries 限制。
        for i in range(10):
            faq_svc.create_faq(
                db, tenant_id=tid,
                question=f"預約問題{i}", answer=f"預約答案{i}", sort_order=i,
            )
        ctx = faq_svc.build_context(db, tid, "預約", max_entries=3)
        assert ctx.count("Q: ") == 3

    def test_context_empty_when_no_match(self, db):
        tid = _tenant(db)
        _seed(db, tid)
        assert faq_svc.build_context(db, tid, "完全無關ABCXYZ") == ""
