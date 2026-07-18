"""QA 任務 #3 驗收測試 — LINE webhook grep 門檻 + M2 issue 開票結構檢查。

用途
----
本檔為任務 #3 的純 QA 驗收測試，不新增任何產品程式碼（`src/`）。它做兩件事：

A. 以 grep / 結構檢查斷言 `line_webhook.py` 對齊架構決策的 4 條 grep 門檻
   （raw-body 先於 parse、`isRedelivery` 僅診斷 log、`hmac.new` 唯一入口、
   四條拒絕路徑共用 `_INVALID_SIGNATURE_DETAIL`）。

B. 驗證 `docs/M2_ISSUES.md` 中四項 M2 技術債各具獨立 GitHub issue ID 與
   驗收條件，且未混入本輪 webhook 程式碼。

為什麼是 pytest
----
1. 與既有 webhook 測試共用 `run_tests.sh` 入口，CI 不必再寫 Bash glue。
2. 失敗訊息直接由 pytest report 印出，便於定位。
3. 測試本身只讀檔、不寫入、不 import 產品程式——隔離風險為零。

執行
----
    bash run_tests.sh tests/test_qa_task3_line_webhook_audit.py
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

# ── 路徑常數（相對於 repo root） ──────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
# R7-A 拆分後:line_webhook 為套件,grep 門檻改對全套件串接來源驗證。
WEBHOOK = ROOT / "src" / "saas_mvp" / "routers" / "line_webhook"
M2_ISSUES = ROOT / "docs" / "M2_ISSUES.md"


# ── 內部 helper ─────────────────────────────────────────────────────────────
def _read(path: Path) -> str:
    assert path.exists(), f"必要檔案不存在：{path}"
    if path.is_dir():
        # R7-A:套件——串接全部子模組(__init__ 先,docstring 在最前)。
        return "\n".join(
            p.read_text(encoding="utf-8") for p in sorted(path.glob("*.py"))
        )
    return path.read_text(encoding="utf-8")


def _is_call_to(node: ast.AST, name: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    )


def _is_attr_call(node: ast.AST, obj_name: str, attr_name: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == obj_name
        and node.func.attr == attr_name
    )


# ════════════════════════════════════════════════════════════════════════════
# A. line_webhook.py 架構 grep 門檻（架構決策 §1–§4）
# ════════════════════════════════════════════════════════════════════════════


def test_raw_body_precedes_json_loads():
    """架構決策 §1：`await request.body()` 必須在 `json.loads(body)` 之前。

    任何重構移位會讓 HMAC 驗章靜默失效（test 不驗行號順序）。
    """
    src = _read(WEBHOOK)
    lines = src.splitlines()

    body_idx = next(
        (i for i, ln in enumerate(lines, start=1) if "await request.body()" in ln),
        None,
    )
    loads_idx = next(
        (i for i, ln in enumerate(lines, start=1) if "json.loads(body)" in ln),
        None,
    )
    assert body_idx is not None, "line_webhook.py 缺少 `await request.body()`"
    assert loads_idx is not None, "line_webhook.py 缺少 `json.loads(body)`"
    assert body_idx < loads_idx, (
        f"行號順序違規：`await request.body()` 在第 {body_idx} 行，"
        f"`json.loads(body)` 在第 {loads_idx} 行（必須 body 先）"
    )


def test_isRedelivery_is_pure_diagnostic_log():
    """架構決策 §3：`isRedelivery` 僅作診斷 log，不得參與控制流。

    冪等鍵固定為 `webhookEventId`；混用 `isRedelivery` 會產生不對稱雙路徑。
    """
    src = _read(WEBHOOK)
    assert "isRedelivery" in src, "line_webhook.py 必須提到 isRedelivery（診斷 log）"

    # 控制流關鍵字不得與 isRedelivery 同行／相鄰。
    bad_pattern = re.compile(
        r"isRedelivery[^,)\n]*(skip|continue|return|raise)",
        re.MULTILINE,
    )
    matches = bad_pattern.findall(src)
    assert not matches, (
        f"`isRedelivery` 不得參與控制流；違規關鍵字：{matches}"
    )

    # 並須緊接 `_log.info`／`logging` 呼叫（確保為診斷而非條件分支）。
    ctx_pattern = re.compile(
        r"isRedelivery[\s\S]{0,200}_log\.(info|warning|error|debug)",
        re.MULTILINE,
    )
    assert ctx_pattern.search(src), (
        "`isRedelivery` 區塊內 200 字內必須出現 `_log.<level>` 呼叫"
    )


def test_hmac_new_has_single_canonical_entrypoint():
    """架構決策 §2：`hmac.new` 只能在 `_constant_time_verify` helper 內出現。

    分散 inline 計算會打破 timing side-channel 防護。
    """
    src = _read(WEBHOOK)
    tree = ast.parse(src)

    hmac_calls = [
        node for node in ast.walk(tree)
        if _is_attr_call(node, "hmac", "new")
    ]
    assert len(hmac_calls) == 1, (
        f"`hmac.new` 程式碼行必須恰好 1 次（helper 內），"
        f"實際 {len(hmac_calls)} 次"
    )

    helper = next(
        (
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_constant_time_verify"
        ),
        None,
    )
    assert helper is not None, (
        "缺少 `_constant_time_verify` helper（單一驗簽入口）"
    )
    hmac_call = hmac_calls[0]
    assert helper.lineno < hmac_call.lineno <= helper.end_lineno, (
        f"`hmac.new`（第 {hmac_call.lineno} 行）必須在 "
        f"`_constant_time_verify` 函式體內（第 {helper.lineno} 行起）"
    )


def test_invalid_signature_detail_canonical_constant():
    """架構決策 §2：`_INVALID_SIGNATURE_DETAIL` 是模組常數，且被所有拒絕路徑共用。"""
    src = _read(WEBHOOK)
    tree = ast.parse(src)
    assert '_INVALID_SIGNATURE_DETAIL = "Invalid X-Line-Signature"' in src, (
        "缺少統一的 `_INVALID_SIGNATURE_DETAIL` 常數定義"
    )

    # 計算所有 raise HTTPException 的 detail 是否都用同一個常數。
    raise_lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or not _is_call_to(node.exc, "HTTPException"):
            continue
        detail_kw = next(
            (kw for kw in node.exc.keywords if kw.arg == "detail"),
            None,
        )
        if (
            detail_kw is not None
            and isinstance(detail_kw.value, ast.Name)
            and detail_kw.value.id == "_INVALID_SIGNATURE_DETAIL"
        ):
            raise_lines.append(node.lineno)

    # 至少 3 條拒絕路徑（無 config / 缺 header 或簽章錯 / destination 不符）。
    assert len(raise_lines) >= 3, (
        f"至少 3 條拒絕路徑應使用 `_INVALID_SIGNATURE_DETAIL`，"
        f"實際找到 {len(raise_lines)} 條"
    )

    # 反例：不得在 webhook handler 主體內 inline 寫死 "Invalid X-Line-Signature"
    # （除了常數定義那一行）。
    literal_uses = [
        ln for ln in src.splitlines()
        if '"Invalid X-Line-Signature"' in ln
        and not ln.lstrip().startswith("_INVALID_SIGNATURE_DETAIL")
    ]
    assert len(literal_uses) == 0, (
        f"不得 inline 寫死字面值，僅可引用常數；違規行：\n  "
        + "\n  ".join(literal_uses)
    )


# ════════════════════════════════════════════════════════════════════════════
# B. M2 issue 開票結構（架構決策 §6 + 任務 #3 驗收標準）
# ════════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def m2_issues_text() -> str:
    return _read(M2_ISSUES)


_M2_EXPECTED = [
    # (issue_id, github_number, 主題關鍵字)
    ("M2-LINE-WEBHOOK-001", 70, "MAX_ATTEMPTS"),
    ("M2-LINE-WEBHOOK-002", 71, "TTL"),
    ("M2-LINE-WEBHOOK-003", 72, "last_error"),
    ("M2-LINE-WEBHOOK-004", 73, "pending"),
]


@pytest.mark.parametrize("issue_id, github_number, _kw", _M2_EXPECTED)
def test_m2_issue_has_github_tracker_url(m2_issues_text, issue_id, github_number, _kw):
    """架構決策 §6：四項 M2 技術債各有獨立 GitHub issue tracker ID。"""
    expected_url = f"https://github.com/x812033727/SaaS/issues/{github_number}"
    assert issue_id in m2_issues_text, f"缺少 `{issue_id}` 段落標題"
    assert expected_url in m2_issues_text, (
        f"`{issue_id}` 段落缺少 GitHub issue URL `{expected_url}`"
    )


@pytest.mark.parametrize("issue_id, _num, title_keyword", _M2_EXPECTED)
def test_m2_issue_has_acceptance_block(m2_issues_text, issue_id, _num, title_keyword):
    """架構決策 §6：每票須附具體驗收條件，且條件內容含主題關鍵字。"""
    # 取出 `## <issue_id>` 段落（到下一個 `## ` 或檔尾）。
    pattern = re.compile(
        rf"## {re.escape(issue_id)}\n(.*?)(?=\n## |\Z)",
        re.DOTALL,
    )
    match = pattern.search(m2_issues_text)
    assert match, f"`{issue_id}` 段落不存在"

    section = match.group(1)
    assert "驗收：" in section, f"`{issue_id}` 缺少「驗收：」區塊"
    # 取出驗收區塊後的內容，必須含對應主題關鍵字
    accept_block = section.split("驗收：", 1)[1]
    assert title_keyword in accept_block, (
        f"`{issue_id}` 驗收區塊缺少主題關鍵字 `{title_keyword}`"
    )


def test_m2_issues_not_mixed_into_webhook_code():
    """任務 #3 驗收標準：M2 技術債不得混入本輪 webhook 程式碼。

    允許的：docstring 內以 `[M2-LINE-WEBHOOK-NNN](../../../docs/M2_ISSUES.md#...)`
    形式指向 docs/M2_ISSUES.md（這是文件引用，不是實作）。

    禁止的：`MAX_ATTEMPTS` 賦值、TTL 刪除 SQL、Prometheus client import、
    監控告警規則等『M2 hardening 程式碼』直接寫在 webhook 路由檔內。
    """
    src = _read(WEBHOOK)
    forbidden_tokens = [
        ("MAX_ATTEMPTS = 5", "`MAX_ATTEMPTS` 實作不應出現在 webhook 路由"),
        ("MAX_ATTEMPTS = ", "`MAX_ATTEMPTS` 設定不應出現在 webhook 路由"),
        ("prometheus_client", "Prometheus 監控不應混入 webhook 路由"),
        ("delete(LineWebhookEvent", "TTL 清理 SQL 不應混入 webhook 路由"),
    ]
    for token, msg in forbidden_tokens:
        assert token not in src, f"{msg}（找到 token: {token!r}）"

    # 反向確認：docstring 仍指向四張 issue 錨點（確保沒漏開票）
    for issue_id, _num, _kw in _M2_EXPECTED:
        assert issue_id in src, (
            f"webhook docstring 應引用 `{issue_id}` 錨點（指向 docs/M2_ISSUES.md）"
        )


def test_m2_issues_file_is_standalone_and_not_in_pr():
    """任務 #3 驗收標準：M2 開票為獨立檔案，不混入本輪 webhook PR。

    確認 `docs/M2_ISSUES.md` 與本輪 webhook 路由是兩個獨立檔案路徑，
    並且 M2 開票清單不會被誤加到 webhook 路由的 PR diff 內。
    """
    assert M2_ISSUES != WEBHOOK, "M2 開票不得與 webhook 路由共用檔案"
    assert M2_ISSUES.exists(), "M2 開票檔 `docs/M2_ISSUES.md` 必須存在"
    assert WEBHOOK.exists(), "webhook 路由檔必須存在"

    # docs/M2_ISSUES.md 不得 import 任何 src/ 程式碼（純 markdown 開票）。
    m2_text = M2_ISSUES.read_text(encoding="utf-8")
    assert "from saas_mvp" not in m2_text, "M2 開票檔不得 import 產品程式碼"
    assert "import saas_mvp" not in m2_text, "M2 開票檔不得 import 產品程式碼"
