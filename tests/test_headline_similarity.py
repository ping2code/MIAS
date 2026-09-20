from analyzer.headline_similarity import headline_similarity


headline_1 = (
    "Analyst: Meta Needs Just 115 Million Users "
    "to Ignite a $28 Billion AI Gold Rush"
)

headline_2 = (
    "Analyst: Meta Needs Just 115 Million Users "
    "to Ignite a $28 Billion AI Gold Rush — "
    "But It’s Not Likely to Happen"
)

headline_3 = (
    "Nvidia Announces New AI Data Center Partnership"
)


print(
    "Similar story:",
    headline_similarity(
        headline_1,
        headline_2
    )
)

print(
    "Different story:",
    headline_similarity(
        headline_1,
        headline_3
    )
)
