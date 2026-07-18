"""line_webhook 套件:事件分派(follow/unfollow/auto-reply/主分派;純搬移自 599-841,908-1014)。"""
import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.line_client import (
    LineProfileClient,
    LineReplyClient,
)
from saas_mvp.models.line_channel_config import (
    InvalidTargetLangError,
    LineChannelConfig,
    validate_target_lang,
)
from saas_mvp.models.line_webhook_event import (
    LineWebhookEventStage,
)
from saas_mvp.models.customer import Customer, upsert_customer_from_line
from saas_mvp.models.line_user_lang import get_user_lang, upsert_user_lang
from saas_mvp.quota import has_char_quota, has_quota, increment_usage
from saas_mvp.translation import Translator
from saas_mvp.translation.commands import parse_lang_command
from saas_mvp.services import auto_reply as auto_reply_svc
from saas_mvp.services import flex_menu as flex_menu_svc
from saas_mvp.services import line_chat as line_chat_svc
from saas_mvp.models.auto_reply_rule import REPLY_TYPE_FLEX, REPLY_TYPE_TEXT


from saas_mvp.routers.line_webhook._shared import (
    _log, _QUOTA_EXCEEDED_MSG,
)
from saas_mvp.routers.line_webhook.replies import (
    _DEFAULT_WELCOME_BOOKING,
    _DEFAULT_WELCOME_GENERIC,
    _DEFAULT_WELCOME_TRANSLATION,
    _WELCOME_QUICK_REPLY,
    _handle_booking_event,
    _resolve_display_name,
    _translate_sync,
)


def _handle_line_event(
    db: Session,
    tenant_id: int,
    plan: str,
    default_target_lang: str,
    access_token: str,
    event: dict,
    event_idx: int,
    translator: Translator,
    line_client: LineReplyClient,
    stage_holder: list[str] | None = None,
    bot_mode: str = "translation",
    profile_client: LineProfileClient | None = None,
) -> str:
    """處理單筆 LINE event，回傳最後完成的處理階段。"""
    stage = LineWebhookEventStage.CLAIMED.value
    if stage_holder is not None:
        stage_holder[0] = stage

    delivery_ctx = event.get("deliveryContext") or {}
    if delivery_ctx.get("isRedelivery") is True:
        _log.info(
            "LINE event redelivery flag observed for tenant %d event_idx=%d; using webhookEventId for idempotency",
            tenant_id,
            event_idx,
        )

    # ── 好友事件：三種 bot_mode 一體處理（unfollow 的顧客檔回寫不分模式） ────
    early_etype = event.get("type")
    if early_etype == "follow":
        return _handle_follow_event(
            db,
            tenant_id,
            access_token,
            event,
            line_client,
            stage_holder,
            profile_client,
            bot_mode,
        )
    if early_etype == "unfollow":
        return _handle_unfollow_event(db, tenant_id, event, stage_holder)

    # ── bot_mode 分流：booking/auto_reply 各自處理；translation（預設）維持現狀 ─
    if bot_mode == "booking":
        return _handle_booking_event(
            db,
            tenant_id,
            access_token,
            event,
            line_client,
            stage_holder,
            profile_client,
        )
    if bot_mode == "auto_reply":
        return _handle_auto_reply_event(
            db,
            tenant_id,
            access_token,
            event,
            line_client,
            stage_holder,
        )

    event_type = event.get("type")
    if event_type != "message":
        return stage

    message = event.get("message", {})
    if message.get("type") != "text":
        return stage

    text = message.get("text", "")
    reply_token = event.get("replyToken", "")
    line_user_id = event.get("source", {}).get("userId", "")

    lang_code, remaining_text = parse_lang_command(text)

    if lang_code:
        try:
            validate_target_lang(lang_code)
        except InvalidTargetLangError:
            line_client.reply(
                reply_token,
                f"無效的語言代碼：{lang_code!r}，請使用 BCP-47 格式（例如：ja、en、zh-TW）",
                access_token=access_token,
            )
            return stage

        if not remaining_text:
            if line_user_id:
                upsert_user_lang(db, tenant_id, line_user_id, lang_code)
            line_client.reply(
                reply_token,
                f"語言已切換為：{lang_code}",
                access_token=access_token,
            )
            return stage

    if lang_code:
        target_lang = lang_code
    elif line_user_id:
        target_lang = get_user_lang(db, tenant_id, line_user_id) or default_target_lang
    else:
        target_lang = default_target_lang

    translate_text = remaining_text if lang_code else text

    stage = LineWebhookEventStage.QUOTA_CHECKED.value
    if stage_holder is not None:
        stage_holder[0] = stage
    if not has_quota(db, tenant_id, plan):
        line_client.reply(reply_token, _QUOTA_EXCEEDED_MSG, access_token=access_token)
        return stage

    if not has_char_quota(db, tenant_id, plan):
        line_client.reply(reply_token, _QUOTA_EXCEEDED_MSG, access_token=access_token)
        return stage

    result = _translate_sync(translator, translate_text, target_lang)
    stage = LineWebhookEventStage.TRANSLATED.value
    if stage_holder is not None:
        stage_holder[0] = stage
    if result.skipped:
        _log.info(
            "skip same-language LINE event for tenant %d event_idx=%d detected=%s target=%s",
            tenant_id,
            event_idx,
            result.detected_lang,
            target_lang,
        )
        return stage

    # ── 6c. 回覆（失敗會向上拋；此時尚未計量，不會白扣） ────────────────────
    # reply 是阻塞 I/O，但 _process_events 是 sync 函式，BackgroundTasks 會透過
    # run_in_threadpool 放到 threadpool 執行，已移出 event loop；不需要再包
    # asyncio.to_thread。sync 函式內再包一層屬冗餘雙重包裝，反而多佔 thread。
    line_client.reply(reply_token, result.text, access_token=access_token)
    stage = LineWebhookEventStage.REPLY_SENT.value
    if stage_holder is not None:
        stage_holder[0] = stage

    increment_usage(db, tenant_id, plan, chars=len(result.text))
    stage = LineWebhookEventStage.USAGE_INCREMENTED.value
    if stage_holder is not None:
        stage_holder[0] = stage
    return stage


