"""Channel-neutral inbound and outbound messages.

The engine only produces these; ``renderers`` turns them into WhatsApp shapes. Limits
match WhatsApp interactive messages: at most 3 reply buttons, at most 10 list rows.
``key`` names the message template that produced the body (for tests and logs); it is
never shown to the customer.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_BUTTONS = 3
MAX_ROWS = 10
MIN_CAROUSEL_CARDS = 2
MAX_CAROUSEL_CARDS = 10
MAX_CAROUSEL_BUTTONS = 2  # quick-reply buttons per media-carousel card


@dataclass(frozen=True)
class Inbound:
    """One customer message: typed text, or a tapped button/list row."""

    text: str | None = None
    reply_id: str | None = None
    reply_title: str | None = None
    # The WhatsApp contact's own profile name, if the webhook included one. Used only
    # to greet a customer who has no saved name yet; never logged.
    profile_name: str | None = None
    # A native WhatsApp catalog order, already normalised by the webhook parser:
    # ``{"catalog_id", "text", "items": [{"product_id", "quantity", "price", "currency"}],
    # "unreadable": int}``. Prices in it are never trusted.
    order: dict | None = None

    @classmethod
    def typed(cls, text: str) -> Inbound:
        return cls(text=text)

    @classmethod
    def tapped(cls, reply_id: str, title: str | None = None) -> Inbound:
        return cls(reply_id=reply_id, reply_title=title)


@dataclass(frozen=True)
class Button:
    id: str
    title: str


@dataclass(frozen=True)
class Row:
    id: str
    title: str
    description: str | None = None


def _unique_ids(items) -> None:
    ids = [item.id for item in items]
    if len(ids) != len(set(ids)):
        raise ValueError("Reply IDs must be unique within a message.")


@dataclass(frozen=True)
class OutText:
    body: str
    key: str | None = None


@dataclass(frozen=True)
class OutButtons:
    body: str
    buttons: tuple[Button, ...]
    key: str | None = None

    def __post_init__(self):
        if not 1 <= len(self.buttons) <= MAX_BUTTONS:
            raise ValueError(f"A button message needs 1 to {MAX_BUTTONS} buttons.")
        _unique_ids(self.buttons)


@dataclass(frozen=True)
class OutProductCard:
    """One product: an optional image header, a body (name + price) and reply buttons.

    ``image_url`` must be an ``https://`` URL or ``None`` (text-only card); it is never
    validated here (the engine only ever passes a value already checked against the
    Backend Port contract).
    """

    body: str
    buttons: tuple[Button, ...]
    image_url: str | None = None
    key: str | None = None

    def __post_init__(self):
        if not 1 <= len(self.buttons) <= MAX_BUTTONS:
            raise ValueError(f"A product card needs 1 to {MAX_BUTTONS} buttons.")
        _unique_ids(self.buttons)


@dataclass(frozen=True)
class CarouselCard:
    """One carousel card: a required image header, a short body and quick-reply buttons.

    Every card of a carousel must carry the same number of buttons (a Meta rule).
    """

    image_url: str
    body: str
    buttons: tuple[Button, ...]

    def __post_init__(self):
        if not self.image_url.startswith("https://"):
            raise ValueError("A carousel card needs an https image.")
        if not 1 <= len(self.buttons) <= MAX_CAROUSEL_BUTTONS:
            raise ValueError(f"A carousel card needs 1 to {MAX_CAROUSEL_BUTTONS} buttons.")
        _unique_ids(self.buttons)


@dataclass(frozen=True)
class OutCarousel:
    """An interactive media carousel (2 to 10 cards) with a short body line.

    ``fallback`` holds the same products as ordinary messages (an intro and one
    ``OutProductCard`` each). If WhatsApp rejects the carousel, delivery sends those
    instead, so no product is ever dropped.
    """

    body: str
    cards: tuple[CarouselCard, ...]
    fallback: tuple[Outbound, ...] = ()
    key: str | None = None

    def __post_init__(self):
        if not MIN_CAROUSEL_CARDS <= len(self.cards) <= MAX_CAROUSEL_CARDS:
            raise ValueError(
                f"A carousel needs {MIN_CAROUSEL_CARDS} to {MAX_CAROUSEL_CARDS} cards."
            )
        if len({len(card.buttons) for card in self.cards}) != 1:
            raise ValueError("Every carousel card needs the same number of buttons.")
        _unique_ids([b for card in self.cards for b in card.buttons])


@dataclass(frozen=True)
class OutList:
    body: str
    button: str  # label of the button that opens the list
    rows: tuple[Row, ...]
    key: str | None = None
    section_title: str | None = None

    def __post_init__(self):
        if not 1 <= len(self.rows) <= MAX_ROWS:
            raise ValueError(f"A list message needs 1 to {MAX_ROWS} rows.")
        _unique_ids(self.rows)


@dataclass(frozen=True)
class OutCTA:
    """A WhatsApp ``cta_url`` interactive message: one tappable link button."""

    body: str
    display_text: str
    url: str
    key: str | None = None

    def __post_init__(self):
        if not self.url.startswith("https://"):
            raise ValueError("CTA URL must be https.")


@dataclass(frozen=True)
class OutTemplate:
    """A pre-approved WhatsApp template message, the only kind allowed outside the 24-hour
    customer-service window. ``name`` comes from settings (never guessed); the optional body
    parameters fill the template's numbered variables in order."""

    name: str
    language_code: str
    body_parameters: tuple[str, ...] = ()
    key: str | None = None

    def __post_init__(self):
        if not self.name or not all(ch.islower() or ch.isdigit() or ch == "_" for ch in self.name):
            raise ValueError("Template names are lowercase letters, digits and underscores.")
        if not self.language_code:
            raise ValueError("A template needs a language code.")


Outbound = OutText | OutButtons | OutList | OutCTA | OutProductCard | OutCarousel | OutTemplate
