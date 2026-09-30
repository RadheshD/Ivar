"""Parsing the AI Planner's 'IVA PLAN v1' handoff message."""

from apps.ai.plan_message import PLAN_MAX_LENGTH, parse_plan_message

EXAMPLE = (
    "IVA PLAN v1\n"
    "goal: healthy_eating\n"
    "diet: veg\n"
    "calories: 2446\n"
    "protein_g: 91\n"
    "picks: Plant Protein Mix; Active Protein; Ivar Active Nutrition Drinks; Makhana Crunch"
)


def test_the_example_message():
    e = parse_plan_message(EXAMPLE)
    assert e is not None
    assert e.goal == "healthy_eating"
    assert e.diet == "veg"
    assert e.calories == 2446
    assert e.protein_g == 91
    assert e.picks == (
        "Plant Protein Mix",
        "Active Protein",
        "Ivar Active Nutrition Drinks",
        "Makhana Crunch",
    )


def test_missing_header_is_not_a_plan_message():
    assert parse_plan_message("goal: healthy_eating\ncalories: 2446") is None
    assert parse_plan_message("hi") is None
    assert parse_plan_message("") is None


def test_header_must_match_exactly():
    assert parse_plan_message("iva plan v1\ngoal: healthy_eating") is None
    assert parse_plan_message("IVA PLAN v2\ngoal: healthy_eating") is None
    assert parse_plan_message(" IVA PLAN v1\ngoal: healthy_eating") is None


def test_unknown_goal_is_dropped_not_guessed():
    e = parse_plan_message("IVA PLAN v1\ngoal: lose_weight")
    assert e.goal is None


def test_unknown_diet_is_dropped():
    e = parse_plan_message("IVA PLAN v1\ndiet: keto")
    assert e.diet is None


def test_calories_out_of_range_is_dropped():
    assert parse_plan_message("IVA PLAN v1\ncalories: 799").calories is None
    assert parse_plan_message("IVA PLAN v1\ncalories: 6001").calories is None
    assert parse_plan_message("IVA PLAN v1\ncalories: 800").calories == 800
    assert parse_plan_message("IVA PLAN v1\ncalories: 6000").calories == 6000


def test_calories_non_numeric_is_dropped():
    assert parse_plan_message("IVA PLAN v1\ncalories: a lot").calories is None


def test_protein_out_of_range_is_dropped():
    assert parse_plan_message("IVA PLAN v1\nprotein_g: 9").protein_g is None
    assert parse_plan_message("IVA PLAN v1\nprotein_g: 401").protein_g is None
    assert parse_plan_message("IVA PLAN v1\nprotein_g: 10").protein_g == 10
    assert parse_plan_message("IVA PLAN v1\nprotein_g: 400").protein_g == 400


def test_picks_are_split_trimmed_and_capped_at_ten():
    names = "; ".join(f"Product {i}" for i in range(1, 13))
    e = parse_plan_message(f"IVA PLAN v1\npicks: {names}")
    assert len(e.picks) == 10
    assert e.picks[0] == "Product 1"
    assert e.picks[-1] == "Product 10"


def test_empty_pick_entries_are_dropped():
    e = parse_plan_message("IVA PLAN v1\npicks: Makhana Crunch; ; Active Protein")
    assert e.picks == ("Makhana Crunch", "Active Protein")


def test_health_data_we_never_accept_is_ignored():
    e = parse_plan_message(
        "IVA PLAN v1\n"
        "bmi: 22.5\n"
        "bmr: 1500\n"
        "tdee: 2200\n"
        "healthy_weight_range: 60-70\n"
        "nutrition_score: 8\n"
    )
    assert e.goal is None and e.diet is None and e.calories is None and e.protein_g is None
    assert e.picks == ()


def test_unknown_keys_are_ignored():
    e = parse_plan_message("IVA PLAN v1\nfoo: bar\ngoal: healthy_eating")
    assert e.goal == "healthy_eating"


def test_lines_without_a_colon_are_ignored():
    e = parse_plan_message("IVA PLAN v1\nnot a key value line\ngoal: healthy_eating")
    assert e.goal == "healthy_eating"


def test_oversized_message_is_not_a_plan_message():
    oversized = "IVA PLAN v1\n" + "x" * PLAN_MAX_LENGTH
    assert len(oversized) > PLAN_MAX_LENGTH
    assert parse_plan_message(oversized) is None


def test_message_at_the_length_cap_is_still_accepted():
    body = "IVA PLAN v1\ngoal: healthy_eating"
    padded = body + "\n" + "x" * (PLAN_MAX_LENGTH - len(body) - 1)
    assert len(padded) == PLAN_MAX_LENGTH
    e = parse_plan_message(padded)
    assert e is not None
    assert e.goal == "healthy_eating"


def test_non_string_input_is_not_a_plan_message():
    assert parse_plan_message(None) is None
