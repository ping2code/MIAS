"""Run the API: ``python -m api``. Settings come from the process environment only (see ``api.settings``).

Invalid configuration prints one safe line to stderr and exits 2 without binding. The server header is off, proxy
headers aren't trusted, and uvicorn shuts down gracefully on SIGTERM.
"""
import os
import sys


def main(environ=None, *, run=None, err=None):
    from api.app import create_app
    from api.settings import ApiConfigurationError, load_api_settings
    err = err or sys.stderr
    try:
        settings = load_api_settings(os.environ if environ is None else environ)
    except ApiConfigurationError as error:
        print(f"mias-api: configuration error: {error}", file=err)
        return 2
    if run is None:
        import uvicorn
        run = uvicorn.run
    run(create_app(settings), host=settings.host, port=settings.port, server_header=False, proxy_headers=False,
        log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
