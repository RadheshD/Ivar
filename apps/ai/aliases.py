"""Keyword, alias and vocabulary tables for the rules stage.

Entries are written naturally in English, romanized Telugu, romanized Hindi, Telugu
script and Devanagari; ``rules`` normalizes them with the same function it applies to
customer text. Multi-word entries match consecutive words. Telugu/Devanagari entries of
three or more characters also match as a word prefix (Telugu attaches suffixes such as
"-లో" to the word).

Telugu and Hindi entries need native-speaker review before launch.
"""

# --------------------------------------------------------------------------- intent signals

GREETING = (
    "hi", "hii", "hello", "helo", "hey", "good morning", "good afternoon", "good evening",
    "namaste", "namaskar", "namaskaram", "namaskaramandi",
    "నమస్కారం", "నమస్తే", "హాయ్", "హలో",
    "नमस्ते", "नमस्कार", "हेलो", "हाय",
)  # fmt: skip

LANGUAGE_NAMES = {
    "en": ("english", "inglish", "angrezi", "अंग्रेज़ी", "अंग्रेजी", "ఇంగ్లీష్", "ఇంగ్లిష్"),
    "te": ("telugu", "తెలుగు", "तेलुगु", "तेलुगू"),
    "hi": ("hindi", "हिंदी", "हिन्दी", "హిందీ"),
}

LANGUAGE_CHANGE = (
    "language", "languages", "change language", "switch", "switch to", "set language",
    "bhasha", "bhaasha", "matladu", "maatladu", "matladandi", "baat karo", "baat kijiye",
    "భాష", "మాట్లాడు", "మాట్లాడండి",
    "भाषा", "बात करो", "बात कीजिए",
)  # fmt: skip

TRACKING = (
    "track", "tracking", "tracking id", "delivery", "delivered", "deliver", "courier",
    "delivery boy", "rider", "eta",
    "ట్రాక్", "ట్రాకింగ్", "డెలివరీ",
    "ट्रैक", "ट्रैकिंग", "डिलीवरी",
)  # fmt: skip

CHECKOUT = (
    "checkout", "check out", "place order", "place my order", "place the order",
    "confirm order", "confirm my order", "buy now", "pay now", "make payment",
    "make the payment", "proceed", "proceed to pay",
    "order karo", "order kar", "order kardo", "order cheyyi", "order chey", "order cheyandi",
    "order cheyyandi", "order pettu",
    "ఆర్డర్ చేయి", "ఆర్డర్ చెయ్యి", "ఆర్డర్ చేయండి", "ఆర్డర్ పెట్టు", "చెక్అవుట్",
    "ऑर्डर करो", "ऑर्डर कर", "ऑर्डर कीजिए", "ऑर्डर प्लेस", "चेकआउट",
)  # fmt: skip

PAYMENT = (
    "payment", "payments", "paid", "pay status", "refund", "refunded", "paisa", "paise",
    "bhugtan", "chellimpu",
    "పేమెంట్", "చెల్లింపు",
    "पेमेंट", "भुगतान",
)  # fmt: skip

ORDER_NOUN = ("order", "orders", "ఆర్డర్", "ऑर्डर", "आर्डर")

STATUS = (
    "status", "where", "when", "kahan", "kahaan", "kaha", "kab", "aayega", "ayega",
    "ekkada", "eppudu", "enti", "vastundi",
    "ఎక్కడ", "ఎప్పుడు", "స్టేటస్", "వస్తుంది",
    "कहाँ", "कहा", "कब", "स्टेटस", "आएगा",
)  # fmt: skip

MY = ("my", "mera", "meri", "mere", "naa", "నా", "मेरा", "मेरी", "मेरे")

REMOVE = (
    "remove", "delete", "take out", "take off", "hatao", "hata", "hata do", "nikalo",
    "nikal do", "teeseyi", "teesey", "teesiveyi", "tiseyi", "teeseyandi",
    "తీసేయి", "తీసెయ్యి", "తొలగించు", "తీసివేయి",
    "हटाओ", "हटा", "हटा दो", "निकालो", "निकाल दो",
)  # fmt: skip

# A strong update signal decides alone; a weak one needs a quantity and no "add".
UPDATE_STRONG = (
    "quantity", "qty", "change quantity", "update quantity", "instead", "badlo", "badal",
    "badal do", "maarchu", "marchu", "maarchandi", "matra",
    "మార్చు", "మార్చండి",
    "बदलो", "बदल दो", "मात्रा",
)  # fmt: skip
UPDATE_WEAK = ("make", "make it", "change", "update", "set")

