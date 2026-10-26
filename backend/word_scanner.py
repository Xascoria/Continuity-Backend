import json
import re
import string
from pathlib import Path

WORDS_PATH = Path(__file__).resolve().parent / "words.json"

ALLOWED_CHARS = frozenset(
    string.digits + string.ascii_letters + string.punctuation + " "
)
MIN_LENGTH = 3
MAX_LENGTH = 25

# Characters people commonly slip between letters to dodge filters
# (e.g. "d.i.g.g.e.r", "d_i_g_g_e_r", "d-i-g-g-e-r"). Whitespace is always
# treated as a separator too. Don't put leetspeak characters (@ $ ! | + ...)
# in here, they are handled by LEET_MAP.
SEPARATORS = [
    ".", ",", "_", "-", "*", "/", "\\", "'", '"', "`", "~", "^", ":", ";", "#",
]

# Leetspeak substitutions: letter/digit -> look-alike characters.
# Matching works in both directions, so a listed word containing "5" also
# matches "s"/"$", and a listed word containing "s" also matches "5"/"$".
LEET_MAP = {
    "a": ["4", "@"],
    "b": ["8"],
    "e": ["3"],
    "g": ["9", "6"],
    "i": ["1", "!", "|"],
    "l": ["1", "|"],
    "o": ["0"],
    "s": ["5", "$"],
    "t": ["7", "+"],
    "z": ["2"],
}

_SEPARATOR_RE = r"[\s" + "".join(re.escape(s) for s in SEPARATORS) + r"]*"


def _equivalents(char: str) -> set:
    """All characters that `char` may be written as (itself + leet variants)."""
    char = char.lower()
    result = {char}
    result.update(LEET_MAP.get(char, ()))
    # Reverse direction: a listed leet char (e.g. "3") also matches its letter.
    result.update(k for k, variants in LEET_MAP.items() if char in variants)
    return result


def _char_pattern(char: str) -> str:
    options = _equivalents(char)
    if len(options) == 1:
        return re.escape(char)
    return "[" + "".join(re.escape(c) for c in sorted(options)) + "]"


def _build_bad_word_regex() -> "re.Pattern[str]":
    with open(WORDS_PATH, encoding="utf-8") as f:
        words = json.load(f)

    ignored = set(SEPARATORS)
    patterns = set()
    for word in words:
        # Whitespace/separators inside a listed word are irrelevant; drop them
        # and allow optional separators between every character instead, so
        # "digger" also matches "d i g g e r" and "2 girls 1 cup" matches
        # "2girls1cup".
        chars = [c for c in str(word) if not c.isspace() and c not in ignored]
        # Words with characters outside the allowed set (e.g. emoji, accents)
        # can never appear in valid input, so there is no need to scan for them.
        if not chars or not all(c in ALLOWED_CHARS for c in chars):
            continue
        patterns.add(_SEPARATOR_RE.join(_char_pattern(c) for c in chars))

    return re.compile("|".join(sorted(patterns)), re.IGNORECASE)


_BAD_WORD_RE = _build_bad_word_regex()


def is_acceptable(text: str) -> bool:
    """Return True if `text` is allowed, False otherwise.

    Rules:
      1. Only digits, ASCII letters, punctuation and spaces are allowed.
      2. Length must be between 3 and 25 characters (inclusive).
      3. Must not contain any word from words.json, ignoring case, whitespace,
         common separators (SEPARATORS) and leetspeak (LEET_MAP), e.g.
         "D I G G E R", "d.i.g.g.e.r" and "d1gg3r" are all rejected if
         "digger" is listed.
    """
    if not isinstance(text, str):
        return False
    if not (MIN_LENGTH <= len(text) <= MAX_LENGTH):
        return False
    if not all(c in ALLOWED_CHARS for c in text):
        return False
    return _BAD_WORD_RE.search(text) is None
