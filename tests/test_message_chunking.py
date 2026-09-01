"""
Tests for sending each ticket as its own Telegram message.

The digest grew past Telegram's 4096-character limit once the draw block began
listing each ticket's own legs, and /predict reported "Prediction failed" for
bets that had in fact already been booked. These pin both halves of the fix:
one message per ticket, and codes that survive a send failing anyway.
"""

from __future__ import annotations

from types import SimpleNamespace

from notifiers.telegram_bot import (
    TELEGRAM_MAX_CHARS,
    TICKET_SEPARATOR,
    _collect_booking_codes,
    _strip_html,
    split_html_message,
)


def _ticket(title: str, legs: int, width: int = 95) -> str:
    body = "\n".join(f"<b>{i}.</b> " + "x" * width for i in range(1, legs + 1))
    return f"\n{TICKET_SEPARATOR}\n<b>{title}</b>\n{body}"


def _full_digest() -> str:
    return (
        "<b>Daily AI Predictions</b>\n"
        + _ticket("TICKET 1: TOP 10 BANKERS", 10)
        + _ticket("TICKET 2: TOP 20 MEGA ACCUMULATOR", 20)
        + _ticket("TICKET 4: 2 ODDS BANKER", 8)
        + _ticket("TICKET 3: DAILY DRAWS", 25)
    )


def test_message_without_tickets_is_left_alone():
    assert split_html_message("<b>hi</b>") == ["<b>hi</b>"]


def test_each_ticket_becomes_its_own_message():
    digest = _full_digest()
    assert len(digest) > TELEGRAM_MAX_CHARS  # the case that broke /predict
    messages = split_html_message(digest)
    assert len(messages) == 4  # T1, T2, T4, T3 — one each
    assert all(len(m) <= TELEGRAM_MAX_CHARS for m in messages)
    for msg in messages:
        assert msg.count(TICKET_SEPARATOR) == 1


def test_tickets_split_even_when_the_digest_would_fit():
    """Splitting is about readability too, not only the length limit."""
    small = _ticket("TICKET 1", 2) + _ticket("TICKET 2", 2)
    assert len(small) < TELEGRAM_MAX_CHARS
    assert len(split_html_message(small)) == 2


def test_split_loses_no_content():
    digest = _full_digest()
    rejoined = "".join(split_html_message(digest)).replace("\n", "")
    assert rejoined == digest.replace("\n", "")


def test_header_rides_with_the_first_ticket():
    """The preamble is not a ticket; it should not be a lonely message."""
    messages = split_html_message(_full_digest())
    assert "Daily AI Predictions" in messages[0]
    assert "TICKET 1" in messages[0]


def test_oversized_single_line_is_cut_without_orphaning_a_tag():
    line = "<b>" + "y" * (TELEGRAM_MAX_CHARS * 2) + "</b>"
    chunks = split_html_message(line)
    assert all(len(c) <= TELEGRAM_MAX_CHARS for c in chunks)
    assert "".join(chunks) == line
    for chunk in chunks:
        # No chunk ends inside a "<...>".
        assert chunk.count("<") == chunk.count(">")


def test_one_huge_ticket_falls_back_to_line_splitting():
    """A single ticket too long on its own still has to be broken up."""
    huge = _ticket("TICKET 2", 200)
    messages = split_html_message(huge)
    assert len(messages) > 1
    assert all(len(m) <= TELEGRAM_MAX_CHARS for m in messages)
    assert "".join(messages).replace("\n", "") == huge.replace("\n", "")


def test_strip_html_gives_a_plain_text_fallback():
    assert _strip_html("<b>Code:</b> <code>ABC123</code> &amp; more") == "Code: ABC123 & more"


def _book(code: str | None, success: bool = True):
    return SimpleNamespace(booking_result=SimpleNamespace(success=success, booking_code=code))


def test_collect_booking_codes_from_dual_result():
    dual = SimpleNamespace(
        tier_10=_book("AAA111"),
        tier_20=_book("BBB222"),
        two_odds=_book("CCC333"),
        draws=SimpleNamespace(
            tickets=[
                SimpleNamespace(
                    ticket=SimpleNamespace(label="5-fold"),
                    booking_result=SimpleNamespace(success=True, booking_code="DDD444"),
                )
            ]
        ),
    )
    assert _collect_booking_codes(dual=dual) == [
        ("Top 10", "AAA111"),
        ("Top 20", "BBB222"),
        ("2 Odds", "CCC333"),
        ("5-fold", "DDD444"),
    ]


def test_collect_booking_codes_skips_failed_and_missing():
    dual = SimpleNamespace(
        tier_10=_book(None),
        tier_20=_book("BBB222", success=False),
        two_odds=None,
        draws=None,
    )
    assert _collect_booking_codes(dual=dual) == []