ADD = (
    "add", "put", "add to cart", "daalo", "dalo", "daal", "daal do", "jodo", "jod do",
    "pettu", "pettandi", "veyyi",
    "యాడ్", "జోడించు", "వెయ్యి", "వేయి", "పెట్టు",
    "जोड़ो", "जोड़ दो", "डालो", "डाल दो", "ऐड", "एड",
)  # fmt: skip

CART = ("cart", "basket", "కార్ట్", "బాస్కెట్", "कार्ट", "बास्केट")

SELECT = (
    "select", "choose", "pick", "this one", "that one", "i ll take", "ill take",
    "ye wala", "yeh wala", "ye wali", "yeh wali", "idi", "enchuko", "chuno",
    "ఇది", "ఎంచుకో", "ఎంచుకోండి",
    "चुनो", "चुनिए", "यह वाला", "ये वाला",
)  # fmt: skip

HELP = (
    "help", "support", "what can you do", "how does this work", "how to order",
    "madad", "madat", "sahayam", "sahaayam",
    "సహాయం", "సహాయము", "హెల్ప్",
    "मदद", "सहायता", "हेल्प",
)  # fmt: skip

MENU = (
    "menu", "main menu", "categories", "category", "browse", "catalog", "catalogue",
    "start over", "back to menu",
    "మెనూ", "मेनू", "मेन्यू",
)  # fmt: skip

WANT = (
    "want", "need", "want to", "do you have", "have", "show", "show me", "see", "search",
    "find", "looking for", "get me", "give me", "buy",
    "kavali", "kaavali", "kavalandi", "kaavaali", "unda", "undha", "unnaya", "unnaaya",
    "chupinchu", "choopinchu", "chupinchandi", "chudu", "ivvu", "ivvandi",
    "chahiye", "chaiye", "chahie", "chahiya", "dikhao", "dikha do", "dikhaiye", "milega",
    "hai kya", "hain kya",
    "కావాలి", "ఉందా", "ఉన్నాయా", "చూపించు", "చూపించండి", "ఇవ్వు",
    "चाहिए", "दिखाओ", "दिखाइए", "मिलेगा", "है क्या", "हैं क्या",
)  # fmt: skip

# Words that carry no entity meaning and are dropped before entity extraction.
FILLERS = (
    # English
    "i", "me", "a", "an", "the", "some", "any", "to", "of", "for", "and", "with", "in", "on",
    "from", "please", "pls", "plz", "can", "could", "you", "u", "is", "are", "it", "its",
    "this", "that", "one", "ones", "items", "item", "stuff", "things", "also", "now",
    "just", "do", "does", "did", "go", "through", "am", "be", "been", "has", "bro", "sir",
    "madam", "ok", "okay", "thanks", "thank", "yes", "what", "id", "boy",
    # asking words that never name a product ("suggest ragi mix", "show me oats", "call ...")
    "suggest", "suggestion", "suggestions", "recommend", "recommendation", "show", "tell",
    "call",
    # romanized Telugu
    "naaku", "naku", "nenu", "maaku", "lo", "ki", "ku", "ni", "nundi", "nunchi", "tho",
    "konni", "koncham", "andi", "ga", "emi", "emiti", "undi", "unnayi", "anna", "cheyyi",
    "chey", "cheyandi", "cheyyandi", "kosam", "ivi", "avi", "ayyinda", "cheppu",
    # romanized Hindi
    "mujhe", "muje", "mujhko", "humko", "hume", "ka", "ke", "ko", "mein", "se", "aur",
    "kuch", "thoda", "hai", "hain", "kya", "karo", "kar", "kardo", "de", "dena", "dijiye",
    "ji", "bhai", "yaar", "zara", "wala", "wali", "wale", "ye", "yeh", "woh", "abhi",
    "bhi", "na", "hua", "hui",
    # Telugu script
    "నాకు", "నేను", "లో", "కి", "ని", "నుండి", "తో", "కొన్ని", "కొంచెం", "అండి", "ఏమి",
    "ఏంటి", "ఉంది", "చేయి", "చెయ్యి", "చేయండి", "దయచేసి", "కోసం", "అయ్యిందా",
    # Devanagari
    "मुझे", "मुझको", "का", "की", "के", "को", "में", "से", "और", "कुछ", "थोड़ा", "है",
    "हैं", "क्या", "करो", "कर", "दो", "दे", "दीजिए", "जी", "भाई", "वाला", "वाली", "ये",
    "यह", "भी", "अभी", "हुआ",
)  # fmt: skip

# --------------------------------------------------------------------------- choices

