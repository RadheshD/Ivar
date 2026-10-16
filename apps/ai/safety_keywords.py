"""Emergency keywords (DATA ONLY). A message containing one gets ONLY the fixed emergency
reply, before the rules layer and before any LLM call (see ``apps.ai.recommend.emergency_matches``).

Matching: NFKC + lower-case; Latin-script phrases match whole words, Devanagari and Telugu
script phrases match as substrings. Add a phrase to the right list; nothing else changes.

COVERAGE IS INCOMPLETE. The English list is the seed set; every other list is a short starter
set that NEEDS NATIVE-SPEAKER REVIEW and will miss many ways people describe an emergency
(spelling variants, dialects, other languages). The model's own ``health_context=emergency``
is a second, separate net, not a substitute for these lists. The reply wording must be
reviewed by a qualified person before launch.
"""

from __future__ import annotations

# style -> (needs_native_review, phrases)
EMERGENCY_KEYWORDS: dict[str, tuple[bool, tuple[str, ...]]] = {
    "en": (
        False,
        (
            "chest pain",
            "can't breathe",
            "cannot breathe",
            "trouble breathing",
            "difficulty breathing",
            "short of breath",
            "fainted",
            "fainting",
            "passed out",
            "severe bleeding",
            "heavy bleeding",
            "severe allergic reaction",
            "anaphylaxis",
            "throat swelling",
            "swollen throat",
            "overdose",
            "poisoning",
            "seizure",
            "stroke",
            "heart attack",
            "want to die",
            "kill myself",
            "end my life",
            "suicidal",
        ),
    ),
    # Devanagari Hindi
    "hi": (
        True,
        (
            "सीने में दर्द",
            "सांस नहीं",
            "साँस नहीं",
            "सांस लेने में तकलीफ",
            "बेहोश",
            "खून बह",
            "जहर",
            "दौरा पड़",
            "हार्ट अटैक",
            "आत्महत्या",
            "मरना चाहता",
            "मरना चाहती",
        ),
    ),
    # Telugu script
    "te": (
        True,
        (
            "ఛాతీ నొప్పి",
            "శ్వాస ఆడ",
            "ఊపిరి ఆడ",
            "స్పృహ తప్పి",
            "రక్తస్రావం",
            "విషం",
            "మూర్ఛ",
            "గుండెపోటు",
            "ఆత్మహత్య",
            "చనిపోవాలని",
        ),
    ),
    # Roman Hindi / Hinglish
    "hinglish": (
        True,
        (
            "seene mein dard",
            "sans nahi",
            "saans nahi",
            "saans lene mein takleef",
            "behosh",
            "khoon beh",
            "zeher",
            "dil ka daura",
            "heart attack",
            "aatmahatya",
            "marna chahta",
            "marna chahti",
        ),
    ),
    # Roman Telugu / Tenglish
    "tenglish": (
        True,
        (
            "chaati noppi",
            "oopiri adatledu",
            "oopiri ada",
            "spruha tappi",
            "raktasravam",
            "vishham",
            "moorcha",
            "gunde potu",
            "aatmahatya",
            "chanipovali",
        ),
    ),
}


# Self-injury and self-harm phrasings. A match gets the ``safety.self_harm`` reply (the same
# text as ``safety.emergency`` until a qualified reviewer supplies wording). Latin-script phrases
# match whole words and "*" stands for up to four other words. COVERAGE IS INCOMPLETE: no keyword
# list and no model can catch every phrasing. Every non-English list is a starter set that NEEDS
# NATIVE-SPEAKER REVIEW, and the reply wording MUST be reviewed by a qualified person.
SELF_HARM_KEYWORDS: dict[str, tuple[bool, tuple[str, ...]]] = {
    "en": (
        False,
        (
            "cutting myself",
            "cut myself",
            "cutting my hand",
            "cutting my wrist",
            "cutting my wrists",
            "cutting my arm",
            "cutting my arms",
            "cutting my leg",
            "cutting my finger",
            "cut my wrist",
            "cut my wrists",
            "cut my hand",
            "slit my wrist",
            "slit my wrists",
            "hurt myself",
            "hurting myself",
            "harm myself",
            "harming myself",
            "injure myself",
            "i want to die",
            "i wanna die",
            "want to die",
            "end it all",
            "no reason to live",
            "better off dead",
            "kill myself",
            "killing myself",
            "end my life",
            "suicide",
            "suicidal",
        ),
    ),
    "hi": (
        True,
        ("खुद को नुकसान", "खुद को चोट", "खुद को काट", "हाथ काट", "नस काट", "मरना चाहता", "मरना चाहती"),
    ),
    "te": (
        True,
        ("నన్ను నేను గాయ", "చేయి కోసుకు", "చేతిని కోసుకు", "చనిపోవాలని", "ఆత్మహత్య"),
    ),
    "hinglish": (
        True,
        ("khud ko hurt", "khud ko nuksan", "khud ko cut", "haath kaat", "nas kaat", "marna chahta",
         "marna chahti"),
    ),
    "tenglish": (
        True,
        ("naa cheyyi kosu", "nannu nenu", "chanipovali", "aatmahatya"),
    ),
}  # fmt: skip

# Bleeding and injury phrasings: the emergency reply.
INJURY_KEYWORDS: dict[str, tuple[bool, tuple[str, ...]]] = {
    "en": (
        False,
        (
            "bleeding",
            "bleeding a lot",
            "wont stop bleeding",
            "deep cut",
            "badly cut",
            "cut badly",
            "broken bone",
            "head injury",
        ),
    ),
    "hi": (True, ("खून बह", "खून निकल", "गहरा कट", "गहरी चोट")),
    "te": (True, ("రక్తం కారు", "రక్తం వస్తోంది", "లోతైన గాయం")),
    "hinglish": (True, ("khoon beh", "khoon nikal", "gehra cut", "bahut khoon")),
    "tenglish": (True, ("raktam kaaru", "raktam vastondi", "lotaina gaayam")),
}
