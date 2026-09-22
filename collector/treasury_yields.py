"""Separate official daily yield time-series adapter; feed updated is not publication."""

from datetime import date
from decimal import Decimal
from xml.etree import ElementTree as ET

from collector.treasury_normalizer import base_event, number


DATASET = "daily_treasury_yield_curve"
YIELD_URL = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
NS = {"a": "http://www.w3.org/2005/Atom", "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata",
      "d": "http://schemas.microsoft.com/ado/2007/08/dataservices"}
BENCHMARKS = ("BC_2YEAR", "BC_10YEAR", "BC_30YEAR")


def parse_yield_observations(xml):
    if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
        raise ValueError("XML declarations unsupported")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as error:
        raise ValueError("Invalid yield XML") from error
    if root.tag != "{" + NS["a"] + "}feed":
        raise ValueError("Expected yield Atom feed")
    rows = []
    for entry in root.findall("a:entry", NS):
        props = entry.find("a:content/m:properties", NS)
        if props is None:
            raise ValueError("Missing yield properties")
        stamp = props.findtext("d:NEW_DATE", namespaces=NS)
        if not stamp or len(stamp) < 10:
            raise ValueError("Missing yield observation date")
        observed = date.fromisoformat(stamp[:10]).isoformat()
        values = {}
        for node in props:
            name = node.tag.rsplit("}", 1)[-1]
            if name.startswith("BC_") and node.attrib.get("{" + NS["m"] + "}null") != "true" and node.text:
                values[name] = number(node.text)
        if not values:
            raise ValueError("Empty yield observation")
        rows.append({"observation_date": observed, "yields_percent": values})
    return rows


def normalize_yield_events(rows, today):
    by_date = {}
    for row in rows:
        observed = row["observation_date"]
        if date.fromisoformat(observed) > today:
            raise ValueError("Future yield observation")
        if observed in by_date and by_date[observed] != row:
            raise ValueError("Conflicting yield observations")
        by_date[observed] = row
    events = []
    previous = None
    for observed, row in sorted(by_date.items()):
        event = base_event("yield_curve", f"Treasury daily par yield curve — {observed}",
                           YIELD_URL + f"?data={DATASET}&field_tdr_date_value_month={observed[:7].replace('-', '')}",
                           f"yield:{DATASET}:{observed}", "observation")
        event.update(event_type="treasury_yield_observation", observation_date=observed,
                     reference_period=observed, publication_basis="unverified_observation_only")
        metrics = {"yields_percent": dict(row["yields_percent"]), "movement_bps": {}}
        if previous:
            gap = (date.fromisoformat(observed) - date.fromisoformat(previous["observation_date"])).days
            # Avoid labeling a long missing-data interval as a daily change.
            if 0 < gap <= 4:
                metrics["prior_observation_date"] = previous["observation_date"]
                metrics["prior_yields_percent"] = dict(previous["yields_percent"])
                for name in BENCHMARKS:
                    if name in row["yields_percent"] and name in previous["yields_percent"]:
                        metrics["movement_bps"][name] = float((Decimal(str(row['yields_percent'][name])) - Decimal(str(previous['yields_percent'][name]))) * 100)
        values = row["yields_percent"]
        if all(k in values for k in ("BC_2YEAR", "BC_10YEAR")):
            metrics["spread_2s10s_bps"] = float((Decimal(str(values['BC_10YEAR'])) - Decimal(str(values['BC_2YEAR']))) * 100)
        event["metrics"] = metrics
        event["summary"] = f"Official daily closing par yields for {observed}; values in percent, changes in basis points. Publication time is not verified."
        events.append(event)
        previous = row
    return events
