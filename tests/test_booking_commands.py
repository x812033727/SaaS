"""預約指令解析器測試（純函式、無 DB）。"""

from __future__ import annotations

from saas_mvp.booking.commands import parse_booking_command, parse_postback_data


class TestParseTextCommand:
    def test_slots_keyword_zh(self):
        assert parse_booking_command("時段") == ("slots", {})

    def test_slots_slash(self):
        assert parse_booking_command("/slots") == ("slots", {})

    def test_book_slash_with_party(self):
        assert parse_booking_command("/book 12 4") == (
            "book",
            {"slot_id": 12, "party_size": 4},
        )

    def test_book_zh_default_party_one(self):
        assert parse_booking_command("預約 12") == (
            "book",
            {"slot_id": 12, "party_size": 1},
        )

    def test_book_missing_slot_id_omits_key(self):
        action, params = parse_booking_command("預約")
        assert action == "book"
        assert "slot_id" not in params

    def test_cancel_with_id(self):
        assert parse_booking_command("取消 7") == (
            "cancel",
            {"reservation_id": 7},
        )

    def test_my_keyword(self):
        assert parse_booking_command("我的預約") == ("my", {})

    def test_help(self):
        assert parse_booking_command("說明") == ("help", {})

    def test_non_command_returns_none(self):
        assert parse_booking_command("隨便打字") == (None, {})

    def test_empty_returns_none(self):
        assert parse_booking_command("") == (None, {})

    def test_book_garbage_slot_id_omitted(self):
        action, params = parse_booking_command("預約 abc")
        assert action == "book"
        assert "slot_id" not in params


class TestParsePostback:
    def test_book(self):
        assert parse_postback_data("action=book&slot_id=42&party=2") == (
            "book",
            {"slot_id": 42, "party_size": 2},
        )

    def test_book_default_party(self):
        assert parse_postback_data("action=book&slot_id=42") == (
            "book",
            {"slot_id": 42, "party_size": 1},
        )

    def test_cancel(self):
        assert parse_postback_data("action=cancel&reservation_id=7") == (
            "cancel",
            {"reservation_id": 7},
        )

    def test_slots(self):
        assert parse_postback_data("action=slots") == ("slots", {})

    def test_unknown_action(self):
        assert parse_postback_data("action=explode") == (None, {})

    def test_garbage(self):
        assert parse_postback_data("garbage") == (None, {})

    def test_empty(self):
        assert parse_postback_data("") == (None, {})
