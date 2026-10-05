"""Hindi reply text. NEEDS NATIVE-SPEAKER REVIEW BEFORE LAUNCH.

Keys and {placeholders} must match en.py.
"""

MESSAGES = {
    # ------------------------------------------------------------------ language & onboarding
    "language.prompt": (
        "Welcome to Ivar! Please choose your language.\nదయచేసి మీ భాషను ఎంచుకోండి.\nकृपया अपनी भाषा चुनें।"
    ),
    "language.prompt_greeting_named": (
        "Hi {name}! 👋 Welcome to Ivar!\n"
        "Please choose your language.\nదయచేసి మీ భాషను ఎంచుకోండి.\nकृपया अपनी भाषा चुनें।"
    ),
    "language.prompt_greeting_unnamed": (
        "Welcome to Ivar!\nPlease choose your language.\nదయచేసి మీ భాషను ఎంచుకోండి.\nकृपया अपनी भाषा चुनें।"
    ),
    "language.changed": "ठीक है! अब से मैं हिंदी में जवाब दूँगा।",
    "diet.ask": (
        "क्या आपकी कोई खाने की पसंद है? मैं सिर्फ़ उससे मेल खाने वाले उत्पाद दिखाऊँगा। "
        "आप इसे बाद में मेनू से बदल सकते हैं।"
    ),
    "diet.veg": "शाकाहारी",
    "diet.egg": "शाकाहारी + अंडा",
    "diet.non_veg": "मांसाहारी",
    "diet.skip": "छोड़ें",
    "diet.saved": "सेव हो गया: {diet}।",
    "diet.skipped": "कोई बात नहीं, मैं सब कुछ दिखाऊँगा।",
    "diet.invalid": "कृपया कोई एक विकल्प चुनें, या छोड़ें।",
    "diet.hidden_all": '"{query}" के नतीजे मिले, लेकिन कोई भी आपकी खाने की पसंद ({diet}) से मेल नहीं खाता।',
    "diet.hidden_some": "आपकी खाने की पसंद ({diet}) की वजह से {count} और आइटम छिपे हैं।",
    "btn.show_anyway": "फिर भी दिखाएँ",
    "btn.change_diet": "पसंद बदलें",
    # ------------------------------------------------------------------ welcome & AI Planner
    "welcome.planner_intro": "हमारा AI प्लानर आपके लिए एक व्यक्तिगत खाने की योजना बना सकता है।",
    "welcome.browse_prompt": "या सीधे ऑर्डर करने के लिए मेनू देखें।",
    "planner.cta_body": "अपनी व्यक्तिगत खाने की योजना बनाने के लिए नीचे टैप करें।",
    "btn.planner": "मेरी खाने की योजना",
    "menu.website": "वेबसाइट पर पूरा मेन्यू",
    "website.cta_body": "हमारी वेबसाइट पर पूरा मेन्यू देखने के लिए नीचे टैप करें।",
    "btn.website": "पूरा मेन्यू खोलें",
    "btn.browse_menu": "मेनू देखें",
    # ------------------------------------------------------------------ menu & general
    "greeting": "नमस्ते {name}! 👋",
    "welcome_back": "फिर से स्वागत है, {name}!",
    "menu.body": 'आपको क्या चाहिए? आप टाइप भी कर सकते हैं, जैसे "स्नैक्स दिखाओ" या "मेरा ऑर्डर"।',
    "menu.button": "मेनू",
    "menu.cart": "कार्ट देखें",
    "menu.orders": "मेरा ऑर्डर",
    "menu.help": "मदद",
    "menu.language": "भाषा बदलें",
    "menu.diet": "खाने की पसंद",
    "help": (
        "मैं उत्पाद देखने, कार्ट में जोड़ने, चेकआउट करने और आपके ऑर्डर व पेमेंट की स्थिति "
        'जानने में मदद कर सकता हूँ। "स्नैक्स दिखाओ", "2 मखाना जोड़ो" या "मेरा ऑर्डर कहाँ है" '
        'लिखकर देखें। कभी भी "मेनू" लिखें।'
    ),
    "fallback": "माफ़ कीजिए, मैं समझ नहीं पाया।",
    "list.choose": "चुनें",
    "list.more": "और…",
    "list.more_desc": "अगले आइटम दिखाएँ",
    "btn.yes": "हाँ",
    "btn.menu": "मेनू",
    "btn.help": "मदद",
    "btn.cart": "कार्ट देखें",
    "btn.checkout": "चेकआउट",
    "btn.edit_cart": "कार्ट बदलें",
    "btn.change_qty": "मात्रा बदलें",
    "btn.remove": "हटाएँ",
    "btn.confirm": "ऑर्डर पक्का करें",
    "btn.cancel": "रद्द करें",
    "btn.payment_status": "पेमेंट स्थिति",
    "btn.order_status": "ऑर्डर स्थिति",
    # ------------------------------------------------------------------ clarify (never acted on)
    "clarify.GREETING": "क्या आप नमस्ते कह रहे हैं?",
    "clarify.LANGUAGE_SELECT": "क्या आप भाषा बदलना चाहते हैं?",
    "clarify.PRODUCT_SEARCH": "आप कौन सा उत्पाद ढूँढ रहे हैं? कृपया उसका नाम लिखें।",
    "clarify.CATEGORY_SELECT": "क्या आप मेनू देखना चाहते हैं?",
    "clarify.PRODUCT_SELECT": "आप किस उत्पाद की बात कर रहे हैं? कृपया उसका नाम लिखें।",
    "clarify.ADD_TO_CART": "क्या जोड़ना है, और कितने?",
    "clarify.REMOVE_FROM_CART": "कार्ट से कौन सा आइटम हटाऊँ?",
    "clarify.UPDATE_QUANTITY": "किस आइटम की मात्रा, कितनी करनी है?",
    "clarify.VIEW_CART": "क्या आप अपना कार्ट देखना चाहते हैं?",
    "clarify.CHECKOUT": "क्या आप चेकआउट करना चाहते हैं?",
    "clarify.ORDER_STATUS": "क्या आप अपने ऑर्डर की स्थिति देखना चाहते हैं?",
    "clarify.PAYMENT_STATUS": "क्या आप अपने पेमेंट की स्थिति देखना चाहते हैं?",
    "clarify.TRACKING": "क्या आप अपना ऑर्डर ट्रैक करना चाहते हैं?",
    "clarify.HELP": "क्या आपको मदद चाहिए?",
    # ------------------------------------------------------------------ browsing
    "group.body": "{group}: एक श्रेणी चुनें।",
    "category.body": "{category}: {total} में से {start}–{end}।",
    "category.empty": "{category}: अभी दिखाने के लिए कुछ नहीं है।",
    "category.none": 'माफ़ कीजिए, "{query}" नाम की कोई श्रेणी नहीं मिली।',
    "category.choose": '"{query}" से आपका मतलब इनमें से कौन सा है?',
    "search.results": '"{query}" के नतीजे: {total} में से {start}–{end}।',
    "search.none": 'माफ़ कीजिए, "{query}" नहीं मिला। आप मेनू से देख सकते हैं।',
    "choose.product": '"{query}" के कई नतीजे मिले। कौन सा चाहिए?',
    "ask.product": "आपको कौन सा उत्पाद चाहिए? नाम लिखें या मेनू देखें।",
    "product.price": "कीमत: {price}",
    "product.pack": "पैक: {pack}",
    "product.protein": "प्रोटीन: {protein} ग्राम",
    "product.kcal": "ऊर्जा: {kcal} किलो कैलोरी",
    "product.unverified": "पोषण मान अभी सत्यापित नहीं हैं।",
    "product.unavailable": "माफ़ कीजिए, {name} अभी स्टॉक में नहीं है।",
    "product.choose_variant": "{name}: एक विकल्प चुनें।",
    "product.ask_qty": "कितने चाहिए? बटन दबाएँ या संख्या लिखें।",
    "product.out_of_stock_tag": "स्टॉक में नहीं",
    "btn.add_to_cart": "कार्ट में जोड़ें",
    "btn.order_now": "अभी ऑर्डर करें",
    "btn.continue_shopping": "खरीदारी जारी रखें",
    "carousel.body": "ये रहे हमारे {category}",
    "checkout.welcome_back": "{name}, फिर से स्वागत है! आप चेकआउट के बीच में थे।",
    "btn.continue_checkout": "चेकआउट जारी रखें",
    "cards.footer": "और देखना है, या मेनू पर वापस जाना है?",
    "cards.stale": "माफ़ कीजिए, वह वस्तु अभी उपलब्ध नहीं है।",
    # ------------------------------------------------------------------ cart
    "cart.added": "{qty} × {name} जोड़ दिया।",
    "cart.updated": "{name} को {qty} कर दिया।",
    "cart.removed": "{name} हटा दिया।",
    "cart.empty": "आपका कार्ट खाली है।",
    "cart.title": "आपका कार्ट:",
    "cart.line": "• {name} × {qty} = {total}",
    "cart.subtotal": "कुल: {subtotal}",
    "cart.edit": "कौन सा आइटम बदलना चाहते हैं?",
    "cart.line_actions": "{name} × {qty}। क्या करना है?",
    "cart.not_found": 'आपके कार्ट में "{query}" नहीं मिला।',
    "cart.choose_line": "आप किस आइटम की बात कर रहे हैं?",
    "qty.ask_update": "{name} की नई मात्रा? संख्या लिखें।",
    "qty.ask": "कृपया एक संख्या लिखें, जैसे 2।",
    "qty.invalid": "यह मात्रा संभव नहीं है। कृपया कोई दूसरी संख्या आज़माएँ।",
    # ------------------------------------------------------------------ checkout & payment
    "checkout.ask_address": "कहाँ डिलीवर करें? सेव किया पता चुनें या नया जोड़ें।",
    "checkout.type_address": "कृपया अपना पूरा डिलीवरी पता लिखें।",
    "checkout.new_address": "नया पता",
    "checkout.address_invalid": "कृपया पूरा पता लिखें (कम से कम 10 अक्षर)।",
    "checkout.confirm": "कृपया अपना ऑर्डर पक्का करें।",
    "checkout.deliver_to": "डिलीवरी पता: {address}",
    "checkout.created": "ऑर्डर {order} हो गया। कुल {total}।",
    "checkout.payment_line": "पेमेंट: {status}।",
    "checkout.pay_link": "यहाँ भुगतान करें: {url}",
    "checkout.no_link": "पेमेंट लिंक अभी उपलब्ध नहीं है। थोड़ी देर बाद पेमेंट स्थिति देखें।",
    "checkout.cancelled": "चेकआउट रद्द किया गया। आपका कार्ट सुरक्षित है।",
    "checkout.price_changed": "कुछ कीमतें बदल गई हैं। मौजूदा कीमतों के साथ आपका कार्ट यह है।",
    "checkout.product_unavailable": "कुछ आइटम अब उपलब्ध नहीं हैं। कृपया अपना कार्ट देखें।",
    "checkout.not_serviceable": "माफ़ कीजिए, उस पते पर अभी डिलीवरी नहीं होती। कृपया दूसरा पता दें।",
    "checkout.below_minimum": "आपका ऑर्डर न्यूनतम ऑर्डर मूल्य से कम है। कृपया और आइटम जोड़ें।",
    "checkout.rejected": "हम आपका ऑर्डर नहीं कर पाए। कृपया कार्ट देखकर फिर से कोशिश करें।",
    "payment.PENDING": "ऑर्डर {order} का पेमेंट बाकी है।",
    "payment.PAID": "ऑर्डर {order} का पेमेंट मिल गया है। धन्यवाद!",
    "payment.FAILED": "ऑर्डर {order} का पेमेंट विफल रहा। ऑर्डर अभी बाकी है; आप पेमेंट लिंक से फिर कोशिश कर सकते हैं।",
    "payment.REFUNDED": "ऑर्डर {order} का पेमेंट वापस कर दिया गया है।",
    "payment.unknown": "ऑर्डर {order} की पेमेंट स्थिति: {status}।",
    # ------------------------------------------------------------------ orders
    "order.none": "आपका अभी कोई ऑर्डर नहीं है।",
    "order.not_found": "ऑर्डर {order} नहीं मिला।",
    "order.status": "ऑर्डर {order}: {status}।",
    "order.payment": "पेमेंट: {status}।",
    "order.eta": "अनुमानित डिलीवरी: {eta}।",
    "order.eta_na": "डिलीवरी का समय: अभी उपलब्ध नहीं है।",
    "order.tracking": "ट्रैकिंग ID: {tracking}।",
    "order.tracking_na": "ट्रैकिंग: अभी उपलब्ध नहीं है।",
    "status.order.PENDING": "बाकी",
    "status.order.CONFIRMED": "पक्का",
    "status.order.PROCESSING": "तैयार हो रहा है",
    "status.order.PACKED": "पैक हो गया",
    "status.order.OUT_FOR_DELIVERY": "डिलीवरी के लिए निकला",
    "status.order.DELIVERED": "डिलीवर हो गया",
    "status.order.CANCELLED": "रद्द",
    "status.payment.PENDING": "बाकी",
    "status.payment.PAID": "भुगतान हो गया",
    "status.payment.FAILED": "विफल",
    "status.payment.REFUNDED": "वापस किया गया",
    # ------------------------------------------------------------------ AI Planner: plan message
    "plan.clause_goal_healthy_eating": "सेहतमंद खाना",
    "plan.clause_calories": "लगभग {kcal} किलो कैलोरी",
    "plan.clause_protein": "{protein} ग्राम प्रोटीन",
    "plan.and": " और ",
    "plan.per_day_suffix": " रोज़ाना",
    "plan.summary": "आपकी योजना: {details}।",
    "plan.no_valid_fields": "अपनी योजना साझा करने के लिए धन्यवाद!",
    "plan.pick_line": "• {name} — {price}",
    "plan.picks_unresolved": "हमारे कैटलॉग में नहीं मिला: {names}।",
    "plan.picks_out_of_stock": "अभी स्टॉक में नहीं: {names}।",
    "plan.added_to_cart": "आपकी योजना के आइटम कार्ट में जोड़ दिए।",
    "plan.picks_skipped_at_add": "{count} आइटम अब उपलब्ध नहीं थे और छोड़ दिए गए।",
    "btn.add_all_to_cart": "सभी कार्ट में जोड़ें",
    # ------------------------------------------------------------------ errors
    "error.unavailable": "माफ़ कीजिए, हमारी सेवा अभी व्यस्त है। कृपया थोड़ी देर बाद फिर कोशिश करें।",
    "error.generic": "माफ़ कीजिए, कुछ गड़बड़ हो गई। कृपया फिर से कोशिश करें।",
    "error.not_found": "माफ़ कीजिए, वह नहीं मिला।",
    "error.out_of_stock": "माफ़ कीजिए, यह आइटम अभी स्टॉक में नहीं है।",
    # ------------------------------------------------------------------ rate limit
    "limit.rate_limited": "आप थोड़ा तेज़ी से संदेश भेज रहे हैं — कृपया थोड़ा धीमा करें। "
    "जब आप तैयार हों, मैं यहाँ रहूँगा।",
}