ORDINALS = {
    1: ("first", "1st", "modati", "modatidi", "pehla", "pehli", "మొదటిది", "మొదటి", "पहला", "पहली"),
    2: (
        "second", "2nd", "rendava", "rendavadi", "doosra", "doosri", "dusra", "dusri",
        "రెండవది", "రెండవ", "दूसरा", "दूसरी",
    ),
    3: (
        "third", "3rd", "moodava", "moodavadi", "teesra", "teesri", "tisra",
        "మూడవది", "మూడవ", "तीसरा", "तीसरी",
    ),
    4: ("fourth", "4th", "naalugava", "chautha", "నాలుగవది", "चौथा"),
    5: ("fifth", "5th", "aidava", "paanchva", "ఐదవది", "पांचवा"),
}  # fmt: skip

# "option 2", "number 3", "no 1": the number that follows is a choice, not a quantity.
CHOICE_PREFIX = ("option", "opt", "number", "no", "choice")

# --------------------------------------------------------------------------- quantities

NUMBER_WORDS = {
    # English
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "dozen": 12,
    # romanized Hindi
    "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5, "chhe": 6,
    "chhah": 6, "saat": 7, "aath": 8, "nau": 9, "das": 10,
    # romanized Telugu
    "okati": 1, "okkati": 1, "oka": 1, "rendu": 2, "moodu": 3, "mudu": 3, "naalugu": 4,
    "nalugu": 4, "aidu": 5, "aidhu": 5, "aaru": 6, "edu": 7, "eedu": 7, "enimidi": 8,
    "tommidi": 9, "padi": 10,
    # Devanagari
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "पाँच": 5, "छह": 6, "छः": 6, "सात": 7,
    "आठ": 8, "नौ": 9, "दस": 10,
    # Telugu script
    "ఒకటి": 1, "ఒక": 1, "రెండు": 2, "మూడు": 3, "నాలుగు": 4, "ఐదు": 5, "ఆరు": 6, "ఏడు": 7,
    "ఎనిమిది": 8, "తొమ్మిది": 9, "పది": 10,
}  # fmt: skip

# Number words that are also ordinary words ("do you have", "one of", "oka" = "a").
# They count as a quantity only when a unit or a product/category word follows.
AMBIGUOUS_NUMBER_WORDS = ("one", "do", "दो", "oka", "ఒక", "ek", "एक")

UNITS = {
    "packet": (
        "packet", "packets", "pkt", "pkts", "pack", "packs", "paket",
        "ప్యాకెట్", "ప్యాకెట్లు", "पैकेट",
    ),
    "piece": ("piece", "pieces", "pc", "pcs", "mukka", "mukkalu", "పీస్", "ముక్క", "ముక్కలు", "पीस"),
    "box": ("box", "boxes", "dabba", "dibba", "డబ్బా", "डिब्बा"),
    "bottle": ("bottle", "bottles", "బాటిల్", "बोतल"),
    "unit": ("unit", "units", "nos"),
}  # fmt: skip

# A number followed by one of these is a pack size ("quinoa 500 g"), not a quantity.
PACK_SIZE_UNITS = (
    "g", "gm", "gms", "gram", "grams", "kg", "kgs", "kilo", "ml", "l", "ltr", "litre",
    "liter", "litres", "liters",
)  # fmt: skip

# --------------------------------------------------------------------------- entities

# Built-in category words. A message whose entity text is entirely one of these is a
# CATEGORY_SELECT with the canonical English key as category_query.
CATEGORY_TERMS = {
    "snacks": (
        "snacks", "snack", "namkeen", "chirutindi", "స్నాక్స్", "చిరుతిండి", "स्नैक्स", "स्नैक", "नमकीन",
    ),
    "breakfast": ("breakfast", "tiffin", "tiffins", "nashta", "naashta", "అల్పాహారం", "టిఫిన్", "नाश्ता"),
    "bowls": ("bowls", "bowl", "బౌల్", "బౌల్స్", "बाउल"),
    "beverages": ("beverages", "beverage", "drinks", "drink", "పానీయాలు", "ड्रिंक्स", "पेय"),
    "cookies": ("cookies", "cookie", "biscuits", "biscuit", "కుకీలు", "కుకీస్", "बिस्कुट", "कुकीज"),
    "shots": ("shots", "wellness shots", "షాట్స్", "शॉट्स"),
    "cooking essentials": (
        "cooking essentials", "cooking", "groceries", "grocery", "కిరాణా", "किराना",
    ),
    "ready mixes": ("ready mix", "ready mixes", "instant mix"),
    "energy bites": ("energy bites",),
    "fresh": ("fresh", "fresh food", "ready to eat", "తాజా", "ताजा"),
    "packaged": ("packaged", "packaged food", "packaged foods"),
    "non veg": ("non veg", "nonveg", "non vegetarian", "నాన్ వెజ్", "नॉन वेज"),
    "cafe": ("cafe", "café", "cafe favourites"),
}  # fmt: skip

