"""MIAS API service (Phase 13): one HTTP boundary over the existing sealed MIAS contracts.

The only package that imports FastAPI, Starlette or pydantic. Build the app with ``api.app.create_app(settings)``;
importing any module here starts nothing and reads nothing. See ``docs/phase13b-api-foundation.md``.
"""