class _StubMessage:
    """Stands in for the '🧠 Analyzing...' placeholder message."""

    chat_id = 999

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.edits: list[tuple[str, object]] = []

    async def edit_text(self, text, parse_mode=None, disable_web_page_preview=None, reply_markup=None):
        if self.fail:
            raise RuntimeError("Message is too long")
        self.edits.append((text, reply_markup))


class _StubBot:
    def __init__(self, fail_html: bool = False):
        self.fail_html = fail_html
        self.sent: list[tuple[str, object, object]] = []

    async def send_message(self, chat_id, text, parse_mode=None, disable_web_page_preview=None, reply_markup=None):
        if self.fail_html and parse_mode is not None:
            raise RuntimeError("Can't parse entities")
        self.sent.append((text, parse_mode, reply_markup))


def _notifier(bot):
    from notifiers.telegram_bot import TelegramNotifier

    obj = TelegramNotifier.__new__(TelegramNotifier)  # skip network setup in __init__
    obj._bot = bot
    obj._admin_chat_id = 999
    return obj


async def _deliver(bot, status_msg, text, markup="KB"):
    await _notifier(bot)._deliver_long_html(
        chat_id=999, text=text, status_msg=status_msg, reply_markup=markup
    )


def test_delivery_edits_placeholder_then_sends_the_rest():
    import asyncio

    bot, status = _StubBot(), _StubMessage()
    asyncio.run(_deliver(bot, status, _full_digest()))

    messages = split_html_message(_full_digest())
    assert len(status.edits) == 1                      # first ticket replaces the placeholder
    assert status.edits[0][0] == messages[0]
    assert len(bot.sent) == len(messages) - 1          # the rest follow, one per ticket
    assert status.edits[0][1] is None                  # keyboard is not on the first message...
    assert bot.sent[-1][2] == "KB"                     # ...it rides on the last


def test_delivery_falls_back_to_plain_text_when_html_is_refused():
    import asyncio

    bot, status = _StubBot(fail_html=True), _StubMessage(fail=True)
    asyncio.run(_deliver(bot, status, "<b>Code:</b> <code>ABC123</code>"))

    assert len(bot.sent) == 1
    text, parse_mode, _ = bot.sent[0]
    assert parse_mode is None
    assert "ABC123" in text          # the code survives the formatting failure


def test_send_custom_message_accepts_main_pys_call_and_splits():
    """
    main.py passed disable_web_page_preview=, which send_custom_message did not
    accept — a TypeError swallowed by that call site's except, so the startup
    digest never reached Telegram at all.
    """
    import asyncio

    bot = _StubBot()
    notifier = _notifier(bot)
    asyncio.run(
        notifier.send_custom_message(
            text=_full_digest(),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup="KB",
        )
    )
    assert len(bot.sent) == len(split_html_message(_full_digest()))
    assert all(len(text) <= TELEGRAM_MAX_CHARS for text, _, _ in bot.sent)
    assert bot.sent[-1][2] == "KB"


def test_startup_path_signature_matches_the_notifier():
    """Pin the call in main.py against the method it calls."""
    import inspect

    from notifiers.telegram_bot import TelegramNotifier

    sig = inspect.signature(TelegramNotifier.send_custom_message)
    sig.bind(
        None,
        text="x",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=None,
    )


def test_startup_booking_is_off_by_default():
    """A restart is a deploy, not a betting decision."""
    from config.settings import Settings

    s = Settings(telegram_bot_token="1:x", admin_chat_id=1)
    assert s.startup_prediction_autobook is False
    assert s.startup_prediction_enabled is True


def test_startup_settings_read_from_the_environment(monkeypatch):
    from config.settings import Settings

    monkeypatch.setenv("STARTUP_PREDICTION_AUTOBOOK", "true")
    monkeypatch.setenv("STARTUP_PREDICTION_ENABLED", "false")
    s = Settings(telegram_bot_token="1:x", admin_chat_id=1)
    assert s.startup_prediction_autobook is True
    assert s.startup_prediction_enabled is False


def test_digest_never_prints_a_none_booking_code():
    """
    A live Top 20 came back success=True with booking_code=None and the digest
    printed "SportyBet Code: None" — a ticket that looked playable and was not.
    """
    from services.pipeline import PredictionBookingPipeline

    def _pick(i):
        return SimpleNamespace(
            fixture=SimpleNamespace(home_name=f"H{i}", away_name=f"A{i}", label=f"H{i} v A{i}"),
            selection="Over 1.5", market="Over/Under 1.5", probability=0.85,
        )

    dual = SimpleNamespace(
        tier_10=SimpleNamespace(picks=[_pick(1)], booking_result=None),
        tier_20=SimpleNamespace(
            picks=[_pick(2)],
            booking_result=SimpleNamespace(success=True, booking_code=None, total_odds="190.47"),
        ),
        two_odds=None,
        draws=None,
        filter_stats=SimpleNamespace(total=151),
    )
    digest = PredictionBookingPipeline.format_telegram_dual_digest(dual, "2026-09-01")
    assert "None" not in digest
    assert "Code" not in digest
