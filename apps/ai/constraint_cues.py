"""Constraint cue phrases (DATA ONLY, no logic; matching lives in ``constraints.py``).

A constraint is something the customer asked for that a product LIST must respect: a price
limit ("under 300"), a diet ("veg"), a high-protein or low-calorie request. A message with
one of these is answered by the recommendation flow, which filters and sorts in code; it is
never answered by a name search that would silently ignore it (see ``IntentPipeline``).

How matching works (see ``apps.ai.constraints``): the message is normalised like every other
stage (case-folded, punctuation and symbols become spaces; the rupee sign is first turned
into the word "rs") and compared WORD BY WORD. Devanagari and Telugu keywords of three or
more letters also match as a word prefix ("లోపు" in "లోపుతో"), like the other keyword tables.

Price phrases come in two positions relative to the number: ``*_BEFORE`` phrases precede it
("under 300", "budget Rs 300"), ``*_AFTER`` phrases follow it ("300 ke andar", "300 లోపు").
A currency word may sit between a phrase and the number or after the number ("under Rs 300",
"under 300 rupees"). A MAX phrase makes the number a price limit by itself. A MIN phrase
("above", "more than", "at least") also reads as an ordinary quantity ("at least 3 packets"),
so it counts only with a currency word next to the number.

Add a phrase by appending it to the right tuple; nothing else needs to change.

Every NON-ENGLISH entry here is a first guess and NEEDS NATIVE-SPEAKER REVIEW before launch
(the ``True`` flag in each entry says so).
"""

from __future__ import annotations

# style -> (needs_native_review, phrases)
Table = dict[str, tuple[bool, tuple[str, ...]]]

# The rupee sign is rewritten to "rs" before matching, so "₹300" and "rs 300" are the same.
CURRENCY: Table = {
    "en": (False, ("rs", "inr", "rupee", "rupees")),
    "hinglish": (True, ("rupaye", "rupaya", "rupay", "rupaiye", "rupees")),
    "tenglish": (True, ("rupayalu", "rupayi", "rupai", "roopayalu", "roopai")),
    "hi": (True, ("रुपये", "रुपए", "रुपया", "रूपये", "रु")),
    "te": (True, ("రూపాయలు", "రూపాయి", "రూపాయల", "రూ")),
}

# ------------------------------------------------------------------ price: upper limit

PRICE_MAX_BEFORE: Table = {
    "en": (
        False,
        (
            "under",
            "below",
            "less than",
            "lesser than",
            "within",
            "upto",
            "up to",
            "max",
            "maximum",
            "not more than",
            "no more than",
            "at most",
            "budget",
            "budget of",
            "cheaper than",
        ),
    ),
    "hinglish": (True, ("budget", "max")),
    "tenglish": (True, ("budget", "max")),
    "hi": (True, ("अधिकतम", "बजट")),
    "te": (True, ("గరిష్టంగా", "బడ్జెట్")),
}

PRICE_MAX_AFTER: Table = {
    "en": (False, ("or less", "or under", "or below", "and below", "and under", "max")),
    "hinglish": (
        True,
        (
            "ke andar",
            "ke ander",
            "se kam",
            "tak",
            "se niche",
            "se neeche",
            "ke niche",
            "ke neeche",
        ),
    ),
    "tenglish": (
        True,
        ("lopu", "lopala", "loapu", "loga", "kante takkuva", "kanna takkuva", "varaku"),
    ),
    "hi": (True, ("के अंदर", "के भीतर", "से कम", "तक", "से नीचे", "के नीचे")),
    "te": (True, ("లోపు", "లోపల", "లోగా", "కంటే తక్కువ", "కన్నా తక్కువ", "వరకు", "కింద")),
}

# ------------------------------------------------------------------ price: lower limit
# These count only with a currency word beside the number (see the module note).

PRICE_MIN_BEFORE: Table = {
    "en": (False, ("over", "above", "more than", "at least", "minimum", "min")),
    "hinglish": (True, ()),
    "tenglish": (True, ()),
    "hi": (True, ("न्यूनतम",)),
    "te": (True, ("కనీసం",)),
}

PRICE_MIN_AFTER: Table = {
    "en": (False, ("or more", "or above", "and above", "and up", "min")),
    "hinglish": (True, ("se zyada", "se jyada", "se jada", "se upar", "ke upar")),
    "tenglish": (True, ("kante ekkuva", "kanna ekkuva", "paina")),
    "hi": (True, ("से ज्यादा", "से अधिक", "से ऊपर", "के ऊपर")),
    "te": (True, ("కంటే ఎక్కువ", "కన్నా ఎక్కువ", "పైన")),
}

# "between 100 and 300 [rupees]": the words around the two numbers (English only for now).
PRICE_BETWEEN: Table = {
    "en": (False, ("between",)),
}

