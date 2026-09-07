"""Read-only discovery for a server-side portal integration; never issues tokens."""
from fastapi import APIRouter, Depends

from app.config import get_settings
from app.platform.orbit_contract import (
    AuthenticationContract, BackendContract, CONTRACT_PATH,
)
from app.security import Actor, get_actor

router = APIRouter(tags=["platform-contract"])


@router.get(CONTRACT_PATH, response_model=BackendContract)
async def backend_contract(actor: Actor = Depends(get_actor)) -> BackendContract:
    # The authenticated local Actor, not an arbitrary tenant header, owns scope.
    # Discovery is not a permission grant: each advertised resource retains its
    # own permission, capability and customer/carrier binding checks.
    settings = get_settings()
    return BackendContract(
        application_version=settings.app_version,
        tenant_id=actor.tenant_id,
        authentication=AuthenticationContract(issuer=settings.oidc_issuer),
    )
