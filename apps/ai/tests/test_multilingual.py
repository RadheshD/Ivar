"""Every intent in every supported language style, decided without an LLM.

Styles: en (English), te (Telugu script), hi (Devanagari), te_rom (romanized Telugu),
hi_rom (romanized Hindi), mixed (code-mixed). Telugu/Hindi phrasing needs native review.
"""

import pytest

from apps.ai.intents import Intent, Stage
from apps.ai.llm import FakeLLMClient
from apps.ai.pipeline import IntentPipeline

I = Intent  # noqa: E741
OATS_3 = {"product_query": "oats", "quantity": 3}

CASES = [
    # GREETING
    (I.GREETING, "en", "hello", {}),
    (I.GREETING, "te", "నమస్కారం", {}),
    (I.GREETING, "hi", "नमस्ते", {}),
    (I.GREETING, "te_rom", "namaskaram", {}),
    (I.GREETING, "hi_rom", "namaste ji", {}),
    (I.GREETING, "mixed", "hi andi", {}),
    # LANGUAGE_SELECT
    (I.LANGUAGE_SELECT, "en", "switch to telugu", {"language_choice": "te"}),
    (I.LANGUAGE_SELECT, "te", "తెలుగులో మాట్లాడు", {"language_choice": "te"}),
    (I.LANGUAGE_SELECT, "hi", "हिंदी में बात करो", {"language_choice": "hi"}),
    (I.LANGUAGE_SELECT, "te_rom", "telugu lo matladu", {"language_choice": "te"}),
    (I.LANGUAGE_SELECT, "hi_rom", "hindi mein baat karo", {"language_choice": "hi"}),
    (I.LANGUAGE_SELECT, "mixed", "language change to hindi please", {"language_choice": "hi"}),
    # PRODUCT_SEARCH
    (I.PRODUCT_SEARCH, "en", "do you have makhana", {"product_query": "makhana"}),
    (I.PRODUCT_SEARCH, "te", "మఖానా ఉందా", {"product_query": "makhana"}),
    (I.PRODUCT_SEARCH, "hi", "मखाना है क्या", {"product_query": "makhana"}),
    (I.PRODUCT_SEARCH, "te_rom", "naaku oats kavali", {"product_query": "oats"}),
    (I.PRODUCT_SEARCH, "hi_rom", "mujhe protein chahiye", {"product_query": "protein"}),
    (I.PRODUCT_SEARCH, "mixed", "makhana unda bro", {"product_query": "makhana"}),
    # CATEGORY_SELECT
    (I.CATEGORY_SELECT, "en", "show me snacks", {"category_query": "snacks"}),
    (I.CATEGORY_SELECT, "te", "నాకు స్నాక్స్ కావాలి", {"category_query": "snacks"}),
    (I.CATEGORY_SELECT, "hi", "मुझे स्नैक्स चाहिए", {"category_query": "snacks"}),
    (I.CATEGORY_SELECT, "te_rom", "naaku snacks kavali", {"category_query": "snacks"}),
    (I.CATEGORY_SELECT, "hi_rom", "snacks dikhao", {"category_query": "snacks"}),
    (I.CATEGORY_SELECT, "mixed", "breakfast items chupinchu", {"category_query": "breakfast"}),
    # PRODUCT_SELECT
    (I.PRODUCT_SELECT, "en", "select the second one", {"choice_index": 2}),
    (I.PRODUCT_SELECT, "te", "రెండవది ఎంచుకో", {"choice_index": 2}),
    (I.PRODUCT_SELECT, "hi", "दूसरा वाला चुनो", {"choice_index": 2}),
    (I.PRODUCT_SELECT, "te_rom", "rendavadi select cheyyi", {"choice_index": 2}),
    (I.PRODUCT_SELECT, "hi_rom", "doosra wala chahiye", {"choice_index": 2}),
    (I.PRODUCT_SELECT, "mixed", "option 2 select cheyyi", {"choice_index": 2}),
    # ADD_TO_CART
    (
        I.ADD_TO_CART, "en", "add 2 packets of makhana crunch",
        {"product_query": "makhana crunch", "quantity": 2, "unit": "packet"},
    ),
    (
        I.ADD_TO_CART, "te", "మఖానా రెండు ప్యాకెట్లు జోడించు",
        {"product_query": "makhana", "quantity": 2, "unit": "packet"},
    ),
    (
        I.ADD_TO_CART, "hi", "दो पैकेट मखाना जोड़ो",
        {"product_query": "makhana", "quantity": 2, "unit": "packet"},
    ),
    (
        I.ADD_TO_CART, "te_rom", "2 packets makhana add cheyyi",
        {"product_query": "makhana", "quantity": 2, "unit": "packet"},
    ),
    (
        I.ADD_TO_CART, "hi_rom", "do packet makhana add karo",
        {"product_query": "makhana", "quantity": 2, "unit": "packet"},
    ),
    (
        I.ADD_TO_CART, "mixed", "makhana 3 packets cart lo pettu",
        {"product_query": "makhana", "quantity": 3, "unit": "packet"},
    ),
    # REMOVE_FROM_CART
    (I.REMOVE_FROM_CART, "en", "remove the oats", {"product_query": "oats"}),
    (I.REMOVE_FROM_CART, "te", "ఓట్స్ తీసేయి", {"product_query": "oats"}),
    (I.REMOVE_FROM_CART, "hi", "ओट्स हटाओ", {"product_query": "oats"}),
    (I.REMOVE_FROM_CART, "te_rom", "oats teeseyi", {"product_query": "oats"}),
    (I.REMOVE_FROM_CART, "hi_rom", "oats hatao", {"product_query": "oats"}),
    (I.REMOVE_FROM_CART, "mixed", "cart nundi oats remove cheyyi", {"product_query": "oats"}),
    # UPDATE_QUANTITY
    (I.UPDATE_QUANTITY, "en", "change oats quantity to 3", OATS_3),
    (I.UPDATE_QUANTITY, "te", "ఓట్స్ మూడు కి మార్చు", OATS_3),
    (I.UPDATE_QUANTITY, "hi", "ओट्स की मात्रा तीन करो", OATS_3),
    (I.UPDATE_QUANTITY, "te_rom", "oats quantity 3 ki marchu", OATS_3),
    (I.UPDATE_QUANTITY, "hi_rom", "oats ki quantity 3 kar do", OATS_3),
    (
        I.UPDATE_QUANTITY, "mixed", "oats 3 packets ki change cheyyi",
        {"product_query": "oats", "quantity": 3, "unit": "packet"},
    ),
    # VIEW_CART
    (I.VIEW_CART, "en", "show my cart", {}),
    (I.VIEW_CART, "te", "నా కార్ట్ చూపించు", {}),
    (I.VIEW_CART, "hi", "मेरा कार्ट दिखाओ", {}),
    (I.VIEW_CART, "te_rom", "naa cart chupinchu", {}),
    (I.VIEW_CART, "hi_rom", "mera cart dikhao", {}),
    (I.VIEW_CART, "mixed", "my cart dikhao", {}),
    # CHECKOUT
    (I.CHECKOUT, "en", "I want to checkout", {}),
    (I.CHECKOUT, "te", "ఆర్డర్ చేయి", {}),
    (I.CHECKOUT, "hi", "ऑर्डर कर दो", {}),
    (I.CHECKOUT, "te_rom", "order cheyyi", {}),
    (I.CHECKOUT, "hi_rom", "order kar do", {}),
    (I.CHECKOUT, "mixed", "checkout cheyyi please", {}),
    # ORDER_STATUS
    (I.ORDER_STATUS, "en", "where is my order", {}),
    (I.ORDER_STATUS, "te", "నా ఆర్డర్ ఎక్కడ ఉంది", {}),
    (I.ORDER_STATUS, "hi", "मेरा ऑर्डर कहाँ है", {}),
    (I.ORDER_STATUS, "te_rom", "naa order ekkada undi", {}),
    (I.ORDER_STATUS, "hi_rom", "mera order kahan hai", {}),
    (I.ORDER_STATUS, "mixed", "order status enti", {}),
    # PAYMENT_STATUS
    (I.PAYMENT_STATUS, "en", "did my payment go through", {}),
    (I.PAYMENT_STATUS, "te", "నా పేమెంట్ అయ్యిందా", {}),
    (I.PAYMENT_STATUS, "hi", "मेरा पेमेंट हुआ क्या", {}),
    (I.PAYMENT_STATUS, "te_rom", "payment ayyinda", {}),
    (I.PAYMENT_STATUS, "hi_rom", "payment hua kya", {}),
    (I.PAYMENT_STATUS, "mixed", "payment status enti", {}),
    # TRACKING
    (I.TRACKING, "en", "track my order", {}),
    (I.TRACKING, "te", "నా ఆర్డర్ ట్రాక్ చేయి", {}),
    (I.TRACKING, "hi", "मेरा ऑर्डर ट्रैक करो", {}),
    (I.TRACKING, "te_rom", "delivery ekkada undi", {}),
    (I.TRACKING, "hi_rom", "delivery boy kahan hai", {}),
    (I.TRACKING, "mixed", "tracking id cheppu", {}),
    # HELP
    (I.HELP, "en", "help", {}),
    (I.HELP, "te", "సహాయం కావాలి", {}),
    (I.HELP, "hi", "मदद चाहिए", {}),
    (I.HELP, "te_rom", "sahayam kavali", {}),
    (I.HELP, "hi_rom", "madad chahiye", {}),
    (I.HELP, "mixed", "help kavali", {}),
    # FALLBACK
    (I.FALLBACK, "en", "blue sky tomorrow", {}),
    (I.FALLBACK, "te", "ఆకాశం నీలం", {}),
    (I.FALLBACK, "hi", "आसमान नीला", {}),
    (I.FALLBACK, "te_rom", "aakasam neelam ga undi", {}),
    (I.FALLBACK, "hi_rom", "aasmaan neela hai", {}),
    (I.FALLBACK, "mixed", "sky neelam", {}),
]  # fmt: skip

