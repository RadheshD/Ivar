"""Recommendation cue phrases (DATA ONLY, no logic; matching lives in ``recommend.py``).

A message that contains one of these goes straight to the recommendation extraction call
(one Groq call, no classifier call first). Add a phrase by appending it to the tuple of the
right style; nothing else needs to change.

How a cue is matched (see ``apps.ai.recommend.cue_matches``): text and cue are NFKC-
normalised and lower-cased. Latin-script cues must match whole words ("suggest" does not
match "suggestion"; list that separately if wanted). Devanagari and Telugu-script cues
match anywhere in the text (those scripts have no reliable word spacing for this).

Every non-English entry is a first guess and NEEDS NATIVE-SPEAKER REVIEW before launch.
Keep sets small: a cue only decides *which* Groq call is made, so a missing cue costs one
extra call (the classifier path still reaches the flow), while a too-broad cue sends
unrelated chat to the recommendation call.
"""

from __future__ import annotations

# style -> (needs_native_review, cues)
CUES: dict[str, tuple[bool, tuple[str, ...]]] = {
    "en": (
        False,
        (
            "suggest",
            "recommend",
            "recommendation",
            "what should i",
            "what's good",
            "good for",
            "best for",
            "options for",
        ),
    ),
    # Roman-script Hindi / Hinglish.
    "hinglish": (True, ("suggest karo", "recommend karo", "kya khana chahiye")),
    # Roman-script Telugu / Tenglish.
    "tenglish": (True, ("suggest cheyyi", "emi tinali")),
    # Devanagari and Telugu script: empty on purpose until a native speaker supplies them.
    "hi": (True, ()),
    "te": (True, ()),
}

# Phrases seen on real phones. Kept apart from the starter set above (CUES) on purpose: the
# starter set is pinned by a test. Matched exactly like CUES. Non-English entries (none yet)
# would need native-speaker review.
EXTRA_CUES: dict[str, tuple[bool, tuple[str, ...]]] = {
    "en": (
        False,
        (
            "what would you recommend",
            "what'd you recommend",
            "what do you recommend",
            "recommend me",
            "suggest me",
            "what do i have for",
            "what can i eat",
            "what do i eat",
            "what to eat",
            "which food",
        ),
    ),
}

# Health wording sends a message to the extraction call (which sets health_context) so the
# fixed safety sentences are always composed; it never selects or sorts products. A starter
# set only: coverage is incomplete, and every non-English entry NEEDS NATIVE-SPEAKER REVIEW.
HEALTH_CUES: dict[str, tuple[bool, tuple[str, ...]]] = {
    "en": (
        False,
        (
            "headache",
            "head ache",
            "migraine",
            "fever",
            "stomach ache",
            "stomach pain",
            "cough",
            "nausea",
            "nauseous",
            "vomiting",
            "tired",
            "weak",
            "weakness",
            "fatigue",
            "pregnant",
            "pregnancy",
            "breastfeeding",
            "my baby",
            "my kid",
            "my child",
            "toddler",
            "infant",
            "elderly",
            "allergy",
            "allergic",
            "intolerance",
            "nut free",
        ),
    ),
    "hi": (
        True,
        ("सिरदर्द", "बुखार", "पेट दर्द", "खांसी", "थकान", "कमजोरी", "गर्भवती", "प्रेगनेंट", "एलर्जी"),
    ),
    "te": (True, ("తలనొప్పి", "జ్వరం", "కడుపు నొప్పి", "దగ్గు", "నీరసం", "గర్భవతి", "అలర్జీ")),
    "hinglish": (
        True,
        (
            "sar dard",
            "sirdard",
            "bukhar",
            "pet dard",
            "khansi",
            "thakan",
            "kamzori",
            "pregnant",
            "allergy",
            "bachcha",
        ),
    ),
    "tenglish": (
        True,
        (
            "talanoppi",
            "jwaram",
            "kadupu noppi",
            "daggu",
            "neerasam",
            "garbhavati",
            "allergy",
            "pillalu",
        ),
    ),
}


# ---------------------------------------------------------------------------------------
# Health / weight-loss phrases: these are answered by the fixed medical reply ("I can't give
# medical or dietary advice ...") and never reach the model or the product picker. A "*" in
# a Latin-script phrase stands for up to four other words ("lose * weight" matches
# "lose my body weight"). Matching is case-insensitive; Devanagari/Telugu script phrases
# match as substrings. Add a phrase here to extend it. NEEDS NATIVE-SPEAKER REVIEW for every
# non-English entry.
MEDICAL_CUES: dict[str, tuple[bool, tuple[str, ...]]] = {
    "en": (
        False,
        (
            "lose * weight",
            "losing * weight",
            "reduce * weight",
            "weight loss",
            "slim",
            "slimming",
            "fat loss",
            "lose * fat",
            "burn fat",
            "diabetes",
            "diabetic",
            "cholesterol",
            "blood pressure",
            "thyroid",
            "pcos",
        ),
    ),
    "hinglish": (
        True,
        (
            "weight kam",
            "wajan kam",
            "vajan kam",
            "weight ghatana",
            "fat kam",
            "patla hona",
            "motapa",
            "sugar ki bimari",
        ),
    ),
    "tenglish": (
        True,
        (
            "weight taggali",
            "weight taggadaniki",
            "baruvu taggali",
            "baruvu taggadaniki",
            "fat taggali",
            "slim avvali",
        ),
    ),
    "hi": (True, ("वजन घटा", "वजन कम", "मोटापा", "पतला होना", "चर्बी", "डायबिटीज", "मधुमेह")),
    "te": (True, ("బరువు తగ్గ", "సన్నగా", "కొవ్వు తగ్గ", "డయాబెటిస్", "మధుమేహం")),
}

