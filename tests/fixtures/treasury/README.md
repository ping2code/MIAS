# Treasury fixture provenance

- `auctions.json`: public TreasuryDirect response excerpts captured September 22,
  2026 using `https://www.treasurydirect.gov/TA_WS/securities/search?format=json&auctionDate=2026-09-21`.
  HTTP 200, application/json. Two completed bill auctions; unrelated fields were
  omitted, retained values were not changed. There are no credentials in this data.
- `yields.xml`: public Treasury nominal par-curve XML captured September 22, 2026
  using `https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value_month=202609`.
  HTTP 200, text/xml; charset=UTF-8. The last two entries (September 18 and 21) were
  retained; namespace prefixes were reserialized by ElementTree. Dates, values and
  the feed/entry updated metadata were preserved. Updated metadata is not treated
  as release publication time.
- `refunding.html` and `debt_letter.txt`: synthetic content for deterministic
  tests, modeled on official Treasury release HTML/letter structure. The refunding
  date and amounts are not asserted to be an actual Treasury release.
- PDF tests generate a synthetic text-bearing or blank/encrypted PDF in memory;
  no live document is fetched during tests.
