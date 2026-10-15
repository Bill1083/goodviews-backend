"""Local hate / swearing detector for review text. No network, no API key.

Matching is on whole words (so "class" never trips "ass"), and tolerant of the
usual evasions: leetspeak (sh1t, a$$hole), masked letters (f*ck, f**k),
stretched letters (fuuuck) and separators (f.u.c.k, f u c k).

Deliberately NOT flagged: mild words (damn, hell, crap, piss) - film reviews
use them constantly and a strike is a heavy outcome. A short list of film
titles / names that contain flagged words is removed before scanning.

check() returns (category, matched_word) for the first hit, or None."""
import re
import unicodedata

# "profanity" - strong swearing. "hate" - slurs and targeted hatred.
PROFANITY = [
    "fuck", "motherfucker", "shit", "bullshit", "bitch", "cunt", "asshole",
    "bastard", "twat", "wanker", "bollocks", "slut", "whore", "dickhead",
    "cocksucker", "pussy", "fuk", "fck",
]

HATE = [
    "nigger", "nigga", "faggot", "kike", "spic", "chink", "gook", "wetback",
    "tranny", "retard", "towelhead", "raghead", "beaner", "paki", "dyke",
    "coon", "gypo", "shemale",
]

# Phrases (checked on the plainly-normalised text) for hatred that uses no
# slur at all.
HATE_PHRASES = [
    r"kill all (?:the )?(?:jews|muslims|blacks|gays|whites|asians|immigrants|women)",
    r"(?:jews|muslims|blacks|gays|asians|immigrants) (?:should|must|need to) (?:die|be killed|be gassed)",
    r"gas the (?:jews|gays|blacks)",
    r"go back to (?:your|ur) country",
    r"white power",
    r"heil hitler",
]

# Names / titles that legitimately contain a flagged word. Stripped first.
ALLOWED_PHRASES = [
    "moby dick", "dick tracy", "dick van dyke", "van dyke", "philip k dick",
    "pussy galore", "pussycat", "pussy cat", "pussyfoot", "dyke bridge",
]

_LEET = str.maketrans({"@": "a", "4": "a", "3": "e", "1": "i", "!": "i", "0": "o", "$": "s", "5": "s", "7": "t", "+": "t"})
_PROFANITY_SUFFIXES = ("", "s", "es", "ed", "er", "ers", "ing", "in", "y", "ty", "ie", "ies", "head", "face")
# Slurs only take a plural - "spic" + "y" is "spicy", "spic" + "es" is "spices".
_HATE_SUFFIXES = ("", "s")


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _suffix_group(suffixes: tuple[str, ...]) -> str:
    return "(?:" + "|".join(s for s in suffixes if s) + ")?"


def _term_pattern(word: str, suffixes: tuple[str, ...]) -> re.Pattern[str]:
    """One term as a regex: letters may repeat (fuuuck) and may be separated
    by a single . _ - (f.u.c.k); only whole words match."""
    body = r"[._\-]?".join(rf"{re.escape(ch)}+" for ch in word)
    return re.compile(rf"(?<![a-z0-9]){body}{_suffix_group(suffixes)}(?![a-z0-9])")


_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = (
    [("hate", w, _term_pattern(w, _HATE_SUFFIXES)) for w in HATE]
    + [("profanity", w, _term_pattern(w, _PROFANITY_SUFFIXES)) for w in PROFANITY]
)
_PHRASES = [re.compile(p) for p in HATE_PHRASES]

# Every spelling of every term, for matching masked tokens like "f**king".
_FORMS: list[tuple[str, str, str]] = [
    (category, word, word + suffix)
    for category, words, suffixes in (("hate", HATE, _HATE_SUFFIXES), ("profanity", PROFANITY, _PROFANITY_SUFFIXES))
    for word in words
    for suffix in suffixes
]

_SPACED_LETTERS = re.compile(r"(?<![a-z0-9])(?:[a-z] ){3,}[a-z](?![a-z0-9])")


def _normalise(text: str) -> str:
    text = _strip_accents(text).lower()
    for phrase in ALLOWED_PHRASES:
        text = text.replace(phrase, " ")
    # "f u c k" -> "fuck"
    return _SPACED_LETTERS.sub(lambda m: m.group(0).replace(" ", ""), text)


def _deleet(text: str) -> str:
    """Leetspeak substitution applied only to tokens that also contain a
    letter, so scores like "10/10" and years are left alone."""
    return " ".join(
        tok.translate(_LEET) if re.search(r"[a-z]", tok) else tok
        for tok in text.split(" ")
    )


def _masked_match(text: str) -> tuple[str, str] | None:
    """Tokens with a mask run inside them (f*ck, f**king, sh*t). Starts and
    ends with a real letter, so star ratings like "*****" never qualify."""
    for token in re.findall(r"[a-z][a-z*#%]*[*#%]+[a-z*#%]*[a-z]", text):
        collapsed = re.sub(r"[*#%]+", "*", token)
        regex = re.compile("".join("[a-z]{1,4}" if ch == "*" else re.escape(ch) for ch in collapsed))
        for category, word, form in _FORMS:
            if regex.fullmatch(form):
                return category, word
    return None


def check(text: str | None) -> tuple[str, str] | None:
    if not text or not text.strip():
        return None
    plain = _normalise(text)
    candidates = (plain, _deleet(plain))

    for candidate in candidates:
        for pattern in _PHRASES:
            if pattern.search(candidate):
                return "hate", "hate phrase"
    for candidate in candidates:
        for category, word, pattern in _PATTERNS:
            if pattern.search(candidate):
                return category, word
    return _masked_match(plain)
