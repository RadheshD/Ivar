"""Closing messages: "thanks", "bye" and a light "ok". Rules only: no model, no catalog, no state.

THE PHRASE LISTS BELOW ARE THE ONE PLACE TO ADD OR CHANGE WORDS. Nothing else needs to
change: lists are normalized when this module loads.

Matching is whole-message and exact, after the shared normalisation (``apps.ai.text``:
casefold, punctuation/emoji to spaces) plus one extra step for ASCII letters only: a run
of the same letter collapses to one ("thankssss" -> "thanks", "byeee" -> "bye"). Both the
message and the phrase lists get the same treatment, so they always agree. Indic text is
never collapsed.

A message counts only when the WHOLE of it is made of known phrases ("thanks", "thank you
bye", "ok thanks"). A phrase inside a longer request ("thanks for the menu, show snacks")
is NOT a closing message and falls through to the normal intent layer. If a message holds
more than one kind ("thanks bye", "ok thanks"), the order of strength is BYE, THANKS, ACK.

ACK ("ok", "cool", "theek hai") is weaker: it is a real answer to some questions, so
``classify`` only says what the words are. The conversation engine decides whether ACK
applies (never while anything is pending). Deliberately NOT ACK: yes, yeah, sure, haan, no.
"""

from __future__ import annotations

import re

from .intents import Intent
from .text import normalize

# --------------------------------------------------------------------------- phrase lists

THANKS_PHRASES = (
    # English
    "thanks", "thank you", "thank u", "thx", "thnx", "thanx", "ty", "tq", "thankyou",
    "thanks a lot", "thanks a ton", "thank you so much", "thank you very much", "many thanks",
    "ok thanks", "okay thanks", "ok thank you", "okay thank you", "much appreciated",
    # romanized Hindi / Telugu
    "dhanyavaad", "dhanyavad", "dhanyawad", "shukriya", "shukria",
    "dhanyavadalu", "dhanyavadamulu", "dhanyavaadalu",
    # Hindi
    "धन्यवाद", "शुक्रिया", "बहुत धन्यवाद", "बहुत शुक्रिया",
    # Telugu
    "ధన్యవాదాలు", "ధన్యవాదములు", "థాంక్స్", "థాంక్ యు", "థాంక్యూ",
)  # fmt: skip

BYE_PHRASES = (
    # English
    "bye", "bye bye", "goodbye", "good bye", "see you", "see ya", "cya", "good night",
    "take care", "see you later", "see you soon", "ok bye", "okay bye",
    # romanized Hindi / Telugu
    "alvida", "shubh ratri", "subha ratri",
    # Hindi
    "अलविदा", "बाय", "बाय बाय", "शुभ रात्रि",
    # Telugu
    "బై", "బై బై", "శుభ రాత్రి", "శుభరాత్రి",
)  # fmt: skip

ACK_PHRASES = (
    # English
    "ok", "okay", "k", "kk", "cool", "great", "nice", "fine", "alright", "all right",
    "got it", "noted",
    # romanized Hindi / Telugu
    "theek hai", "thik hai", "theek", "accha", "achha", "acha", "sare", "sarey",
    # Hindi
    "ठीक है", "ठीक", "अच्छा",
    # Telugu
    "సరే", "సరె",
)  # fmt: skip

# Emoji-only acknowledgements (the shared normalisation drops emoji, so they are read
# from the raw text). Skin-tone and variation-selector characters are ignored.
ACK_EMOJI = frozenset("👍👌")
_EMOJI_MODIFIERS = frozenset(["️", "‍", *map(chr, range(0x1F3FB, 0x1F400))])

# A real closing message is short; anything longer is never tried.
MAX_TOKENS = 8

# --------------------------------------------------------------------------- matching

_REPEATS_RE = re.compile(r"([a-z])\1+")


def fold(text: str) -> str:
    """The comparison form: shared normalisation, then repeated ASCII letters collapsed."""
    return _REPEATS_RE.sub(r"\1", normalize(text))


def _table(phrases) -> frozenset[tuple[str, ...]]:
    return frozenset(tuple(fold(p).split()) for p in phrases) - {()}


_THANKS = _table(THANKS_PHRASES)
_BYE = _table(BYE_PHRASES)
_ACK = _table(ACK_PHRASES)
_LONGEST = max(len(p) for p in _THANKS | _BYE | _ACK)


def classify(text: str | None) -> Intent | None:
    """``Intent.BYE`` / ``THANKS`` / ``ACK`` when the whole message is made of such phrases,
    else ``None``. With several kinds present the strongest wins: BYE, then THANKS, then ACK."""
    if not isinstance(text, str) or not text.strip():
        return None
    tokens = fold(text).split()
    if not tokens:
        return Intent.ACK if _only_ack_emoji(text) else None
    if len(tokens) > MAX_TOKENS:
        return None
    # reachable[i]: the kinds of phrase seen on some way of splitting tokens[:i] into phrases
    reachable: list[set[frozenset[str]]] = [set() for _ in tokens] + [set()]
    reachable[0].add(frozenset())
    for i in range(len(tokens)):
        if not reachable[i]:
            continue
        for size in range(1, min(_LONGEST, len(tokens) - i) + 1):
            chunk = tuple(tokens[i : i + size])
            for kind, table in (("thanks", _THANKS), ("bye", _BYE), ("ack", _ACK)):
                if chunk in table:
                    reachable[i + size].update(seen | {kind} for seen in reachable[i])
    done = reachable[len(tokens)]
    if not done:
        return None
    if any("bye" in seen for seen in done):
        return Intent.BYE
    if any("thanks" in seen for seen in done):
        return Intent.THANKS
    return Intent.ACK


def _only_ack_emoji(text: str) -> bool:
    chars = [ch for ch in text if not ch.isspace() and ch not in _EMOJI_MODIFIERS]
    return bool(chars) and all(ch in ACK_EMOJI for ch in chars)
