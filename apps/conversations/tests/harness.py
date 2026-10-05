"""Transcript-test helpers: drive the engine like a customer and inspect replies."""

from __future__ import annotations

from apps.conversations.backend_port import BackendUnavailable
from apps.conversations.channel import (
    Inbound,
    OutButtons,
    OutCarousel,
    OutList,
    OutProductCard,
)
from apps.conversations.messages import t

WA_ID = "919876500001"
OTHER_WA_ID = "919876500002"
ADDRESS = "Flat 12, Green Park Road, Madhapur, Hyderabad 500081"

# One phrase per language for the same request; used to run a flow in all three.
PHRASES = {
    "en": {
        "search": "do you have makhana",
        "snacks": "show me snacks",
        "add": "add 2 packets makhana",
        "update": "change makhana quantity to 3",
        "remove": "remove the makhana",
        "cart": "show my cart",
        "checkout": "checkout",
        "order": "where is my order",
        "payment": "did my payment go through",
        "track": "track my order",
    },
    "te": {
        "search": "మఖానా ఉందా",
        "snacks": "నాకు స్నాక్స్ కావాలి",
        "add": "రెండు ప్యాకెట్లు మఖానా జోడించు",
        "update": "మఖానా మూడు కి మార్చు",
        "remove": "మఖానా తీసేయి",
        "cart": "నా కార్ట్ చూపించు",
        "checkout": "ఆర్డర్ చేయి",
        "order": "నా ఆర్డర్ ఎక్కడ ఉంది",
        "payment": "నా పేమెంట్ అయ్యిందా",
        "track": "నా ఆర్డర్ ట్రాక్ చేయి",
    },
    "hi": {
        "search": "मखाना है क्या",
        "snacks": "मुझे स्नैक्स चाहिए",
        "add": "दो पैकेट मखाना जोड़ो",
        "update": "मखाना की मात्रा तीन करो",
        "remove": "मखाना हटाओ",
        "cart": "मेरा कार्ट दिखाओ",
        "checkout": "ऑर्डर कर दो",
        "order": "मेरा ऑर्डर कहाँ है",
        "payment": "मेरा पेमेंट हुआ क्या",
        "track": "मेरा ऑर्डर ट्रैक करो",
    },
}
LANGUAGES = tuple(PHRASES)


def in_script(text: str, lang: str) -> bool:
    """True when ``text`` is written in ``lang``'s script (English: no Indic script)."""
    telugu = any("ఀ" <= ch <= "౿" for ch in text)
    devanagari = any("ऀ" <= ch <= "ॿ" for ch in text)
    return {"te": telugu, "hi": devanagari, "en": not (telugu or devanagari)}[lang]


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Chat:
    """One customer talking to the engine."""

    def __init__(self, engine, whatsapp_id: str = WA_ID):
        self.engine = engine
        self.whatsapp_id = whatsapp_id
        self.last: list = []

    def send(self, text: str, profile_name: str | None = None) -> Chat:
        inbound = Inbound(text=text, profile_name=profile_name)
        self.last = self.engine.handle(self.whatsapp_id, inbound)
        return self

    def tap(self, reply_id: str, title: str | None = None, profile_name: str | None = None) -> Chat:
        inbound = Inbound(reply_id=reply_id, reply_title=title, profile_name=profile_name)
        self.last = self.engine.handle(self.whatsapp_id, inbound)
        return self

    @property
    def keys(self) -> list[str]:
        return [m.key for m in self.last]

    @property
    def ids(self) -> list[str]:
        found = []
        for m in self.last:
            if isinstance(m, (OutButtons, OutProductCard)):
                found.extend(b.id for b in m.buttons)
            elif isinstance(m, OutCarousel):
                found.extend(b.id for card in m.cards for b in card.buttons)
            elif isinstance(m, OutList):
                found.extend(r.id for r in m.rows)
        return found

    @property
    def rows(self) -> list:
        return [r for m in self.last if isinstance(m, OutList) for r in m.rows]

    @property
    def cards(self) -> list[OutProductCard]:
        return [m for m in self.last if isinstance(m, OutProductCard)]

    @property
    def carousel(self) -> OutCarousel | None:
        return next((m for m in self.last if isinstance(m, OutCarousel)), None)

    @property
    def body(self) -> str:
        return "\n".join(m.body for m in self.last)

    def message(self, key: str):
        return next(m for m in self.last if m.key == key)

    def session(self):
        return self.engine.store.load(self.whatsapp_id)


