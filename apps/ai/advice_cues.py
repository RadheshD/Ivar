"""Advice cue phrases (DATA ONLY, no logic; matching lives in ``recommend.advice_cue_matches``).

The line "I can't say what you personally need, but ..." (``advice.disclaimer``) answers a
customer who ASKS what they need or whether something is good for them. It is shown only when
the model said ``asks_advice`` AND one of these phrases is really in the message, so a plain
request ("something high protein under 300") never gets it. Plain "suggest" / "recommend" are
NOT advice cues. Owner-editable: append a phrase to the right tuple.

Latin-script phrases match whole words (apostrophes ignored); Devanagari and Telugu phrases
match as substrings. Every NON-ENGLISH entry NEEDS NATIVE-SPEAKER REVIEW.
"""

from __future__ import annotations

# style -> (needs_native_review, phrases)
ADVICE_CUES: dict[str, tuple[bool, tuple[str, ...]]] = {
    "en": (
        False,
        (
            "what should i eat",
            "what do i need",
            "what food do i need",
            "what food should i",
            "what do i have to eat",
            "is it good for me",
            "is this healthy",
            "how much should i",
            "what is good for",
            "what's good for",
        ),
    ),
    "hinglish": (
        True,
        (
            "mujhe kya khana chahiye",
            "kya khana chahiye",
            "mere liye kya accha hai",
            "mere liye kya achha hai",
            "kya ye mere liye accha hai",
            "kitna khana chahiye",
            "kaun sa khana",
            "khana lena chahiye",
        ),
    ),
    "tenglish": (
        True,
        (
            "nenu emi tinali",
            "naaku emi kavali",
            "naaku em kavali",
            "naku emi kavali",
            "idi naaku manchidha",
            "nenu entha tinali",
            "emi tinali",
            "ye aaharam teesukovali",
        ),
    ),
    "hi": (
        True,
        (
            "मुझे क्या खाना चाहिए",
            "क्या खाना चाहिए",
            "मेरे लिए क्या अच्छा है",
            "क्या यह मेरे लिए अच्छा है",
            "कितना खाना चाहिए",
            "कौन सा खाना",
            "खाना लेना चाहिए",
        ),
    ),
    "te": (
        True,
        (
            "నేను ఏమి తినాలి",
            "నాకు ఏమి కావాలి",
            "ఇది నాకు మంచిదా",
            "ఎంత తినాలి",
            "ఏ ఆహారం తీసుకోవాలి",
            "ఏమి తినాలి",
        ),
    ),
}

# Question words. A goal word from ``recommend_cues.GOAL_SORT`` (gym, workout, muscle, bulk,
# protein ...) TOGETHER WITH one of these counts as a recommendation cue ("what food do I need
# for the gym"): it sends the message to the recommendation flow instead of a name search.
# Latin-script words match whole words; Devanagari and Telugu words match as substrings.
# Every NON-ENGLISH entry NEEDS NATIVE-SPEAKER REVIEW.
QUESTION_WORDS: dict[str, tuple[bool, tuple[str, ...]]] = {
    "en": (
        False,
        ("what", "whats", "which", "should", "need", "how much", "how many", "can i have"),
    ),
    "hinglish": (True, ("kya", "kaun", "kaunsa", "kaunsi", "chahiye", "kitna")),
    "tenglish": (True, ("emi", "em", "ela", "edi", "ye", "kavali", "entha")),
    "hi": (True, ("क्या", "कौन", "कौनसा", "चाहिए", "कितना")),
    "te": (True, ("ఏమి", "ఏది", "ఎలా", "కావాలి", "ఎంత")),
}