ESCAPE_WORDS = ("मेनू", "रद्द", "वापस", "menu", "cancel")
# Whole-message commands at the address step (see en.py). NEEDS NATIVE-SPEAKER REVIEW.
GREETING_WORDS = (
    "नमस्ते", "नमस्कार", "प्रणाम", "हेलो", "हैलो", "हाय", "हाई",
    "namaste", "namaskar", "pranam", "pranaam", "hello",
)  # fmt: skip
CART_WORDS = (
    "कार्ट", "मेरा कार्ट", "कार्ट दिखाओ", "मेरा कार्ट दिखाओ", "कार्ट दिखाइए",
    "mera cart", "cart dikhao", "mera cart dikhao", "cart dikhaiye",
)  # fmt: skip
ORDER_STATUS_WORDS = (
    "ऑर्डर स्टेटस", "मेरा ऑर्डर", "ऑर्डर ट्रैक करो", "मेरा ऑर्डर कहाँ है",
    "mera order", "order status", "order track karo",
)  # fmt: skip
HELP_WORDS = ("मदद", "सहायता", "हेल्प", "madad", "help")
LANGUAGE_WORDS = ("भाषा", "भाषा बदलो", "भाषा बदलें", "bhasha", "bhasha badlo", "language")
DIET_WORDS = {
    "veg": ("शाकाहारी", "veg", "shakahari"),
    "egg": ("अंडा", "शाकाहारी + अंडा", "egg", "anda"),
    "non_veg": ("मांसाहारी", "non veg", "nonveg", "mansahari"),
    "skip": ("छोड़ें", "छोड़ो", "बाद में", "skip", "chhodo"),
}
