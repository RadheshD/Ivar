"""Code-only cue data (DATA ONLY, no logic; matching lives in ``apps.ai.code_router``).

Everything here is read in code, with no model: allergen / ingredient / suitability wording,
product-fact questions, label and popularity requests, and multi-day plan requests. A match
only decides WHICH fixed template is sent; it never answers from product data it does not
have and never says yes or no to a suitability question.

How cues are matched (the same rules as the other cue files): text and cue are NFKC-folded and
lower-cased with punctuation removed (so "what's" becomes "whats" and "?" is gone). Latin-script
cues match whole words, and a "*" stands for up to four other words. Devanagari and Telugu
script cues match anywhere in the text.

Every non-English entry is a starter set that NEEDS NATIVE-SPEAKER REVIEW before real customers
use it, and coverage is incomplete: no list can catch every phrasing.
"""

from __future__ import annotations

Table = dict[str, tuple[bool, tuple[str, ...]]]  # style -> (needs_native_review, phrases)

# A first-person statement of an allergy or intolerance ("I have a nut allergy").
ALLERGY_STATEMENT: Table = {
    "en": (
        False,
        (
            "i have * allergy",
            "i have an allergy",
            "i am allergic",
            "im allergic",
            "allergic to",
            "my allergy",
            "my * allergy",
            "i am * intolerant",
            "im * intolerant",
            "lactose intolerant",
            "gluten intolerant",
        ),
    ),
    "hi": (
        True,
        ("मुझे * एलर्जी", "मुझे एलर्जी", "एलर्जी है", "allergy है", "मुझे allergy"),
    ),
    "te": (
        True,
        ("నాకు * అలర్జీ", "నాకు అలర్జీ", "అలర్జీ ఉంది", "allergy ఉంది", "నాకు allergy"),
    ),
    "hinglish": (True, ("mujhe * allergy", "mujhe allergy", "allergy hai", "meri allergy")),
    "tenglish": (True, ("naaku * allergy", "naaku allergy", "naku allergy", "allergy undi")),
}

# Allergen, ingredient and additive words. STRONG words always trigger the fixed reply; WEAK words
# are also product words ("show nuts", "add mixed nuts"), so they trigger only inside a question
# or "contains/free" frame (see ``QUESTION_FRAME``).
ALLERGEN_STRONG: Table = {
    "en": (
        False,
        (
            "allergy",
            "allergies",
            "allergic",
            "allergen",
            "allergens",
            "intolerance",
            "intolerant",
            "nut free",
            "peanut free",
            "dairy free",
            "soy free",
            "lactose",
            "gluten",
        ),
    ),
    "hi": (True, ("एलर्जी", "ग्लूटेन", "लैक्टोज")),
    "te": (True, ("అలర్జీ", "గ్లూటెన్", "లాక్టోజ్")),
    "hinglish": (True, ("allergy", "allergic", "gluten", "lactose")),
    "tenglish": (True, ("allergy", "allergic", "gluten", "lactose")),
}
ALLERGEN_WEAK: Table = {
    "en": (False, ("nut", "nuts", "peanut", "peanuts", "dairy", "soy", "soya", "milk", "egg free")),
    "hi": (True, ("मूंगफली", "दूध")),
    "te": (True, ("వేరుశెనగ", "పాలు")),
    "hinglish": (True, ("moongfali", "doodh")),
    "tenglish": (True, ("verusenaga", "paalu")),
}
INGREDIENT: Table = {
    "en": (
        False,
        (
            "ingredient",
            "ingredients",
            "made of",
            "made from",
            "made with",
            "contains",
            "contain",
            "preservative",
            "preservatives",
            "additive",
            "additives",
            "artificial",
        ),
    ),
    "hi": (True, ("सामग्री", "इंग्रीडिएंट", "प्रिज़र्वेटिव")),
    "te": (True, ("పదార్థాలు", "ఇంగ్రీడియంట్", "ప్రిజర్వేటివ్")),
    "hinglish": (True, ("ingredients", "kis se bana", "preservatives")),
    "tenglish": (True, ("ingredients", "denitho chesaru", "preservatives")),
}
# "What's in ...": an ingredient question about a PRODUCT, but also how people ask about their cart
# or the menu ("what's in my cart"). It counts as an ingredient question only when the message is
# not a cart / menu / order request (see ``CART_MENU_WORDS``) or a product is named.
WHATS_IN: Table = {
    "en": (False, ("whats in", "what is in", "what all is in")),
    "hi": (True, ("क्या है इसमें", "इसमें क्या")),
    "te": (True, ("ఇందులో ఏమి",)),
    "hinglish": (True, ("isme kya",)),
    "tenglish": (True, ("indulo emi",)),
}
# Cart, menu, order and category words. With one of these and no product named, a "what's in"
# message is a cart / menu request, not an ingredient question.
CART_MENU_WORDS: Table = {
    "en": (
        False,
        ("cart", "basket", "bag", "order", "orders", "menu", "categories", "category", "trolley"),
    ),
    "hi": (True, ("कार्ट", "टोकरी", "ऑर्डर", "मेन्यू", "मेनू", "श्रेणी")),
    "te": (True, ("కార్ట్", "బుట్ట", "ఆర్డర్", "మెనూ", "మెను", "వర్గం")),
    "hinglish": (True, ("cart", "basket", "order", "menu", "category")),
    "tenglish": (True, ("cart", "basket", "order", "menu", "category")),
}
# Suitability / "is it good for ...": never answered yes or no.
SUITABILITY: Table = {
    "en": (
        False,
        (
            "healthy",
            "good for kids",
            "good for children",
            "good for my kids",
            "good for babies",
            "good for elderly",
            "safe for",
            "suitable for",
            "ok for kids",
            "okay for kids",
        ),
    ),
    "hi": (True, ("सेहतमंद", "बच्चों के लिए", "सुरक्षित है")),
    "te": (True, ("ఆరోగ్యకరమైన", "పిల్లలకు", "సురక్షితమా")),
    "hinglish": (True, ("healthy hai", "bachon ke liye", "kids ke liye", "safe hai")),
    "tenglish": (True, ("healthy aa", "pillalaku", "kids ki", "safe aa")),
}