def _handle_follow_event(
    db: Session,
    tenant_id: int,
    access_token: str,
    event: dict,
    line_client: LineReplyClient,
    stage_holder: list[str] | None = None,
    profile_client: LineProfileClient | None = None,
    bot_mode: str = "translation",
) -> str:
    """follow（加好友/解除封鎖）：建/更新顧客檔 + 回歡迎訊息。

    顧客檔 upsert 為冪等（LINE redelivery / 重複 follow 安全）；歡迎訊息
    文案取租戶自訂 welcome_message，NULL/空白時依 bot_mode 用內建預設。
    booking 模式附「開始預約/查看時段/我的預約」quick-reply 降低第一步門檻。
    """
    stage = LineWebhookEventStage.CLAIMED.value
    if stage_holder is not None:
        stage_holder[0] = stage

    line_user_id = event.get("source", {}).get("userId", "")
    reply_token = event.get("replyToken", "")

    if line_user_id:
        # follow 可取 profile（好友狀態必然成立）；失敗降級 None 不阻擋建檔。
        display_name = _resolve_display_name(profile_client, line_user_id, access_token)
        customer = upsert_customer_from_line(
            db,
            tenant_id=tenant_id,
            line_user_id=line_user_id,
            display_name=display_name,
            bump_booking=False,
        )
        now = datetime.datetime.now(datetime.timezone.utc)
        customer.line_followed = True
        customer.line_followed_at = now
        db.commit()

    if not reply_token:
        return stage

    cfg = db.execute(
        select(LineChannelConfig).where(LineChannelConfig.tenant_id == tenant_id)
    ).scalar_one_or_none()
    custom = (cfg.welcome_message or "").strip() if cfg is not None else ""

    quick_reply = None
    if bot_mode == "booking":
        text = custom or _DEFAULT_WELCOME_BOOKING
        quick_reply = _WELCOME_QUICK_REPLY
    elif bot_mode == "auto_reply":
        text = custom or _DEFAULT_WELCOME_GENERIC
    else:
        text = custom or _DEFAULT_WELCOME_TRANSLATION

    # 顧客檔已 commit；先標 REPLY_SENT 再 reply（reply 失敗不重試，避免
    # redelivery 重複轟炸歡迎訊息——與 booking 路徑同語意）。
    stage = LineWebhookEventStage.REPLY_SENT.value
    if stage_holder is not None:
        stage_holder[0] = stage
    line_client.reply(reply_token, text, access_token=access_token, quick_reply=quick_reply)
    return stage


