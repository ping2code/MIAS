"""Run the API: ``python -m api``. Settings come from the process environment only (see ``api.settings``).

Invalid configuration prints one safe line to stderr and exits 2 without binding. The server header is off, proxy
headers aren't trusted, and uvicorn shuts down gracefully on SIGTERM. Observability settings (``api.observability``)
are read here too; with ``MIAS_LOG_FORMAT=json`` uvicorn's own access log is replaced by the sanitized one.
"""
import os
import sys


def main(environ=None, *, run=None, err=None):
    from api.app import create_app
    from api.observability import Telemetry, configure_logging, load_observability_settings
    from api.settings import ApiConfigurationError, load_api_settings
    err = err or sys.stderr
    environ = os.environ if environ is None else environ
    try:
        settings = load_api_settings(environ)
        observability = load_observability_settings(environ)
    except ApiConfigurationError as error:
        print(f"mias-api: configuration error: {error}", file=err)
        return 2
    if run is None:
        import uvicorn
        run = uvicorn.run
        configure_logging(observability, settings.build_id)
    telemetry = Telemetry(observability, service_version=settings.build_id)
    run(create_app(settings, telemetry=telemetry), host=settings.host, port=settings.port, server_header=False,
        proxy_headers=False, log_level="info", log_config=None, access_log=not observability.log_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
