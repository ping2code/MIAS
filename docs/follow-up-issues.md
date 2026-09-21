# Follow-up issues

## SEC collector executes its pipeline outside the main guard

`collector/sec_collector.py` defines `filings` and `events` under
`if __name__ == "__main__"`, but its processing loop and output execute at module
scope. Importing the module therefore references an undefined `filings` variable
and raises `NameError`.

A separate change should move processing into a callable function and put all
script execution under the main guard. Validate imports without network calls,
then cover collection, deduplication, scoring, and notification with mocks.

The Federal Reserve collector does not import the SEC collector. This defect
does not block its implementation or isolated tests; SEC code is unchanged.
