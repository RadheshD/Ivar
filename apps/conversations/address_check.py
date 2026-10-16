"""Does typed text look like a delivery address? (code only, no model, nothing is stored here)

The rule is deliberately loose, because real addresses vary a lot and a customer who is turned
away can simply add detail. Text is accepted when it has

* at least three DISTINCT words (a word is anything with a letter or digit in it, in any script),
* and at least one of: a digit (house number, PIN code), four or more distinct words, or an
  address word (road, colony, nagar, flat ...).

So "zork florp", "yes", an emoji or "yes yes yes yes" are asked again, while "Flat 4B, Road 12,
Banjara Hills, Hyderabad" (with or without a PIN code), "Jubilee Hills Hyderabad" and addresses in
Hindi, Telugu, Hinglish or Tenglish pass. It cannot tell a real street from an invented one: any
text with four or more different words is accepted, and the order is still shown to the customer for
confirmation before it is placed. The non-English address words are a starter set and need
native-speaker review.
"""

from __future__ import annotations

import re
import unicodedata

_SPLIT = re.compile(r"[\s,;/|]+")
_STRIP = ".,;:()[]{}'\"-"

# Words that make a short, digit-free text an address ("Jubilee Hills Hyderabad").
ADDRESS_WORDS = frozenset(
    """
    road rd street st lane nagar colony layout apartment apartments flat plot house door block
    sector phase hills enclave tower towers residency villa village mandal near opposite behind
    cross main avenue gali marg chowk nilayam illu inti veedhi
    रोड गली नगर कॉलोनी मकान फ्लैट प्लॉट मोहल्ला सड़क पास बिल्डिंग अपार्टमेंट हिल्स गांव
    రోడ్డు రోడ్ వీధి నగర్ కాలనీ ఇల్లు ఇంటి ఫ్లాట్ ప్లాట్ అపార్ట్‌మెంట్ హిల్స్ దగ్గర గ్రామం
    """.split()
)


def _is_word(token: str) -> bool:
    return any(unicodedata.category(ch)[0] in ("L", "N") for ch in token)


def address_words(text: str) -> list[str]:
    """The distinct words of ``text`` (case-folded), in order."""
    seen: dict[str, None] = {}
    for token in _SPLIT.split(text):
        token = token.strip(_STRIP)
        if token and _is_word(token):
            seen.setdefault(token.casefold(), None)
    return list(seen)


def looks_like_address(text: str) -> bool:
    words = address_words(text)
    if len(words) < 3:
        return False
    if any(unicodedata.category(ch) == "Nd" for ch in text):
        return True
    return len(words) >= 4 or any(w in ADDRESS_WORDS for w in words)
