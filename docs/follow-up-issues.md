# Follow-up issues

## Resolved: SEC collector executed its pipeline outside the main guard

`collector/sec_collector.py` defined `filings` and `events` under
`if __name__ == "__main__"`, but its processing loop and output executed at module
scope, so importing the module raised `NameError`.

Fixed in SEC Phase 2O-A. The unchanged processing loop now lives in
`process_sec_filings(filings)`, and collection plus printing run under the main
guard. `tests/test_sec_pipeline.py` covers import, dedup, scoring, decisions and
Telegram handling offline, and checks that the script path matches the function.