# Detected language per style; mixed input is a best guess and not asserted.
STYLE_LANGUAGE = {"en": "en", "te": "te", "hi": "hi", "te_rom": "te", "hi_rom": "hi"}
TEXT_FIELDS = ("product_query", "category_query", "quantity", "unit", "language_choice")


def _id(case):
    intent, style, text, _ = case
    return f"{intent.value}-{style}"


def test_table_covers_every_intent_in_every_style():
    covered = {(intent, style) for intent, style, _, _ in CASES}
    styles = {"en", "te", "hi", "te_rom", "hi_rom", "mixed"}
    # PLAN_RECEIVED is never understood from natural language in any style: it is
    # produced only by the deterministic "IVA PLAN v1" header parser (apps.ai.plan_message),
    # same as CLARIFY is pipeline-only.
    excluded = (Intent.CLARIFY, Intent.PLAN_RECEIVED)
    expected = {(i, s) for i in Intent if i not in excluded for s in styles}
    assert covered == expected


@pytest.fixture(autouse=True)
def _rules_stage_only(settings):
    """These cases pin the rules stage in each language style. The code-only recommendation routes
    (goal words, price limits, safety wording ...) answer some of the same words first by default
    and are covered by their own tests, so they are switched off for this module."""
    settings.IVA_CODE_CONSTRAINTS_ENABLED = False


