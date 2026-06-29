from __future__ import annotations

from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title='evo service', version='artifact-runtime')

    @app.get('/healthz')
    def healthz() -> dict[str, object]:
        return {'ok': True, 'service': 'evo'}

    @app.get('/livez')
    def livez() -> dict[str, object]:
        return {'alive': True}

    @app.get('/readyz')
    def readyz() -> dict[str, object]:
        return {'ready': True}

    return app


def get_app() -> FastAPI:
    return create_app()