# ------------------------------------------------------------------ diet words
# diet key -> style -> (needs_native_review, words). Keys are the Backend Port's DIETS.
# "non veg" is matched before "veg" so the "veg" inside it does not count twice.

DIET: dict[str, Table] = {
    "non_veg": {
        "en": (False, ("non veg", "nonveg", "non vegetarian", "nonvegetarian")),
        "hinglish": (True, ("mansahari", "mansahar")),
        "tenglish": (True, ("mamsaharam", "mamsahari")),
        "hi": (True, ("मांसाहारी", "नॉन वेज")),
        "te": (True, ("మాంసాహారం", "నాన్ వెజ్")),
    },
    "egg": {
        "en": (False, ("egg", "eggs")),
        "hinglish": (True, ("anda", "ande")),
        "tenglish": (True, ("guddu", "guddulu")),
        "hi": (True, ("अंडा", "अंडे")),
        "te": (True, ("గుడ్డు", "గుడ్లు")),
    },
    "veg": {
        "en": (False, ("veg", "vegetarian")),
        "hinglish": (True, ("shakahari", "shakahar")),
        "tenglish": (True, ("sakaharam", "shakaharam")),
        "hi": (True, ("शाकाहारी", "वेज")),
        "te": (True, ("శాకాహారం", "శాఖాహారం", "వెజ్")),
    },
}

# ------------------------------------------------------------------ protein and calories
# Map to an ordering by the real number, nothing more (see ``recommend_cues.GOAL_SORT``).

HIGH_PROTEIN: Table = {
    "en": (
        False,
        (
            "high protein",
            "high in protein",
            "protein rich",
            "rich in protein",
            "more protein",
            "extra protein",
            "lots of protein",
            "protein packed",
        ),
    ),
    "hinglish": (True, ("zyada protein", "jyada protein", "protein zyada", "protein wala")),
    "tenglish": (True, ("protein ekkuva", "ekkuva protein", "protein unna")),
    "hi": (True, ("हाई प्रोटीन", "ज्यादा प्रोटीन", "प्रोटीन ज्यादा", "प्रोटीन वाला")),
    "te": (True, ("హై ప్రోటీన్", "ఎక్కువ ప్రోటీన్", "ప్రోటీన్ ఎక్కువ", "ప్రోటీన్ ఉన్న")),
}

LOW_CALORIE: Table = {
    "en": (
        False,
        ("low calorie", "low calories", "low cal", "low in calories", "fewer calories"),
    ),
    "hinglish": (True, ("kam calorie", "kam calories")),
    "tenglish": (True, ("takkuva calories", "takkuva calorie", "thakkuva calories")),
    "hi": (True, ("कम कैलोरी",)),
    "te": (True, ("తక్కువ కేలరీలు", "తక్కువ క్యాలరీ")),
}

# ------------------------------------------------------------------ ranges
# "250 to 300", "250 se 300", "250 nundi 300 varaku": the word between the two numbers. A
# hyphen between two numbers is rewritten to the sentinel word "rngto" before matching (see
# ``constraints.prepare_ranges``). A pair joined this way is a price range ONLY when the message
# also holds a price or constraint cue (a currency word, a comparator, or a diet / protein /
# calorie word); "between X and Y" is its own cue. Non-English entries need native review.

PRICE_RANGE_JOIN: Table = {
    "en": (False, ("to", "rngto")),
    "hinglish": (True, ("se",)),
    "tenglish": (True, ("nundi", "nunchi")),
    "hi": (True, ("से",)),
    "te": (True, ("నుండి", "నుంచి")),
}

# ------------------------------------------------------------------ spelled-out numbers
# Read ONLY next to a price comparator, a range word or a currency word ("under two hundred");
# anywhere else a number word keeps its ordinary meaning ("add two quinoa" is a quantity).
# word -> value for 1-99: ones (1-9), teens (10-19), tens (20, 30 ... 90). Compounds are built by
# ``constraints`` ("two hundred", "two fifty", "three hundred and fifty", "one thousand",
# "do sau pachas"). Every non-English entry NEEDS NATIVE-SPEAKER REVIEW.

