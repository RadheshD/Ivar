import pytest

from apps.conversations.channel import Button, OutButtons, OutList, OutProductCard, OutText, Row
from apps.conversations.renderers import render, render_all, truncate


def test_text():
    assert render(OutText("Hello")) == {
        "type": "text",
        "text": {"body": "Hello", "preview_url": False},
    }


def test_buttons():
    message = OutButtons("Pick", (Button("VIEW_CART", "View cart"), Button("HELP", "Help")))
    assert render(message) == {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "Pick"},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "VIEW_CART", "title": "View cart"}},
                    {"type": "reply", "reply": {"id": "HELP", "title": "Help"}},
                ]
            },
        },
    }


def test_list():
    message = OutList(
        "Choose one", "Menu", (Row("A", "Alpha", "first"), Row("B", "Beta")), section_title="S"
    )
    rendered = render(message)
    assert rendered["interactive"]["type"] == "list"
    assert rendered["interactive"]["action"] == {
        "button": "Menu",
        "sections": [
            {
                "rows": [
                    {"id": "A", "title": "Alpha", "description": "first"},
                    {"id": "B", "title": "Beta"},
                ],
                "title": "S",
            }
        ],
    }


def test_whatsapp_limits_are_applied():
    long = "x" * 5000
    buttons = OutButtons(long, (Button("A", "A very long button title"),))
    rows = OutList(long, "A very long list button", (Row("A", "R" * 40, "D" * 100),))
    assert len(render(OutText(long))["text"]["body"]) == 4096
    b = render(buttons)["interactive"]
    assert len(b["body"]["text"]) == 1024
    title = b["action"]["buttons"][0]["reply"]["title"]
    assert len(title) <= 20 and title.endswith("…")
    item = render(rows)["interactive"]
    assert len(item["action"]["button"]) == 20
    row = item["action"]["sections"][0]["rows"][0]
    assert (len(row["title"]), len(row["description"])) == (24, 72)
    assert row["title"].endswith("…")


def test_product_card_with_image():
    add = Button("ACT:addcard:seed-mix--std", "Add to cart")
    order = Button("ACT:ordernow:seed-mix--std", "Order now")
    image_url = "https://ivar-eyw.vercel.app/assets/products/eat/seed-mix.jpg"
    message = OutProductCard(
        "Seed Mix\n₹349", (add, order), image_url=image_url, key="product.card"
    )
    rendered = render(message)
    assert rendered == {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "Seed Mix\n₹349"},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": add.id, "title": add.title}},
                    {"type": "reply", "reply": {"id": order.id, "title": order.title}},
                ]
            },
            "header": {"type": "image", "image": {"link": image_url}},
        },
    }


def test_product_card_without_image_has_no_header():
    message = OutProductCard("Ivar Oats\n₹249", (Button("A", "Add to cart"),))
    rendered = render(message)
    assert "header" not in rendered["interactive"]


def test_product_card_button_count_validated():
    with pytest.raises(ValueError):
        OutProductCard("x", ())
    with pytest.raises(ValueError):
        OutProductCard("x", tuple(Button(str(i), str(i)) for i in range(4)))


def test_limits_on_counts():
    with pytest.raises(ValueError):
        OutButtons("x", tuple(Button(str(i), str(i)) for i in range(4)))
    with pytest.raises(ValueError):
        OutButtons("x", ())
    with pytest.raises(ValueError):
        OutList("x", "y", tuple(Row(str(i), str(i)) for i in range(11)))
    with pytest.raises(ValueError):
        OutList("x", "y", (Row("same", "a"), Row("same", "b")))


def test_reply_id_too_long_is_refused():
    with pytest.raises(ValueError):
        render(OutList("x", "y", (Row("r" * 201, "t"),)))


def test_truncate_keeps_short_text():
    assert truncate("short", 20) == "short"


def test_every_engine_message_renders(chat):
    from .harness import onboard

    onboard(chat)
    for text in ("show me snacks", "oats", "do you have makhana", "2", "show my cart"):
        chat.send(text)
        for payload in render_all(chat.last):
            assert payload["type"] in ("text", "interactive")