# Product words the rules stage recognises without catalog hints (English, singular and
# plural). Used to decide whether bare text is a product and as fuzzy-matching targets.
PRODUCT_VOCAB = (
    "makhana", "oats", "oat", "ragi", "millet", "millets", "protein", "nuts", "nut", "seeds",
    "seed", "chana", "quinoa", "chia", "flax", "atta", "tea", "latte", "turmeric", "porridge",
    "pancake", "pancakes", "smoothie", "muesli", "upma", "dosa", "chicken", "fish", "prawn",
    "prawns", "egg", "eggs", "kebab", "seekh", "salmon", "paneer", "avocado", "jowar",
    "crackers", "crisps", "puffs", "trail", "mix", "crunch", "bites", "shot", "steak",
    "garlic", "mango", "dates", "chocolate", "peanut", "jaggery", "coconut", "herbal",
    "green", "multigrain", "roasted", "instant", "lemon", "herb", "grilled", "immunity",
    "digest", "morning", "night", "boost", "fruit", "fruits", "high", "active", "plant",
    "masala", "pepper", "sesame", "cocoa", "nutrition", "grains",
)  # fmt: skip

# Native-script and romanized words mapped to the English word the catalog uses.
TERM_ALIASES = {
    "मखाना": "makhana", "మఖానా": "makhana", "makhane": "makhana",
    "ओट्स": "oats", "ఓట్స్": "oats",
    "प्रोटीन": "protein", "ప్రోటీన్": "protein",
    "chai": "tea", "चाय": "tea", "టీ": "tea",
    "anda": "egg", "ande": "egg", "अंडा": "egg", "अंडे": "egg", "గుడ్డు": "egg", "గుడ్లు": "egg",
    "पनीर": "paneer", "పనీర్": "paneer",
    "चिकन": "chicken", "చికెన్": "chicken",
    "machli": "fish", "मछली": "fish", "చేప": "fish",
    "jhinga": "prawn", "झींगा": "prawn", "royyalu": "prawn", "రొయ్యలు": "prawn",
    "haldi": "turmeric", "हल्दी": "turmeric", "పసుపు": "turmeric",
    "चना": "chana", "senagalu": "chana", "శనగలు": "chana",
    "आटा": "atta", "रागी": "ragi", "రాగి": "ragi",
    "बाजरा": "millet", "సిరిధాన్యాలు": "millet",
    "ज्वार": "jowar", "jonna": "jowar", "జొన్న": "jowar",
    "डोसा": "dosa", "దోశ": "dosa", "उपमा": "upma", "ఉప్మా": "upma",
    "क्विनोआ": "quinoa", "క్వినోవా": "quinoa", "स्मूदी": "smoothie",
}  # fmt: skip

# --------------------------------------------------------------------------- language guess

TELUGU_MARKERS = (
    "naaku", "naku", "nenu", "kavali", "kaavali", "kavalandi", "cheyyi", "chey", "cheyandi",
    "cheyyandi", "undi", "unda", "undha", "unnaya", "ekkada", "eppudu", "lo", "enti", "emi",
    "emiti", "chupinchu", "choopinchu", "pettu", "teeseyi", "teesey", "teesiveyi", "marchu",
    "maarchu", "matladu", "maatladu", "ayyinda", "sahayam", "namaskaram", "namaskaramandi",
    "andi", "nundi", "nunchi", "cheppu", "rendu", "moodu", "okati", "okkati", "rendava",
    "rendavadi", "modati", "modatidi", "ivvu", "ivvandi", "kosam", "konni", "vastundi",
    "chudu", "enchuko", "idi", "naa",
)  # fmt: skip

HINDI_MARKERS = (
    "mujhe", "muje", "mujhko", "mera", "meri", "mere", "chahiye", "chaiye", "chahie",
    "kahan", "kahaan", "hai", "hain", "kya", "karo", "kar", "kardo", "dikhao", "hatao",
    "nikalo", "badlo", "badal", "madad", "hua", "kab", "aayega", "ayega", "namaste", "wala",
    "wali", "doosra", "doosri", "dusra", "pehla", "pehli", "teesra", "mein", "baat", "ka",
    "ke", "ko", "se", "aur", "daalo", "dalo", "jodo", "bhai", "ji", "milega", "kuch", "abhi",
    "bhi",
)  # fmt: skip
