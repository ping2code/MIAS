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


def detect_symbols(event):
    text = (
        f"{event.get('headline', '')} "
        f"{event.get('summary', '')}"
    ).lower()

    matched_symbols = []

    for symbol, keywords in WATCHLIST.items():
        for keyword in keywords:
            if keyword in text:
                matched_symbols.append(symbol)
                break

    event["symbols"] = matched_symbols
    event["relevant"] = len(matched_symbols) > 0

    return event