SPELLED_VALUES: dict[str, tuple[bool, dict[str, int]]] = {
    "en": (
        False,
        {
            "one": 1,
            "two": 2,
            "three": 3,
            "four": 4,
            "five": 5,
            "six": 6,
            "seven": 7,
            "eight": 8,
            "nine": 9,
            "ten": 10,
            "eleven": 11,
            "twelve": 12,
            "thirteen": 13,
            "fourteen": 14,
            "fifteen": 15,
            "sixteen": 16,
            "seventeen": 17,
            "eighteen": 18,
            "nineteen": 19,
            "twenty": 20,
            "thirty": 30,
            "forty": 40,
            "fifty": 50,
            "sixty": 60,
            "seventy": 70,
            "eighty": 80,
            "ninety": 90,
        },
    ),  # fmt: skip
    "hinglish": (
        True,
        {
            "ek": 1,
            "do": 2,
            "teen": 3,
            "char": 4,
            "chaar": 4,
            "paanch": 5,
            "panch": 5,
            "chhe": 6,
            "chhah": 6,
            "saat": 7,
            "aath": 8,
            "nau": 9,
            "das": 10,
            "gyarah": 11,
            "barah": 12,
            "terah": 13,
            "chaudah": 14,
            "pandrah": 15,
            "solah": 16,
            "satrah": 17,
            "atharah": 18,
            "unnis": 19,
            "bees": 20,
            "tees": 30,
            "chalis": 40,
            "pachas": 50,
            "saath": 60,
            "sattar": 70,
            "assi": 80,
            "nabbe": 90,
        },
    ),  # fmt: skip
    "tenglish": (
        True,
        {
            "okati": 1,
            "rendu": 2,
            "moodu": 3,
            "mudu": 3,
            "naalugu": 4,
            "nalugu": 4,
            "aidu": 5,
            "aaru": 6,
            "edu": 7,
            "enimidi": 8,
            "tommidi": 9,
            "padi": 10,
            "iravai": 20,
            "muppai": 30,
            "nalabhai": 40,
            "yabhai": 50,
            "aravai": 60,
            "debbhai": 70,
            "enabhai": 80,
            "tombhai": 90,
        },
    ),  # fmt: skip
    "hi": (
        True,
        {
            "एक": 1,
            "दो": 2,
            "तीन": 3,
            "चार": 4,
            "पांच": 5,
            "पाँच": 5,
            "छह": 6,
            "सात": 7,
            "आठ": 8,
            "नौ": 9,
            "दस": 10,
            "बीस": 20,
            "तीस": 30,
            "चालीस": 40,
            "पचास": 50,
            "साठ": 60,
            "सत्तर": 70,
            "अस्सी": 80,
            "नब्बे": 90,
        },
    ),  # fmt: skip
    "te": (
        True,
        {
            "ఒకటి": 1,
            "రెండు": 2,
            "మూడు": 3,
            "నాలుగు": 4,
            "ఐదు": 5,
            "ఆరు": 6,
            "ఏడు": 7,
            "ఎనిమిది": 8,
            "తొమ్మిది": 9,
            "పది": 10,
            "ఇరవై": 20,
            "ముప్పై": 30,
            "నలభై": 40,
            "యాభై": 50,
            "అరవై": 60,
            "డెబ్బై": 70,
            "ఎనభై": 80,
            "తొంభై": 90,
        },
    ),  # fmt: skip
}

SPELLED_HUNDRED: Table = {
    "en": (False, ("hundred",)),
    "hinglish": (True, ("sau",)),
    "tenglish": (True, ("vanda", "vandalu", "vandala")),
    "hi": (True, ("सौ",)),
    "te": (True, ("వంద", "వందలు", "వందల")),
}

SPELLED_THOUSAND: Table = {
    "en": (False, ("thousand",)),
    "hinglish": (True, ("hazaar", "hajaar", "hazar", "hajar")),
    "tenglish": (True, ("veyyi", "veyyilu")),
    "hi": (True, ("हजार",)),
    "te": (True, ("వెయ్యి", "వేలు")),
}

# Hindi fractional prices ("dhai sau" = 250). Read only beside a price word, like the other
# spelled-out numbers. Roman and Devanagari. Every entry NEEDS NATIVE-SPEAKER REVIEW.
SPELLED_FRACTIONS: dict[str, tuple[bool, dict[str, int]]] = {
    "hinglish": (
        True,
        {
            "dhai sau": 250,
            "dedh sau": 150,
            "saade teen sau": 350,
            "sade teen sau": 350,
            "saade paanch sau": 550,
            "sade paanch sau": 550,
            "saade panch sau": 550,
            "saade char sau": 450,
            "paune do sau": 175,
            "pone do sau": 175,
        },
    ),  # fmt: skip
    "hi": (
        True,
        {
            "ढाई सौ": 250,
            "डेढ़ सौ": 150,
            "साढ़े तीन सौ": 350,
            "साढ़े पाँच सौ": 550,
            "साढ़े पांच सौ": 550,
            "साढ़े चार सौ": 450,
            "पौने दो सौ": 175,
        },
    ),  # fmt: skip
}

# The bare word "protein" counts as the protein-sort cue ONLY when the message also holds a
# price constraint ("dhai sau ke andar protein"); on its own it is an ordinary search word.
# Every NON-ENGLISH entry NEEDS NATIVE-SPEAKER REVIEW.
BARE_PROTEIN: Table = {
    "en": (False, ("protein",)),
    "hinglish": (True, ("protein",)),
    "tenglish": (True, ("protein",)),
    "hi": (True, ("प्रोटीन",)),
    "te": (True, ("ప్రోటీన్",)),
}
