"""Conservative clause-level official action and exposure rules.

Generic geography, industry, and policy keywords never establish relevance.
Each accepted rule records the action clause and its matched scope.
"""

import re

PRODUCTS = {
    "advanced_computing": r"\b(?:advanced[- ]computing (?:chips|items|integrated circuits)|AI accelerators|AI chips|graphics processing units)\b",
    "semiconductor_supply": r"\b(?:semiconductor manufacturing equipment|advanced packaging|semiconductor (?:production|fabrication|supply)|chip fabrication)\b",
    "platform_data": r"\b(?:personal data|targeted advertising|social media platforms|digital advertising|cross-border data transfers)\b",
}
ENTITIES = {"NVDA": r"\b(?:NVIDIA|NVIDIA Corporation)\b",
            "META": r"\b(?:Meta Platforms(?:,? Inc\.?)?|Facebook|Instagram|WhatsApp)\b"}
JURISDICTIONS = {"China": r"\b(?:China|PRC|People.s Republic of China)\b",
                 "Taiwan": r"\bTaiwan\b", "United States": r"\b(?:United States|U\.S\.)\b"}
# Action verbs must occur in the same clause as affected scope and policy mechanism.
VERBS = r"\b(?:proposes?|proposed|imposes?|imposed|restricts?|restricted|prohibits?|prohibited|requires?|required|adopts?|adopted|issues?|issued|amends?|amended|removes?|removed|rescinds?|rescinded|expands?|expanded|adds?|added|designates?|designated|files?|filed|halts?|halted|suspends?|suspended|orders?|ordered)\b"
MECHANISMS = (
    ("operational_disruption", "supply_disruption", r"\b(?:production|shipping|exports|operations)\b", r"\b(?:halted|suspended|closure|interruption)\b"),
    ("sanctions_action", "sanctions", r"\b(?:sanctions|blocking|designat(?:es|ed|ion)|asset freeze)\b", VERBS),
    ("trade_action", "trade_restriction", r"\b(?:tariffs?|import (?:ban|restriction)|duties|Section 301)\b", VERBS),
    ("policy_action", "entity_list", r"\bEntity List\b", VERBS),
    ("policy_action", "export_controls", r"\b(?:export controls?|export licens(?:e|ing)|licens(?:e|ing) requirements?|export restriction)\b", VERBS),
    ("policy_action", "investment_restriction", r"\b(?:outbound investment|investment restriction)\b", VERBS),
    ("regulatory_action", "regulatory_remedy", r"\b(?:order|remedy|settlement|consent decree|complaint|prohibition)\b", VERBS),
)


def detect_relevance(event):
    text = event["body"]
    # Avoid promoting political statements, hypothetical actions or negations.
    if re.match(r"(?:remarks|speech|readout|meeting|opinion)\b", event["headline"], re.I):
        return event
    candidates = []
    for clause in re.split(r"(?<=[.!?;])\s+|\n+", text):
        if re.search(r"\b(?:not|may|might|could|would|considering|urges?|urged|historically|previously)\b", clause, re.I):
            continue
        entities = [s for s, p in ENTITIES.items() if re.search(p, clause, re.I)]
        products = [s for s, p in PRODUCTS.items() if re.search(p, clause, re.I)]
        places = [s for s, p in JURISDICTIONS.items() if re.search(p, clause, re.I)]
        if not entities and not products:
            continue
        for family, category, mechanism, action in MECHANISMS:
            if not re.search(mechanism, clause, re.I) or not re.search(action, clause, re.I):
                continue
            if family == "operational_disruption" and event["agency"] != "moea":
                continue
            proposed = event.get("document_type") == "Proposed Rule" or bool(re.search(r"\b(?:proposes?|proposed|investigation)\b", event["headline"] + " " + clause, re.I))
            stage = "proposed" if proposed else "complaint" if re.search(r"\bcomplaint\b", clause, re.I) else "adopted"
            if family == "operational_disruption":
                stage = "confirmed_disruption"
            elif re.search(r"\b(?:rescinds?|rescinded|removes?|removed)\b", clause, re.I):
                stage = "withdrawn"
            elif re.search(r"\b(?:amends?|amended)\b", clause, re.I):
                stage = "amended"
            candidates.append((family, category, stage, clause, entities, products, places))
            break
    # A multi-action notice requires subdivision; v1 withholds rather than merging.
    kinds = {(x[0], x[1], x[2]) for x in candidates}
    if len(kinds) != 1:
        event["relevance_reasons"] = ["No supported action/scope clause" if not kinds else "Multiple action kinds: manual subdivision required"]
        return event
    family, category, stage = next(iter(kinds))
    event.update(event_type=family, geopolitical_category=category, policy_stage=stage,
                 legal_status=stage, policy_action=category, relevant=True)
    for _, _, _, clause, entities, products, places in candidates:
        direct = entities
        related = []
        if any(p in products for p in ("advanced_computing", "semiconductor_supply")):
            related.append("NVDA")
        if "platform_data" in products:
            related.append("META")
        for symbol in sorted(set(direct + related)):
            rule = "explicit_company_action_v1" if symbol in direct else (
                "nvda_advanced_compute_supply_v1" if symbol == "NVDA" else "meta_platform_data_scope_v1")
            evidence = {"symbol": symbol, "rule": rule, "matched_entities": entities,
                        "matched_products": products, "matched_jurisdictions": places,
                        "policy_scope": category, "source_url": event["url"],
                        "document_id": event["document_id"], "quote": clause,
                        "start": text.find(clause), "end": text.find(clause) + len(clause)}
            event["evidence"].append(evidence)
            event["relevance_reasons"].append(f"{symbol}: {rule}; {category}")
        event["direct_symbols"].extend(direct)
        event["related_symbols"].extend(related)
        event["matched_entities"].extend(entities)
        event["matched_products"].extend(products)
        event["matched_jurisdictions"].extend(places)
    for key in ("direct_symbols", "related_symbols", "matched_entities", "matched_products", "matched_jurisdictions"):
        event[key] = sorted(set(event[key]))
    event["related_symbols"] = sorted(set(event["related_symbols"]) - set(event["direct_symbols"]))
    event["symbols"] = sorted(set(event["direct_symbols"] + event["related_symbols"]))
    event["policy_scope"] = [category]
    # MOEA economic announcements have a native incident/release identity. No military inference.
    if event["agency"] == "moea" and family == "operational_disruption":
        event["identity_anchors"].append(event["document_id"])
    return event
