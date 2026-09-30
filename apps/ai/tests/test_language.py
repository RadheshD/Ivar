import pytest

from apps.ai.language import detect_language
from apps.ai.pipeline import IntentPipeline


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("show me snacks", "en"),
        ("నాకు స్నాక్స్ కావాలి", "te"),
        ("मुझे स्नैक्स चाहिए", "hi"),
        ("naaku snacks kavali", "te"),
        ("mujhe protein chahiye", "hi"),
        ("2 packets makhana add cheyyi", "te"),
        ("mera order kahan hai", "hi"),
        ("oats స్నాక్స్", "te"),  # script beats Latin words
        ("12345", None),
        ("", None),
        (None, None),
        ("🙂", None),
    ],
)
def test_detect_language(text, language):
    assert detect_language(text) == language


def test_tie_between_romanized_markers_is_unknown():
    assert detect_language("kavali chahiye") is None


def test_detected_language_is_only_a_hint():
    # The result reports a guess; nothing in it changes the customer's saved language.
    result = IntentPipeline().understand("mujhe protein chahiye")
    assert result.detected_language == "hi"
    assert result.entities.language_choice is None
