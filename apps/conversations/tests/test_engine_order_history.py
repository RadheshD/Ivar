"""Order history: "My orders" list, order detail with lines, and the words that open them."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from apps.conversations.backend_port import NotAuthorized
from apps.conversations.channel import OutButtons, OutList, OutText
from apps.conversations.fake_backend import FakeBackend
from apps.conversations.messages import ORDER_STATUS_WORDS, en, hi, t, te
from apps.conversations.renderers import INTERACTIVE_BODY_MAX
from apps.iva_common.money import format_money

from .harness import ADDRESS, LANGUAGES, OTHER_WA_ID, WA_ID, Chat, onboard

HISTORY_WORD = {"en": "my orders", "hi": "मेरे ऑर्डर", "te": "నా ఆర్డర్లు"}
# 20:00 UTC is 01:30 the next day in India: the date shown must be the Indian one.
PLACED = datetime(2026, 10, 16, 20, 0, tzinfo=UTC)
PLACED_TEXT = "17 Oct 2026"


def customer_of(chat):
    return chat.engine.backend.get_or_create_customer(chat.whatsapp_id)


def place(backend, customer, items=(("ivar-oats--std", 2),)):
    for variant, quantity in items:
        backend.add_item(customer.id, variant, quantity)
    addresses = backend.list_addresses(customer.id)
    address = addresses[0] if addresses else backend.add_address(customer.id, ADDRESS)
    return backend.checkout(customer.id, address.id, uuid.uuid4().hex)


class StrippedBackend(FakeBackend):
    """A backend that may not know an order's date, total or lines (all optional)."""

    strip_summary = False
    strip_lines = False
    detail_error: Exception | None = None
    last_limit = None

    def list_recent_orders(self, customer_id, limit=5):
        self.last_limit = limit
        views = super().list_recent_orders(customer_id, limit)
        if self.strip_summary:
            views = [replace(v, placed_at=None, total=None, currency=None) for v in views]
        return views

    def get_order_detail(self, customer_id, order_number):
        if self.detail_error:
            raise self.detail_error
        detail = super().get_order_detail(customer_id, order_number)
        if detail is not None and self.strip_lines:
            detail = replace(detail, lines=())
        return detail


@pytest.fixture
def dated():
    return StrippedBackend(clock=lambda: PLACED)


@pytest.fixture
def dchat(make_engine, dated):
    return onboard(Chat(make_engine(dated)))


def status_name(lang, value):
    return t(lang, f"status.order.{value}")


# ----------------------------------------------------------------------- zero orders


@pytest.mark.parametrize("lang", LANGUAGES)
def test_no_orders_gives_the_friendly_message_and_menu(chat, lang):
    onboard(chat, lang)
    chat.send(HISTORY_WORD[lang])
    assert chat.keys == ["order.none"]
    assert [b.title for b in chat.message("order.none").buttons] == [t(lang, "btn.menu")]
    chat.tap("ACT:orders")
    assert chat.keys == ["order.none"]


# ----------------------------------------------------------------------- one order


@pytest.mark.parametrize("lang", LANGUAGES)
def test_one_order_shows_its_detail_directly(make_engine, dated, lang):
    chat = onboard(Chat(make_engine(dated)), lang)
    customer = customer_of(chat)
    placed = place(dated, customer, (("ivar-oats--std", 2), ("quinoa--std", 1)))
    chat.send(HISTORY_WORD[lang])
    assert chat.keys == ["order.status"]
    body = chat.body
    detail = dated.get_order_detail(customer.id, placed.order_number)
    assert len(detail.lines) == 2
    assert t(lang, "order.detail_title", order=placed.order_number, date=PLACED_TEXT) in body
    for line in detail.lines:
        expected = t(
            lang,
            "order.line",
            name=line.name,
            qty=line.quantity,
            total=format_money(line.line_total, detail.currency),
        )
        assert expected in body
    assert t(lang, "order.total", total=format_money(placed.total, placed.currency)) in body
    assert t(lang, "order.lines_note") in body
    status = status_name(lang, "PENDING")
    assert t(lang, "order.status", order=placed.order_number, status=status) in body
    assert t(lang, "order.eta_na") in body and t(lang, "order.tracking_na") in body


