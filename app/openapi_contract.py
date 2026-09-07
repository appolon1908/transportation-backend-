from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}


def install_openapi_contract(
    app: FastAPI,
    *,
    service_name: str,
    migration_head: str,
) -> None:
    """Install one production OpenAPI contract for header-based bearer authentication.

    The runtime accepts the standard ``Authorization: Bearer`` header. FastAPI's
    ordinary ``Header`` dependency documents that as a raw parameter, so this
    adapter promotes it to an HTTP bearer security scheme without changing the
    fail-closed authentication implementation.
    """

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema

        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
            tags=app.openapi_tags,
            servers=app.servers,
        )
        components = schema.setdefault("components", {})
        security_schemes = components.setdefault("securitySchemes", {})
        security_schemes["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "OIDC access token issued for the Freight Platform API audience.",
        }

        for path_item in schema.get("paths", {}).values():
            if not isinstance(path_item, dict):
                continue
            for method, operation in path_item.items():
                if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                    continue
                parameters = operation.get("parameters", [])
                authorization_parameter = any(
                    isinstance(parameter, dict)
                    and str(parameter.get("in", "")).lower() == "header"
                    and str(parameter.get("name", "")).lower() == "authorization"
                    for parameter in parameters
                )
                if not authorization_parameter:
                    continue
                operation["parameters"] = [
                    parameter
                    for parameter in parameters
                    if not (
                        isinstance(parameter, dict)
                        and str(parameter.get("in", "")).lower() == "header"
                        and str(parameter.get("name", "")).lower() == "authorization"
                    )
                ]
                operation["security"] = [{"BearerAuth": []}]

        schema.setdefault("info", {})["x-service-name"] = service_name
        schema["info"]["x-migration-head"] = migration_head
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
    app.openapi_schema = None
