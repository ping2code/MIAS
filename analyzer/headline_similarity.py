import re
from difflib import SequenceMatcher


def normalize_headline(headline):
    headline = headline.lower()

    headline = re.sub(
        r"[^a-z0-9\s]",
        " ",
        headline
    )

    headline = re.sub(
        r"\s+",
        " ",
        headline
    ).strip()

    return headline


def headline_similarity(headline_a, headline_b):
    normalized_a = normalize_headline(headline_a)
    normalized_b = normalize_headline(headline_b)

    return SequenceMatcher(
        None,
        normalized_a,
        normalized_b
    ).ratio()
