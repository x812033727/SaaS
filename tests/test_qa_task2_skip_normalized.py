"""QA 驗收 — 任務 #2：DeepL 同語言 skip 以「正規化後 target」比較。

驗收標準：DeepL 回應含 detected_source_language 等於正規化後 target 時，
translate() 回傳原文，不重複包裝/翻譯。

關鍵語意（本檔鎖死）：
- 比較對象是 _normalize_target_lang(target)，**不是**原始 BCP-47 target。
  例 target=zh-TW → norm=ZH-HANT；唯有 detected=ZH-HANT 才 skip。
- skip 回傳的是傳入的原文物件本身，不含任何 [LANG] 包裝、不回 body 內譯文。

全程離線：mock urllib.request.urlopen，不打真實 DeepL。獨立檔，不改既有測試。
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from saas_mvp.translation.http import DeepLTranslator


def _patch_urlopen(response_body: dict, capture: dict | None = None):
    """回傳 patcher：fake urlopen 吐 response_body，並可擷取送出的 payload。"""
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(response_body).encode()

    def _fake_urlopen(req, timeout=None):
        if capture is not None:
            capture["data"] = req.data
        return _Resp()

    return mock.patch(
        "saas_mvp.translation.http.urllib.request.urlopen", _fake_urlopen
    )


# ── skip 觸發：detected == 正規化後 target ─────────────────────────────────

def test_skip_zh_tw_when_detected_is_normalized_hant():
    """target=zh-TW → norm=ZH-HANT；detected=ZH-HANT 應 skip 回原文。"""
    body = {"translations": [{"detected_source_language": "ZH-HANT",
                              "text": "（譯文不應被回覆）"}]}
    with _patch_urlopen(body):
        t = DeepLTranslator(api_key="k")
        original = "今天天氣很好"
        out = t.translate(original, "zh-TW")
    assert out == original
    assert out is original          # 回傳原文物件本身
    assert "[" not in out           # 無 [LANG] 包裝


def test_skip_zh_cn_when_detected_is_normalized_hans():
    body = {"translations": [{"detected_source_language": "ZH-HANS",
                              "text": "x"}]}
    with _patch_urlopen(body):
        t = DeepLTranslator(api_key="k")
        out = t.translate("今天天气很好", "zh-CN")
    assert out == "今天天气很好"


def test_skip_detected_case_insensitive():
    """detected 小寫 zh-hant 仍應與 norm(ZH-HANT) 比對成功。"""
    body = {"translations": [{"detected_source_language": "zh-hant", "text": "x"}]}
    with _patch_urlopen(body):
        t = DeepLTranslator(api_key="k")
        out = t.translate("原文", "ZH-TW")
    assert out == "原文"


# ── skip 不觸發：detected != 正規化後 target ───────────────────────────────

def test_no_skip_when_detected_is_raw_unnormalized_target():
    """陷阱：detected=ZH-TW（未正規化）!= norm(ZH-HANT) → 不可 skip。

    若實作誤用「原始 target」比較，這條會錯誤地 skip。鎖死必須用 norm。
    """
    body = {"translations": [{"detected_source_language": "ZH-TW",
                              "text": "正體譯文"}]}
    with _patch_urlopen(body):
        t = DeepLTranslator(api_key="k")
        out = t.translate("simplified-ish", "zh-TW")
    assert out == "正體譯文"          # 回譯文，未 skip


def test_no_skip_different_script_hans_vs_hant():
    """來源 ZH-HANS、target zh-TW(norm ZH-HANT)：不同語系，須翻譯不可 skip。"""
    body = {"translations": [{"detected_source_language": "ZH-HANS",
                              "text": "繁體結果"}]}
    with _patch_urlopen(body):
        t = DeepLTranslator(api_key="k")
        out = t.translate("简体来源", "zh-TW")
    assert out == "繁體結果"


def test_no_skip_normal_cross_language():
    body = {"translations": [{"detected_source_language": "EN", "text": "你好"}]}
    with _patch_urlopen(body):
        t = DeepLTranslator(api_key="k")
        out = t.translate("hello", "zh-TW")
    assert out == "你好"


# ── payload 與 skip 共用同一 norm（防呆一致性）────────────────────────────

def test_payload_uses_norm_and_skip_consistent():
    """送出 payload 的 target_lang 與 skip 比較必須是同一個 norm 值。"""
    capture: dict = {}
    body = {"translations": [{"detected_source_language": "ZH-HANT", "text": "y"}]}
    with _patch_urlopen(body, capture):
        t = DeepLTranslator(api_key="k")
        out = t.translate("原文資料", "zh-TW")
    sent = capture["data"].decode()
    assert "target_lang=ZH-HANT" in sent     # payload 用 norm
    assert "ZH-TW" not in sent
    assert out == "原文資料"                  # 同一 norm 觸發 skip


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