# Words that frame a question or a "contains / free of" claim.
QUESTION_FRAME: Table = {
    "en": (
        False,
        ("is", "are", "does", "do", "has", "have", "any", "contain", "contains",
         "without", "safe", "what", "which", "can"),
    ),
    "hi": (True, ("क्या", "है", "हैं")),
    "te": (True, ("ఉందా", "ఉన్నాయా", "ఏమి")),
    "hinglish": (True, ("kya", "hai", "hain", "hota", "hoga")),
    "tenglish": (True, ("undha", "unda", "unnaya", "emi", "aa")),
}  # fmt: skip

# Labels and popularity the product records cannot support: disclosed, never applied silently.
# label key -> style -> (review, phrases). Keys match ``apps.ai.recommend.UNSUPPORTED``.
LABELS: dict[str, Table] = {
    "vegan": {
        "en": (False, ("vegan",)),
        "hi": (True, ("वीगन",)),
        "te": (True, ("వీగన్",)),
        "hinglish": (True, ("vegan",)),
        "tenglish": (True, ("vegan",)),
    },
    "gluten_free": {
        "en": (False, ("gluten free", "glutenfree")),
        "hi": (True, ("ग्लूटेन फ्री",)),
        "te": (True, ("గ్లూటెన్ ఫ్రీ",)),
        "hinglish": (True, ("gluten free",)),
        "tenglish": (True, ("gluten free",)),
    },
    "sugar_free": {
        "en": (False, ("sugar free", "sugarfree", "no sugar", "zero sugar")),
        "hi": (True, ("शुगर फ्री", "बिना चीनी")),
        "te": (True, ("షుగర్ ఫ్రీ",)),
        "hinglish": (True, ("sugar free", "bina sugar")),
        "tenglish": (True, ("sugar free",)),
    },
    "other_label": {
        "en": (False, ("keto", "organic", "low carb", "paleo")),
        "hi": (True, ("ऑर्गेनिक", "जैविक")),
        "te": (True, ("ఆర్గానిక్",)),
        "hinglish": (True, ("keto", "organic")),
        "tenglish": (True, ("keto", "organic")),
    },
    "allergen_free": {
        "en": (False, ("nut free", "peanut free", "dairy free", "lactose free", "soy free")),
        "hi": (True, ("नट फ्री", "डेयरी फ्री")),
        "te": (True, ("నట్ ఫ్రీ", "డైరీ ఫ్రీ")),
        "hinglish": (True, ("nut free", "dairy free")),
        "tenglish": (True, ("nut free", "dairy free")),
    },
}
POPULARITY: Table = {
    "en": (
        False,
        (
            "best seller",
            "best sellers",
            "bestseller",
            "bestsellers",
            "best selling",
            "top selling",
            "top seller",
            "most sold",
            "most popular",
            "popular",
        ),
    ),
    "hi": (True, ("सबसे ज्यादा बिकने वाला", "बेस्ट सेलर", "लोकप्रिय")),
    "te": (True, ("బెస్ట్ సెల్లర్", "ఎక్కువగా అమ్ముడయ్యే", "ప్రసిద్ధ")),
    "hinglish": (True, ("best seller", "sabse zyada bikne wala", "popular")),
    "tenglish": (True, ("best seller", "ekkuva ammudayye", "popular")),
}

