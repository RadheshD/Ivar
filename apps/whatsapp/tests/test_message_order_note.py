"""Message order: the "nutrition not verified" note (plain text) is held until the carousel before
it has been delivered, so it can never appear above the intro."""

from __future__ import annotations

from types import SimpleNamespace

from apps.conversations.channel import OutButtons, OutCarousel, OutText
from apps.conversations.tests.test_code_only_fixes import build
from apps.whatsapp.delivery import send_replies


class RecordingClient:
    def __init__(self):
        self.sent = []

    def send(self, wa_id, payload):
        self.sent.append(payload)
        return SimpleNamespace(wa_message_id=f"wamid.{len(self.sent)}")


def test_the_nutrition_note_never_overtakes_the_carousel(settings):
    settings.IVA_PRODUCT_IMAGE_BASE_URL = "https://img.example/assets/"  # products get images
    chat, _ = build("off")
    chat.send("high protein under 300")
    replies = chat.last
    assert any(isinstance(m, OutCarousel) for m in replies)
    waited = []
    client = RecordingClient()
    results = send_replies(
        "919876500001", replies, client=client,
        wait_for_status=lambda wamid: waited.append(wamid) or True,
    )  # fmt: skip
    assert len(results) == len(replies)  # every message was sent, in order
    # every text or buttons message that follows a carousel / image card waits for its status
    large_then_text = sum(
        1 for a, b in zip(replies, replies[1:], strict=False)
        if (isinstance(a, OutCarousel) or getattr(a, "image_url", None))
        and isinstance(b, OutButtons | OutText)
    )  # fmt: skip
    assert len(waited) >= large_then_text >= 1
