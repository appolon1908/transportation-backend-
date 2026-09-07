"""Production ASGI entrypoint.

This composes the reviewed core API with durable integrations and the four portal
surfaces. Live outbound effects and external portal access remain capability-gated
and disabled by default.
"""

from app.config import get_settings
from app.integrations.api import router as integration_router
from app.integrations.health_api import router as integration_health_router
from app.main import app as app
from app.portals.admin_api import router as portal_admin_router
from app.portals.carrier_api import router as carrier_portal_router
from app.portals.customer_api import router as customer_portal_router
from app.portals.operations_api import router as operations_router
from app.portals.review_api import router as portal_review_router

settings = get_settings()
ROUTERS = (
    integration_health_router,
    integration_router,
    portal_admin_router,
    portal_review_router,
    operations_router,
    customer_portal_router,
    carrier_portal_router,
)

if not getattr(app.state, "freight_production_routers_registered", False):
    for router in ROUTERS:
        app.include_router(router)
    app.state.freight_production_routers_registered = True
    app.openapi_schema = None

app.title = settings.app_name
app.version = settings.app_version