# ----------------------------------------------------------------------- several orders


def test_six_orders_list_only_the_latest_five(dchat, dated):
    customer = customer_of(dchat)
    numbers = [place(dated, customer).order_number for _ in range(6)]
    dchat.send("my orders")
    assert dated.last_limit == 5
    assert dchat.keys == ["orders.title"]
    [menu] = dchat.last
    assert isinstance(menu, OutList) and menu.button == t("en", "list.choose")
    newest_five = numbers[:0:-1]  # newest first; the oldest is left out
    assert [r.title for r in menu.rows] == newest_five
    assert [r.id for r in menu.rows] == [f"ORDER_STATUS:{n}" for n in newest_five]
    assert numbers[0] not in [r.title for r in menu.rows]


@pytest.mark.parametrize("lang", LANGUAGES)
def test_row_text_is_built_from_port_data_only(make_engine, dated, lang):
    chat = onboard(Chat(make_engine(dated)), lang)
    customer = customer_of(chat)
    first = place(dated, customer)
    second = place(dated, customer, (("quinoa--std", 3),))
    dated.simulate_status(first.order_number, "OUT_FOR_DELIVERY")
    chat.send(HISTORY_WORD[lang])
    rows = {r.title: r.description for r in chat.rows}
    for result, status in ((first, "OUT_FOR_DELIVERY"), (second, "PENDING")):
        view = dated.get_order(customer.id, result.order_number)
        assert rows[result.order_number] == t(
            lang,
            "orders.row_desc",
            date=PLACED_TEXT,
            total=format_money(view.total, view.currency),
            status=status_name(lang, status),
        )
    assert "Green Park" not in repr(chat.last)


def test_a_row_without_date_or_total_shows_only_the_status(dchat, dated):
    customer = customer_of(dchat)
    place(dated, customer)
    place(dated, customer)
    dated.strip_summary = True
    dchat.send("my orders")
    assert {r.description for r in dchat.rows} == {status_name("en", "PENDING")}


# ----------------------------------------------------------------------- opening a row


@pytest.mark.parametrize("lang", LANGUAGES)
def test_tapping_a_row_opens_the_detail_with_lines_and_no_invented_delivery(
    make_engine, dated, lang
):
    chat = onboard(Chat(make_engine(dated)), lang)
    customer = customer_of(chat)
    placed = place(dated, customer, (("ivar-oats--std", 2),))
    place(dated, customer)
    chat.tap(f"ORDER_STATUS:{placed.order_number}")
    assert chat.keys == ["order.status"]
    body = chat.body
    assert placed.order_number in body and "•" in body
    assert t(lang, "order.eta_na") in body and t(lang, "order.tracking_na") in body
    assert t(lang, "order.payment", status=t(lang, "status.payment.PENDING")) in body


def test_eta_and_tracking_show_only_when_the_port_returns_them(dchat, dated):
    customer = customer_of(dchat)
    placed = place(dated, customer)
    dated.simulate_payment_confirmed(placed.order_number)
    dated.simulate_status(
        placed.order_number,
        "OUT_FOR_DELIVERY",
        eta_start=datetime(2026, 10, 18, 4, 0, tzinfo=UTC),
        tracking_id="TRK-1",
    )
    dchat.send("where is my order")
    body = dchat.body
    assert "TRK-1" in body and "18-10-2026 09:30" in body
    assert t("en", "order.eta_na") not in body and t("en", "order.tracking_na") not in body
    assert "•" in body  # the typed "where is my order" path uses the same extended view


def test_an_order_without_stored_lines_says_so(dchat, dated):
    customer = customer_of(dchat)
    placed = place(dated, customer)
    dated.strip_lines = True
    dchat.tap(f"ORDER_STATUS:{placed.order_number}")
    body = dchat.body
    assert t("en", "order.lines_na") in body
    assert "•" not in body and t("en", "order.lines_note") not in body
    assert t("en", "order.total", total=format_money(placed.total, placed.currency)) in body