def onboard(chat: Chat, lang: str = "en", diet: str = "skip", name="Ravi", city="Hyderabad"):
    """Get a fresh ``Chat`` past onboarding (language only) and, for test convenience,
    set name/city/diet directly through the port — the bot itself no longer asks for
    any of these during onboarding."""
    chat.send("hi")
    chat.tap(f"LANGUAGE_SELECT:{lang}")
    customer = chat.engine.backend.get_or_create_customer(chat.whatsapp_id)
    assert customer.onboarding_complete, chat.keys
    changes = {}
    if name is not None and customer.name != name:
        changes["name"] = name
    if city is not None and customer.city != city:
        changes["city"] = city
    if diet and diet != "skip" and customer.diet != diet:
        changes["diet"] = diet
    if changes:
        chat.engine.backend.update_profile(customer.id, **changes)
    return chat


def checkout_to_payment(chat: Chat, lang: str = "en") -> str:
    """From an onboarded customer: add, check out with a new address, confirm."""
    chat.send(PHRASES[lang]["add"])
    assert "cart.added" in chat.keys, chat.keys
    chat.send(PHRASES[lang]["checkout"])
    assert "checkout.type_address" in chat.keys, chat.keys
    chat.send(ADDRESS)
    assert "checkout.confirm" in chat.keys, chat.keys
    chat.tap("ACT:confirm")
    assert "checkout.created" in chat.keys, chat.keys
    return chat.session().order_number


def text_for(lang: str, key: str, **values) -> str:
    return t(lang, key, **values)


class FlakyBackend:
    """Wraps a backend; chosen operations fail. Everything else is delegated.

    ``fail[op] = exception`` raises before the call; ``fail_after[op] = exception``
    performs the real call first and then raises (a lost response).
    """

    def __init__(self, inner):
        self.inner = inner
        self.fail: dict[str, Exception] = {}
        self.fail_after: dict[str, Exception] = {}
        self.times: dict[str, int] = {}

    def __getattr__(self, name):
        target = getattr(self.inner, name)
        if not callable(target):
            return target

        def call(*args, **kwargs):
            if name in self.fail and self._consume(name):
                raise self.fail[name]
            result = target(*args, **kwargs)
            if name in self.fail_after and self._consume(name):
                raise self.fail_after[name]
            return result

        return call

    def _consume(self, name: str) -> bool:
        remaining = self.times.get(name)
        if remaining is None:
            return True
        if remaining <= 0:
            return False
        self.times[name] = remaining - 1
        return True


OUTAGE = BackendUnavailable("simulated outage")


def write_fixture(tmp_path, mutate) -> str:
    """Copy the catalog fixture into ``tmp_path``, let ``mutate(data)`` change it, and
    return the path (for ``FakeBackend(fixture_path=...)``)."""
    import copy
    import json

    from apps.conversations.fake_backend import FIXTURE_PATH

    with open(FIXTURE_PATH, encoding="utf-8") as handle:
        data = copy.deepcopy(json.load(handle))
    mutate(data)
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def resize_category(data: dict, category_id: str, count: int) -> None:
    """Make ``category_id`` hold exactly ``count`` products (trim, or clone the first)."""
    import copy

    products = data["products"]
    mine = [p for p in products if p["category_id"] == category_id]
    for extra in mine[count:]:
        products.remove(extra)
    template = mine[0]
    for n in range(len(mine), count):
        clone = copy.deepcopy(template)
        clone["id"] = f"{template['id']}-x{n}"
        clone["name"] = f"{template['name']} {n}"
        for variant in clone["variants"]:
            variant["id"] = f"{clone['id']}--{variant['id'].split('--')[-1]}"
        products.append(clone)
