from __future__ import annotations

import hmac
import ipaddress
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


class TrustedGatewayMiddleware(BaseHTTPMiddleware):
    """Reject production API traffic that did not traverse the private gateway.

    Kong removes any client-supplied proof header and adds the deployment secret.
    Network policy must also keep the backend listener private; this middleware is
    defense in depth, not a replacement for firewalling.
    """

    def _trusted_networks(self) -> list[ipaddress._BaseNetwork]:
        raw = os.getenv("TRUSTED_GATEWAY_CIDRS", "127.0.0.1/32,::1/128")
        return [ipaddress.ip_network(item.strip()) for item in raw.split(",") if item.strip()]

    @staticmethod
    def _is_health_path(path: str) -> bool:
        return path in {"/health", "/health/live", "/health/ready"}

    async def dispatch(self, request: Request, call_next) -> Response:
        if os.getenv("ENVIRONMENT", "development").lower() != "production":
            return await call_next(request)
        if self._is_health_path(request.url.path):
            return await call_next(request)

        client_host = request.client.host if request.client else ""
        try:
            client_ip = ipaddress.ip_address(client_host)
        except ValueError:
            client_ip = None
        if client_ip is None or not any(client_ip in network for network in self._trusted_networks()):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": {
                        "code": "UNTRUSTED_INGRESS",
                        "message": "API requests must traverse the trusted gateway.",
                    }
                },
            )

        expected = os.getenv("GATEWAY_SHARED_SECRET")
        supplied = request.headers.get("X-Freight-Gateway-Proof", "")
        if not expected or not hmac.compare_digest(expected, supplied):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": {
                        "code": "INVALID_GATEWAY_PROOF",
                        "message": "Gateway proof validation failed.",
                    }
                },
            )
        if request.headers.get("X-Forwarded-Proto", "").lower() != "https":
            return JSONResponse(
                status_code=400,
                content={
                    "detail": {
                        "code": "HTTPS_REQUIRED",
                        "message": "Forwarded production requests must use HTTPS.",
                    }
                },
            )
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-site")
        response.headers.setdefault("Cache-Control", "no-store")
        if os.getenv("ENVIRONMENT", "development").lower() == "production":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload"
            )
        if "Server" in response.headers:
            del response.headers["Server"]
        return response