def test_a_long_order_keeps_status_payment_and_delivery_in_the_buttons_message(dchat, dated):
    customer = customer_of(dchat)
    items = []
    for product in dated.list_products(limit=50)[0]:
        detail = dated.get_product(product.id)
        if detail.in_stock and detail.variants[0].in_stock:
            items.append((detail.variants[0].id, 1))
    place(dated, customer, tuple(items[:40]))
    dchat.send("where is my order")
    assert dchat.keys == ["order.detail_title", "order.status"]
    text, buttons = dchat.last
    assert isinstance(text, OutText) and isinstance(buttons, OutButtons)
    assert text.body.count("•") == 40
    assert len(text.body) + len(buttons.body) > INTERACTIVE_BODY_MAX  # one message would not fit
    assert "Payment:" in buttons.body and t("en", "order.eta_na") in buttons.body


# ----------------------------------------------------------------------- privacy


def test_another_customers_order_is_not_shown(dchat, dated, make_engine):
    mine = place(dated, customer_of(dchat))
    other = onboard(Chat(make_engine(dated), OTHER_WA_ID), name="Asha")
    other.send("my orders")
    assert other.keys == ["order.none"]
    other.tap(f"ORDER_STATUS:{mine.order_number}")
    assert other.keys == ["order.not_found"]
    assert "•" not in other.body


def test_not_authorized_for_the_detail_is_treated_as_not_found(dchat, dated):
    placed = place(dated, customer_of(dchat))
    dated.detail_error = NotAuthorized("nope")
    dchat.tap(f"ORDER_STATUS:{placed.order_number}")
    assert dchat.keys == ["order.not_found"]


def test_no_address_is_ever_shown(dchat, dated):
    placed = place(dated, customer_of(dchat))
    place(dated, customer_of(dchat))
    dchat.send("my orders")
    assert "Green Park" not in repr(dchat.last) and "Madhapur" not in repr(dchat.last)
    dchat.tap(f"ORDER_STATUS:{placed.order_number}")
    assert "Green Park" not in dchat.body and "Madhapur" not in dchat.body


# ----------------------------------------------------------------------- routing


@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_menu_row_opens_history(chat, lang):
    onboard(chat, lang)
    chat.send("menu")
    row = next(r for r in chat.rows if r.id == "ACT:orders")
    assert row.title == t(lang, "menu.orders")


def test_menu_row_text_in_english():
    assert en.MESSAGES["menu.orders"] == "My orders"


@pytest.mark.parametrize(
    "lang, word",
    [("en", w) for w in en.ORDER_HISTORY_WORDS]
    + [("hi", w) for w in hi.ORDER_HISTORY_WORDS]
    + [("te", w) for w in te.ORDER_HISTORY_WORDS],
)
def test_every_history_word_opens_the_list(make_engine, dated, lang, word):
    chat = onboard(Chat(make_engine(dated)), lang)
    customer = customer_of(chat)
    place(dated, customer)
    place(dated, customer)
    chat.send(word)
    assert chat.keys == ["orders.title"]


def test_the_requested_english_words_are_covered():
    assert {"my orders", "order history", "orders"} <= set(en.ORDER_HISTORY_WORDS)


def test_history_words_never_overlap_the_status_words():
    from apps.conversations.messages import ORDER_HISTORY_WORDS

    assert not set(ORDER_HISTORY_WORDS) & set(ORDER_STATUS_WORDS)


def test_my_order_still_means_the_latest_order(dchat, dated):
    customer = customer_of(dchat)
    place(dated, customer)
    latest = place(dated, customer)
    dchat.send("where is my order")
    assert dchat.keys == ["order.status"] and latest.order_number in dchat.body


def test_history_at_the_address_step_is_not_saved_as_an_address(dchat, dated):
    customer = customer_of(dchat)
    place(dated, customer)
    place(dated, customer)
    dchat.send("add 2 packets makhana")
    dchat.send("checkout")
    assert len(dated.list_addresses(customer.id)) == 1
    dchat.send("orders")
    assert dchat.keys == ["orders.title"]
    assert len(dated.list_addresses(customer.id)) == 1


def test_the_session_is_not_needed_for_history(dchat, dated):
    customer = customer_of(dchat)
    place(dated, customer)
    place(dated, customer)
    dchat.engine.store.delete(WA_ID)
    dchat.tap("ACT:orders")
    assert dchat.keys == ["orders.title"]
