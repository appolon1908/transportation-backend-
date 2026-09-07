from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}


def test_live_health() -> None:
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "live"


def test_complete_v1_routes_are_registered() -> None:
    registered = {
        (method.upper(), path)
        for path, path_item in app.openapi()["paths"].items()
        for method in path_item
        if method.lower() in _HTTP_METHODS
    }
    required = {
        ("GET", "/health/live"),
        ("GET", "/health/ready"),
        ("GET", "/health/version"),
        ("GET", "/api/v1/me"),
        ("GET", "/api/v1/me/permissions"),
        ("GET", "/api/v1/capabilities"),
        ("GET", "/api/v1/customers"),
        ("POST", "/api/v1/customers"),
        ("GET", "/api/v1/customers/{customer_id}"),
        ("PATCH", "/api/v1/customers/{customer_id}"),
        ("GET", "/api/v1/customers/{customer_id}/locations"),
        ("POST", "/api/v1/customers/{customer_id}/locations"),
        ("GET", "/api/v1/customers/{customer_id}/contacts"),
        ("POST", "/api/v1/customers/{customer_id}/contacts"),
        ("GET", "/api/v1/carriers"),
        ("POST", "/api/v1/carriers"),
        ("GET", "/api/v1/carriers/{carrier_id}"),
        ("PATCH", "/api/v1/carriers/{carrier_id}"),
        ("GET", "/api/v1/carriers/{carrier_id}/contacts"),
        ("GET", "/api/v1/carriers/{carrier_id}/equipment"),
        ("GET", "/api/v1/carriers/{carrier_id}/compliance"),
        ("GET", "/api/v1/carriers/{carrier_id}/insurance"),
        ("POST", "/api/v1/carriers/{carrier_id}/approve"),
        ("POST", "/api/v1/carriers/{carrier_id}/suspend"),
        ("GET", "/api/v1/quotes"),
        ("POST", "/api/v1/quotes"),
        ("GET", "/api/v1/quotes/{quote_id}"),
        ("POST", "/api/v1/quotes/{quote_id}/send"),
        ("POST", "/api/v1/quotes/{quote_id}/accept"),
        ("POST", "/api/v1/quotes/{quote_id}/decline"),
        ("POST", "/api/v1/quotes/{quote_id}/revise"),
        ("GET", "/api/v1/shipments"),
        ("POST", "/api/v1/shipments"),
        ("GET", "/api/v1/shipments/{shipment_id}"),
        ("PATCH", "/api/v1/shipments/{shipment_id}"),
        ("POST", "/api/v1/shipments/{shipment_id}/cancel"),
        ("GET", "/api/v1/shipments/{shipment_id}/legs"),
        ("POST", "/api/v1/shipments/{shipment_id}/legs"),
        ("GET", "/api/v1/shipments/{shipment_id}/stops"),
        ("POST", "/api/v1/shipments/{shipment_id}/stops"),
        ("GET", "/api/v1/loads"),
        ("POST", "/api/v1/loads"),
        ("GET", "/api/v1/loads/{load_id}"),
        ("POST", "/api/v1/loads/{load_id}/shipment-legs"),
        ("DELETE", "/api/v1/loads/{load_id}/shipment-legs/{leg_id}"),
        ("GET", "/api/v1/loads/{load_id}/carrier-search"),
        ("GET", "/api/v1/loads/{load_id}/tenders"),
        ("POST", "/api/v1/loads/{load_id}/tenders"),
        ("POST", "/api/v1/tenders/{tender_id}/accept"),
        ("POST", "/api/v1/tenders/{tender_id}/reject"),
        ("POST", "/api/v1/tenders/{tender_id}/withdraw"),
        ("POST", "/api/v1/loads/{load_id}/dispatch"),
        ("POST", "/api/v1/loads/{load_id}/arrive"),
        ("POST", "/api/v1/loads/{load_id}/depart"),
        ("POST", "/api/v1/loads/{load_id}/deliver"),
        ("GET", "/api/v1/loads/{load_id}/tracking"),
        ("GET", "/api/v1/loads/{load_id}/positions"),
        ("GET", "/api/v1/loads/{load_id}/exceptions"),
        ("POST", "/api/v1/loads/{load_id}/tracking/manual-event"),
        ("POST", "/api/v1/integrations/tracking/{provider}/webhooks"),
        ("POST", "/api/v1/documents/upload-sessions"),
        ("POST", "/api/v1/documents/{document_id}/confirm"),
        ("GET", "/api/v1/loads/{load_id}/documents"),
        ("POST", "/api/v1/loads/{load_id}/documents"),
        ("POST", "/api/v1/loads/{load_id}/pod"),
        ("GET", "/api/v1/invoices"),
        ("POST", "/api/v1/invoices"),
        ("GET", "/api/v1/invoices/{invoice_id}"),
        ("POST", "/api/v1/invoices/{invoice_id}/approve"),
        ("POST", "/api/v1/invoices/{invoice_id}/void"),
        ("GET", "/api/v1/carrier-settlements"),
        ("POST", "/api/v1/carrier-settlements"),
        ("GET", "/api/v1/carrier-settlements/{settlement_id}"),
        ("POST", "/api/v1/carrier-settlements/{settlement_id}/approve"),
        ("GET", "/api/v1/claims"),
        ("POST", "/api/v1/claims"),
        ("GET", "/api/v1/claims/{claim_id}"),
        ("GET", "/api/v1/operations/exceptions"),
        ("POST", "/api/v1/operations/exceptions/{exception_id}/acknowledge"),
        ("POST", "/api/v1/operations/exceptions/{exception_id}/assign"),
        ("POST", "/api/v1/operations/exceptions/{exception_id}/resolve"),
        ("GET", "/api/v1/operations/dead-letters"),
        ("POST", "/api/v1/operations/dead-letters/{message_id}/replay"),
        ("GET", "/api/v1/admin/users"),
        ("GET", "/api/v1/admin/roles"),
        ("GET", "/api/v1/admin/permissions"),
        ("GET", "/api/v1/admin/capabilities"),
        ("PATCH", "/api/v1/admin/capabilities/{code}"),
    }
    missing = required - registered
    assert not missing, f"Missing API routes: {sorted(missing)}"


def test_me_fails_closed_without_identity() -> None:
    response = client.get("/api/v1/me")
    assert response.status_code == 401


def test_document_upload_fails_closed_without_storage() -> None:
    headers = {
        "X-Dev-Tenant-Id": "00000000-0000-0000-0000-000000000001",
        "X-Dev-Actor": "test",
        "X-Dev-Permissions": "*",
    }
    response = client.post("/api/v1/documents/upload-sessions", headers=headers)
    assert response.status_code == 503
    assert response.json()["code"] == "STORAGE_NOT_CONFIGURED"
