import requests

from shared.logger import get_logger
from collector.sec_normalizer import normalize_sec_filing
from analyzer.sec_scoring import score_sec_event
from analyzer.deduplicator import is_duplicate
from alert_engine.decision_engine import evaluate_alert
from alert_engine.formatter import format_alert
from alert_engine.telegram_notifier import send_telegram_alert


logger = get_logger("sec_collector")


SEC_COMPANIES = {
    "META": "0001326801",
    "NVDA": "0001045810",
}


SEC_HEADERS = {
    "User-Agent": "MIAS Market Intelligence Alert System ganapathi2819@gmail.com"
}


def fetch_recent_filings(symbol, cik):
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"

    response = requests.get(
        url,
        headers=SEC_HEADERS,
        timeout=15
    )

    response.raise_for_status()

    data = response.json()

    recent = data.get("filings", {}).get("recent", {})

    filings = []

    forms = recent.get("form", [])
    accession_numbers = recent.get("accessionNumber", [])
    filing_dates = recent.get("filingDate", [])
    primary_documents = recent.get("primaryDocument", [])

    for index in range(min(10, len(forms))):
        filing = {
            "symbol": symbol,
            "form": forms[index],
            "accession_number": accession_numbers[index],
            "filing_date": filing_dates[index],
            "primary_document": primary_documents[index],
        }

        filings.append(filing)

    return filings


def collect_sec_filings():
    all_filings = []

    for symbol, cik in SEC_COMPANIES.items():
        logger.info(
            "Fetching SEC filings for %s",
            symbol
        )

        try:
            filings = fetch_recent_filings(
                symbol,
                cik
            )

            all_filings.extend(filings)

        except requests.RequestException as error:
            logger.error(
                "SEC request failed for %s: %s",
                symbol,
                error
            )

    return all_filings


if __name__ == "__main__":
    filings = collect_sec_filings()

    events = []

for filing in filings:
    event = normalize_sec_filing(filing)

    if is_duplicate(event, namespace="sec:event"):
        logger.info(
            "Duplicate SEC filing skipped: %s %s",
            event["symbols"],
            event["sec_form"]
        )
        continue

    event = score_sec_event(event)
    event = evaluate_alert(event)

    events.append(event)

    logger.info(
        "Processed SEC event symbol=%s form=%s score=%s decision=%s",
        event["symbols"],
        event["sec_form"],
        event["impact_score"],
        event["alert_decision"]
    )

    if event["alert_decision"] == "ALERT":
        message = format_alert(event)

        print(message)

        try:
            result = send_telegram_alert(message)

            logger.info(
                "Telegram SEC alert sent message_id=%s",
                result["result"]["message_id"]
            )

        except Exception as error:
            logger.error(
                "Telegram SEC alert failed: %s",
                error
            )

print("\nPROCESSED SEC EVENTS")
print("=" * 80)

for event in events:
    print(f"Source    : {event['source']}")
    print(f"Publisher : {event['publisher']}")
    print(f"Headline  : {event['headline']}")
    print(f"Symbol    : {event['symbols']}")
    print(f"Form      : {event['sec_form']}")
    print(f"Impact    : {event['impact_score']}/100")
    print(f"Level     : {event['impact_level']}")
    print(f"Decision  : {event['alert_decision']}")
    print(f"Reasons   : {event['score_reasons']}")
    print(f"URL       : {event['url']}")
    print("-" * 80)
