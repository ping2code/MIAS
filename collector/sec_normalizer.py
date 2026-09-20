def normalize_sec_filing(filing):
    symbol = filing["symbol"]
    form = filing["form"]
    accession = filing["accession_number"]
    filing_date = filing["filing_date"]
    primary_document = filing["primary_document"]

    accession_no_dashes = accession.replace("-", "")

    cik_map = {
        "META": "1326801",
        "NVDA": "1045810",
    }

    cik = cik_map[symbol]

    url = (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{cik}/{accession_no_dashes}/{primary_document}"
    )

    event = {
        "source": "SEC EDGAR",
        "publisher": "SEC",
        "headline": f"{symbol} filed SEC Form {form}",
        "url": url,
        "published_at": filing_date,
        "summary": f"SEC filing Form {form} for {symbol}",
        "symbols": [symbol],
        "direct_symbols": [symbol],
        "related_symbols": [],
        "relevant": True,
        "event_type": "sec_filing",
        "sec_form": form,
        "accession_number": accession,
    }

    return event
