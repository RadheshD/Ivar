"""Transcripts: the AI Planner's 'IVA PLAN v1' handoff message, received as free text."""

from .harness import WA_ID, onboard

PLAN_MESSAGE = (
    "IVA PLAN v1\n"
    "goal: healthy_eating\n"
    "diet: veg\n"
    "calories: 2446\n"
    "protein_g: 91\n"
    "picks: Plant Protein Mix; Active Protein; Ivar Active Nutrition Drinks; Makhana Crunch"
)


def test_plan_message_shows_resolved_picks_with_port_prices_and_saves_diet(chat, backend):
    onboard(chat)
    chat.send(PLAN_MESSAGE)
    assert chat.keys == ["plan.summary"]
    body = chat.body
    assert "2446" in body
    assert "91" in body
    assert "Plant Protein Mix" in body and "₹899" in body
    assert "Active Protein" in body and "₹949" in body
    assert "Ivar Active Nutrition Drinks" in body and "₹79" in body
    assert "Makhana Crunch" in body and "₹199" in body
    assert chat.ids == ["ACT:planadd", "CATEGORY_SELECT"]
    assert backend.get_or_create_customer(WA_ID).diet == "veg"


def test_add_all_to_cart(chat, backend):
    onboard(chat)
    chat.send(PLAN_MESSAGE)
    chat.tap("ACT:planadd")
    assert chat.keys[0] == "plan.added_to_cart"
    assert "cart.title" in chat.keys
    customer = backend.get_or_create_customer(WA_ID)
    cart = backend.get_cart(customer.id)
    assert {ln.name for ln in cart.lines} == {
        "Plant Protein Mix",
        "Active Protein",
        "Ivar Active Nutrition Drinks",
        "Makhana Crunch",
    }
    assert all(ln.quantity == 1 for ln in cart.lines)


def test_add_all_to_cart_with_nothing_pending_shows_the_main_menu(chat):
    onboard(chat)
    chat.tap("ACT:planadd")
    assert chat.keys == ["menu.body"]


def test_unresolved_pick_is_mentioned_and_skipped(chat):
    onboard(chat)
    chat.send("IVA PLAN v1\npicks: Makhana Crunch; Nonexistent Product XYZ")
    assert "Makhana Crunch" in chat.body
    assert "Nonexistent Product XYZ" in chat.body
    assert chat.ids == ["ACT:planadd", "CATEGORY_SELECT"]


def test_out_of_stock_pick_is_mentioned_and_skipped_when_shown(chat, backend):
    onboard(chat)
    backend.set_stock("makhana-crunch", False)
    chat.send("IVA PLAN v1\npicks: Makhana Crunch; Active Protein")
    assert "Active Protein" in chat.body
    assert "Makhana Crunch" in chat.body  # mentioned as out of stock
    assert chat.ids == ["ACT:planadd", "CATEGORY_SELECT"]


def test_out_of_stock_between_showing_and_adding_is_skipped(chat, backend):
    onboard(chat)
    chat.send("IVA PLAN v1\npicks: Makhana Crunch; Active Protein")
    backend.set_stock("makhana-crunch", False)  # goes out of stock after the plan was shown
    chat.tap("ACT:planadd")
    assert "plan.picks_skipped_at_add" in chat.keys
    customer = backend.get_or_create_customer(WA_ID)
    cart = backend.get_cart(customer.id)
    assert {ln.name for ln in cart.lines} == {"Active Protein"}


def test_prices_edited_into_the_message_are_ignored(chat):
    onboard(chat)
    chat.send("IVA PLAN v1\npicks: Makhana Crunch @ Rs 1 total")
    assert "• Makhana Crunch — ₹199" in chat.body


def test_health_data_we_never_accept_is_ignored(chat):
    onboard(chat)
    chat.send(
        "IVA PLAN v1\n"
        "bmi: 22.5\n"
        "bmr: 1500\n"
        "tdee: 2200\n"
        "healthy_weight_range: 60-70\n"
        "nutrition_score: 8"
    )
    assert chat.keys == ["plan.summary"]
    for private in ("22.5", "1500", "2200", "60-70"):
        assert private not in chat.body


def test_no_valid_fields_still_acknowledges_the_plan(chat):
    onboard(chat)
    chat.send("IVA PLAN v1\ngoal: lose_weight\ncalories: 1")
    assert chat.keys == ["plan.summary"]
    assert "Thanks for sharing your plan" in chat.body


def test_missing_header_is_treated_as_normal_text(chat):
    onboard(chat)
    chat.send("goal: healthy_eating\ncalories: 2446")
    assert "plan.summary" not in chat.keys


def test_oversized_plan_message_is_treated_as_normal_text(chat):
    onboard(chat)
    chat.send("IVA PLAN v1\n" + "x" * 3000)
    assert "plan.summary" not in chat.keys
