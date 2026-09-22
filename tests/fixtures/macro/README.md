# Macro fixture provenance

All files are offline test data. They contain no credentials or private data.

- `cpi.html`, `ppi.html`, and `employment.html` are **synthetic** excerpts modeled
  on the BLS official news-release structure, including its embargo header,
  release number, reference-period heading, and release prose. They are not
  complete downloaded BLS documents. The report numbers and values are test data.
- `gdp.html` and `pce.html` contain **reduced captured prose and metadata** from
  the BEA releases below, retrieved September 21, 2026. Original navigation,
  scripts, charts, and most tables were removed. Relevant HTML wrappers were
  reconstructed around the captured text; inline links were reduced to text.
- `retail_sales.html` contains **reduced captured prose and metadata** from the
  Census current-release page retrieved September 21, 2026. Its small HTML
  wrapper preserves the date, headline, release number, and body selectors.
- `gdp_index.html` and `pce_index.html` are **synthetic reduced discovery pages**
  modeled on the verified official BEA Current Release links.

Official references:

- https://www.bls.gov/news.release/cpi.htm
- https://www.bls.gov/news.release/ppi.htm
- https://www.bls.gov/news.release/empsit.htm
- https://www.bea.gov/news/2026/gdp-second-estimate-and-corporate-profits-2nd-quarter-2026
- https://www.bea.gov/news/2026/personal-income-and-outlays-july-2026
- https://www.census.gov/retail/sales.html

Tests mutate these fixtures in memory to exercise new months on reused URLs,
GDP stages, explicit corrections, daylight saving, missing/future dates, parser
failures, and stale releases. A frozen test clock controls freshness. Fixture
dates and values are not assertions about the latest economic data.


BLS machine-readable fixtures: `cpi.rss`, `ppi.rss`, and `employment.rss` are
synthetic RSS 2.0 documents modeled on the officially listed BLS release feeds.
They are not captured live RSS: this environment received HTTP 403 from those
endpoints. Historical HTML fixtures remain for pure-parser compatibility tests.
The active collector tests use RSS, with API JSON mocked separately.

`bls_api.json` contains public numeric observations captured on 2026-09-22
at approximately 03:02 UTC using credential-free JSON POST requests to
https://api.bls.gov/publicAPI/v1/timeseries/data/ (HTTP 200, BLS status
REQUEST_SUCCEEDED). It retains August 2026, July 2026 and August 2025
observations for the nine series documented in docs/macro-collector.md. Other
months and response-time metadata were omitted; values and footnotes were not
changed. Live API data is not a release-vintage archive.