# ---------------------------------------------------------------------------------------
# Goal words -> sort key. Applied in code AFTER validation, only when the model returned no
# sort. It maps a goal word to ordering by protein and NOTHING else: no goal maps to
# calories_asc, to any weight-loss meaning, or to any claim about what a product does for the
# customer. Latin-script words match whole words; Devanagari/Telugu match as substrings.
# NEEDS NATIVE-SPEAKER REVIEW for every non-English entry.
GOAL_SORT: dict[str, dict[str, tuple[bool, tuple[str, ...]]]] = {
    "protein_desc": {
        "en": (
            False,
            (
                "gym",
                "workout",
                "work out",
                "muscle",
                "muscles",
                "bulk",
                "bulking",
                "protein",
                "high protein",
                "post workout",
                "post-workout",
            ),
        ),
        # Roman-script Hinglish / Tenglish mostly reuse the English words above.
        "hinglish": (True, ("jim", "masal", "protein wala")),
        "tenglish": (True, ("jim", "protein unna")),
        "hi": (True, ("जिम", "वर्कआउट", "मसल", "प्रोटीन", "बल्क")),
        "te": (True, ("జిమ్", "వర్కౌట్", "కండరా", "ప్రోటీన్", "బల్క్")),
    },
}


# ---------------------------------------------------------------------------------------
# Words that make a sort an EXPLICIT food request. With health wording in the message the
# model's sort is kept only if one of these words is really there (so "weight loss" or
# "diabetes" can never turn into a calorie or protein ranking). Non-English entries NEED
# NATIVE-SPEAKER REVIEW.
EXPLICIT_SORT_WORDS: dict[str, dict[str, tuple[bool, tuple[str, ...]]]] = {
    "calories_asc": {
        "en": (
            False,
            ("low calorie", "low calories", "low cal", "fewer calories", "less calories"),
        ),
        "hi": (True, ("कम कैलोरी",)),
        "te": (True, ("తక్కువ కేలరీలు", "తక్కువ క్యాలరీ")),
        "hinglish": (True, ("kam calorie", "kam calories")),
        "tenglish": (True, ("takkuva calories", "takkuva calorie")),
    },
    "protein_desc": {
        "en": (False, ("protein", "high protein")),
        "hi": (True, ("प्रोटीन",)),
        "te": (True, ("ప్రోటీన్",)),
        "hinglish": (True, ("protein",)),
        "tenglish": (True, ("protein",)),
    },
    "price_asc": {
        "en": (False, ("cheap", "cheapest", "affordable", "budget", "low price", "lowest price")),
        "hi": (True, ("सस्ता", "सस्ते")),
        "te": (True, ("చౌక",)),
        "hinglish": (True, ("sasta", "sasti", "sasta wala")),
        "tenglish": (True, ("chowka", "takkuva price")),
    },
    "price_desc": {
        "en": (False, ("expensive", "premium", "most expensive")),
        "hi": (True, ("महंगा",)),
        "te": (True, ("ఖరీదైన",)),
        "hinglish": (True, ("mehenga", "mehnga")),
        "tenglish": (True, ("kharidaina",)),
    },
}

# ---------------------------------------------------------------------------------------
# Follow-up phrases that refer to an earlier message. No conversation history is ever sent
# to the model, so these are answered as a fresh message: with the neutral "couldn't find
# that" reply and the category browse. NEEDS NATIVE-SPEAKER REVIEW for non-English entries.
FOLLOWUP_CUES: dict[str, tuple[bool, tuple[str, ...]]] = {
    "en": (
        False,
        (
            "cheaper ones",
            "cheaper options",
            "show more",
            "more like this",
            "more like that",
            "the second one",
            "the first one",
            "the third one",
            "the last one",
            "same but",
            "those ones",
        ),
    ),
    "hinglish": (True, ("aur dikhao", "inse sasta", "doosra wala", "pehla wala")),
    "tenglish": (True, ("inka chupinchu", "rendo di", "modati di")),
    "hi": (True, ("और दिखाओ", "दूसरा वाला", "पहला वाला")),
    "te": (True, ("ఇంకా చూపించు", "రెండోది", "మొదటిది")),
}