@pytest.mark.parametrize("case", CASES, ids=[_id(c) for c in CASES])
def test_intent_in_language_style(case):
    intent, style, text, entities = case
    llm = FakeLLMClient()
    result = IntentPipeline(llm).understand(text)

    assert result.intent is intent
    if intent is Intent.FALLBACK:
        assert result.stage is Stage.NONE
        assert llm.calls == 1  # nothing decided, so the LLM was consulted (and failed)
    else:
        assert result.stage is Stage.RULES
        assert result.confidence >= 0.7
        assert llm.calls == 0
        for field in TEXT_FIELDS:
            assert getattr(result.entities, field) == entities.get(field), field
    if style in STYLE_LANGUAGE:
        assert result.detected_language == STYLE_LANGUAGE[style]


@pytest.mark.parametrize(
    ("text", "intent", "entities"),
    [
        ("menu", Intent.CATEGORY_SELECT, {"category_query": None}),
        ("మెనూ", Intent.CATEGORY_SELECT, {"category_query": None}),
        ("hi, do you have makhana", Intent.PRODUCT_SEARCH, {"product_query": "makhana"}),
        ("status of IVA1002", Intent.ORDER_STATUS, {"order_number": "IVA1002"}),
        ("where is my order IVA-1002", Intent.ORDER_STATUS, {"order_number": "IVA1002"}),
        ("payment status iva1003", Intent.PAYMENT_STATUS, {"order_number": "IVA1003"}),
        ("hindi", Intent.LANGUAGE_SELECT, {"language_choice": "hi"}),
        ("english please", Intent.LANGUAGE_SELECT, {"language_choice": "en"}),
        ("change language", Intent.LANGUAGE_SELECT, {"language_choice": None}),
        ("pay now", Intent.CHECKOUT, {}),
        ("2 makhana", Intent.ADD_TO_CART, {"product_query": "makhana", "quantity": 2}),
        ("makhana", Intent.PRODUCT_SEARCH, {"product_query": "makhana"}),
        ("fresh", Intent.CATEGORY_SELECT, {"category_query": "fresh"}),
    ],
)
def test_other_phrasings(text, intent, entities):
    result = IntentPipeline().understand(text)
    assert result.intent is intent
    for field, value in entities.items():
        assert getattr(result.entities, field) == value


def test_irrelevant_entities_are_dropped():
    # "2" is not a quantity for order status; the order number is kept.
    result = IntentPipeline().understand("where is my order IVA1002 2 packets")
    assert result.intent is Intent.ORDER_STATUS
    assert result.entities.order_number == "IVA1002"
    assert result.entities.quantity is None


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("add iva1002 to cart", Intent.ADD_TO_CART),
        ("do you have abc1234", Intent.PRODUCT_SEARCH),
        ("remove iva1002", Intent.REMOVE_FROM_CART),
    ],
)
def test_order_numbers_only_for_order_intents(text, intent):
    result = IntentPipeline().understand(text)
    assert result.intent is intent
    assert result.entities.order_number is None
    assert result.entities.product_query  # the token stays ordinary text


@pytest.mark.parametrize("text", ["iva1002", "track IVA1002", "IVA1002 payment"])
def test_order_number_kept_for_order_intents(text):
    result = IntentPipeline().understand(text)
    assert result.intent in (Intent.ORDER_STATUS, Intent.TRACKING, Intent.PAYMENT_STATUS)
    assert result.entities.order_number == "IVA1002"


def test_fallback_never_carries_an_order_number():
    llm = FakeLLMClient([FakeLLMClient.reply("FALLBACK", 0.9, order_number="IVA1002")])
    result = IntentPipeline(llm).understand("can we talk about the weather")
    assert result.intent is Intent.FALLBACK
    assert result.entities.order_number is None


def test_fallback_keeps_entities_for_the_engine():
    result = IntentPipeline().understand("2")
    assert result.intent is Intent.FALLBACK
    assert result.entities.quantity == 2