def _handle_unfollow_event(
    db: Session,
    tenant_id: int,
    event: dict,
    stage_holder: list[str] | None = None,
) -> str:
    """unfollow（封鎖/解除好友）：標記顧客不可推播。

    商業關鍵：行銷推播（marketing.run_campaign）藉 line_followed=False 跳過
    此顧客，不對推不到的人白扣推播額度。unfollow 事件無 replyToken，不回覆。
    """
    stage = LineWebhookEventStage.CLAIMED.value
    if stage_holder is not None:
        stage_holder[0] = stage

    line_user_id = event.get("source", {}).get("userId", "")
    if not line_user_id:
        return stage

    customer = db.execute(
        select(Customer).where(
            Customer.tenant_id == tenant_id,
            Customer.line_user_id == line_user_id,
        )
    ).scalar_one_or_none()
    if customer is not None:
        customer.line_followed = False
        customer.line_followed_at = datetime.datetime.now(datetime.timezone.utc)
        db.commit()
    return stage


def _record_line_message_best_effort(
    db: Session,
    *,
    tenant_id: int,
    line_user_id: str,
    text: str,
    direction: str,
) -> None:
    if not line_user_id or not text:
        return
    try:
        if direction == "in":
            line_chat_svc.record_inbound(
                db, tenant_id=tenant_id, line_user_id=line_user_id, text=text
            )
        else:
            line_chat_svc.record_outbound(
                db, tenant_id=tenant_id, line_user_id=line_user_id, text=text
            )
    except Exception:  # noqa: BLE001 - 對話紀錄不得阻斷 webhook 主流程
        db.rollback()
        _log.warning(
            "failed to record LINE %s message for tenant %d",
            direction,
            tenant_id,
            exc_info=True,
        )


def _handle_auto_reply_event(
    db: Session,
    tenant_id: int,
    access_token: str,
    event: dict,
    line_client: LineReplyClient,
    stage_holder: list[str] | None = None,
) -> str:
    """auto_reply 模式：關鍵字規則命中才回覆；未命中靜默。"""
    stage = LineWebhookEventStage.CLAIMED.value
    if stage_holder is not None:
        stage_holder[0] = stage

    if event.get("type") != "message":
        return stage

    message = event.get("message", {})
    if message.get("type") != "text":
        return stage

    text = message.get("text", "")
    reply_token = event.get("replyToken", "")
    line_user_id = event.get("source", {}).get("userId", "")

    _record_line_message_best_effort(
        db,
        tenant_id=tenant_id,
        line_user_id=line_user_id,
        text=text,
        direction="in",
    )

    rules = auto_reply_svc.list_rules(db, tenant_id=tenant_id, active_only=True)
    rule = auto_reply_svc.match(rules, text)
    if rule is None:
        return stage

    if rule.reply_type == REPLY_TYPE_TEXT:
        reply_text = rule.reply_text or ""
        if not reply_text:
            return stage
        line_client.reply(reply_token, reply_text, access_token=access_token)
        _record_line_message_best_effort(
            db,
            tenant_id=tenant_id,
            line_user_id=line_user_id,
            text=reply_text,
            direction="out",
        )
    elif rule.reply_type == REPLY_TYPE_FLEX and rule.flex_menu_id is not None:
        menu = flex_menu_svc.get_menu(
            db, tenant_id=tenant_id, menu_id=rule.flex_menu_id
        )
        cards = flex_menu_svc.list_cards(
            db, tenant_id=tenant_id, menu_id=rule.flex_menu_id
        )
        payload = flex_menu_svc.build_flex_payload(menu, cards)
        alt_text = payload.get("altText", "選單")
        line_client.reply_flex(
            reply_token,
            alt_text,
            payload["contents"],
            access_token=access_token,
        )
        _record_line_message_best_effort(
            db,
            tenant_id=tenant_id,
            line_user_id=line_user_id,
            text=alt_text,
            direction="out",
        )
    else:
        return stage

    stage = LineWebhookEventStage.REPLY_SENT.value
    if stage_holder is not None:
        stage_holder[0] = stage
    return stage