# Product-fact questions. fact -> style -> (review, phrases). A fact is answered only for a
# product that resolves through the Backend Port, and only from its record.
FACTS: dict[str, Table] = {
    "protein": {
        "en": (False, ("how much protein", "protein in", "protein does", "protein content")),
        "hi": (True, ("कितना प्रोटीन", "प्रोटीन कितना")),
        "te": (True, ("ఎంత ప్రోటీన్", "ప్రోటీన్ ఎంత")),
        "hinglish": (True, ("kitna protein", "protein kitna")),
        "tenglish": (True, ("entha protein", "protein entha")),
    },
    "calories": {
        "en": (
            False,
            ("how many calories", "how many kcal", "calories in", "how calories", "kcal",
             "calorie count", "calories does", "calories are"),
        ),
        "hi": (True, ("कितनी कैलोरी", "कैलोरी कितनी")),
        "te": (True, ("ఎన్ని కేలరీలు", "కేలరీలు ఎన్ని")),
        "hinglish": (True, ("kitni calories", "calories kitni", "kitna calorie")),
        "tenglish": (True, ("enni calories", "calories enni")),
    },
    "pack_size": {
        "en": (False, ("how many grams", "how many gm", "how much does it weigh", "pack size",
                       "net weight")),
        "hi": (True, ("कितने ग्राम", "वजन कितना")),
        "te": (True, ("ఎన్ని గ్రాములు", "బరువు ఎంత")),
        "hinglish": (True, ("kitne gram", "kitna weight")),
        "tenglish": (True, ("enni grams", "weight entha")),
    },
    "diet_type": {
        "en": (False, ("is it veg", "is it vegetarian", "is this veg", "is it non veg",
                       "veg or non veg")),
        "hi": (True, ("क्या यह वेज है", "वेज है क्या")),
        "te": (True, ("వెజ్ ఆ", "వెజ్ నా")),
        "hinglish": (True, ("veg hai kya", "kya ye veg hai", "veg hai")),
        "tenglish": (True, ("veg aa", "veg a", "veg na")),
    },
    "price": {
        "en": (False, ("price of", "price for", "how much is", "how much does", "cost of")),
        "hi": (True, ("की कीमत", "कितने का")),
        "te": (True, ("ధర ఎంత", "ఎంత ధర")),
        "hinglish": (True, ("kitne ka", "price kya", "kitne ki")),
        "tenglish": (True, ("entha price", "price entha")),
    },
}  # fmt: skip

# Multi-day plan requests go to the planner hand-off. A day count beside a plan word ("7 day
# plan", "diet for 5 days") is matched by pattern in the router; these are the fixed phrases.
DIET_PLAN: Table = {
    "en": (
        False,
        ("diet plan", "meal plan", "diet chart", "meal chart", "weekly plan", "weekly diet",
         "week plan", "weekly meal", "diet schedule", "food plan", "plan my meals"),
    ),
    "hi": (True, ("डाइट प्लान", "मील प्लान", "डाइट चार्ट", "साप्ताहिक प्लान")),
    "te": (True, ("డైట్ ప్లాన్", "మీల్ ప్లాన్", "డైట్ చార్ట్", "వారపు ప్లాన్")),
    "hinglish": (True, ("diet plan", "meal plan", "diet chart", "khane ka plan", "weekly plan")),
    "tenglish": (True, ("diet plan", "meal plan", "diet chart", "weekly plan")),
}  # fmt: skip

# Common words that are NEVER corrected into a product word by the fuzzy stage ("plan" -> "plant").
FUZZY_STOPLIST = frozenset(
    "plan plans diet meal meals day days good food foods plain week weekly chart menu lunch "
    "dinner breakfast kids kid healthy fresh daily light night time tonight today best "
    "some more less low high".split()
)

# Words that never belong to a product name: dropped before a product phrase is read from a
# fact or safety question, and never fuzzy-corrected into a product word (``fuzzy.py``).
QUESTION_FILLER = frozenset(
    "how much many what whats is are does do did the a an of in for it its this that "
    "have has had any can could you tell me about whether there contain contains "
    "protein calories calorie kcal grams gram price cost veg vegetarian ingredients ingredient "
    "nut nuts free allergy allergic allergen gluten dairy lactose soy made good safe "
    "suitable healthy kids children to and or please pls "
    "show suggest recommend".split()
)
