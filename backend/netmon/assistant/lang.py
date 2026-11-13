"""[3b] Pure language detection of a question (architecture § 14.3.5)."""

from __future__ import annotations

LANGUAGE_NAMES = {"en": "English", "he": "Hebrew"}


def detect_language(text: str, fallback: str = "en") -> str:
    """``he`` when at least 30 % of the letters are Hebrew, ``en`` when there are letters
    otherwise; no letters → `fallback` (the user's UI language)."""
    hebrew = 0
    letters = 0
    for ch in text or "":
        if "֐" <= ch <= "׿":
            if ch.isalpha():
                hebrew += 1
                letters += 1
        elif ch.isalpha():
            letters += 1
    if letters == 0:
        return fallback if fallback in LANGUAGE_NAMES else "en"
    return "he" if hebrew * 10 >= letters * 3 else "en"


def answer_instruction(lang: str) -> str:
    return f"(Answer in {LANGUAGE_NAMES.get(lang, 'English')}.)"
