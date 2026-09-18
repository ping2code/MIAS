import re


WATCHLIST = {
    "META": [
        "meta",
        "facebook",
        "instagram",
        "whatsapp",
        "threads",
        "mark zuckerberg",
    ],
    "NVDA": [
        "nvidia",
        "nvda",
        "geforce",
        "cuda",
        "jensen huang",
    ],
}


def contains_term(text, term):
    pattern = rf"\b{re.escape(term.lower())}\b"
    return re.search(pattern, text.lower()) is not None


def detect_symbols(event):
    headline = event.get("headline", "")
    summary = event.get("summary", "")

    matched_symbols = []
    direct_symbols = []
    related_symbols = []

    for symbol, keywords in WATCHLIST.items():

        headline_match = any(
            contains_term(headline, keyword)
            for keyword in keywords
        )

        summary_match = any(
            contains_term(summary, keyword)
            for keyword in keywords
        )

        if headline_match:
            matched_symbols.append(symbol)
            direct_symbols.append(symbol)

        elif summary_match:
            matched_symbols.append(symbol)
            related_symbols.append(symbol)

    event["symbols"] = matched_symbols
    event["direct_symbols"] = direct_symbols
    event["related_symbols"] = related_symbols
    event["relevant"] = bool(matched_symbols)

    return event
