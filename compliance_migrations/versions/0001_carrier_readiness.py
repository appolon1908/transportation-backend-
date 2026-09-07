"""Carrier compliance evidence, readiness policy and write-time enforcement.

Revision ID: 0001_carrier_readiness
Requires canonical core 0005_portal_workflows.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_carrier_readiness"
down_revision = None
branch_labels = ("compliance",)
depends_on = None

TENANT_TABLES = (
    "carrier_compliance_policies",
    "carrier_authority_records",
    "carrier_insurance_records",
    "carrier_safety_records",
    "carrier_compliance_overrides",
    "carrier_readiness_decisions",
)


def _execute(sql: str) -> None:
    """Execute exactly one top-level statement for asyncpg compatibility."""

    op.execute(sa.text(sql))


def _tenant_policy(table_name: str) -> None:
    tenant_expression = (
        "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    )
    policy_name = f"{table_name}_api_tenant"
    _execute(f'ALTER TABLE "{table_name}" ENABLE ROW LEVEL SECURITY')
    _execute(f'ALTER TABLE "{table_name}" FORCE ROW LEVEL SECURITY')
    _execute(f'DROP POLICY IF EXISTS "{policy_name}" ON "{table_name}"')
    _execute(
        f'''CREATE POLICY "{policy_name}" ON "{table_name}"
            TO freight_api
            USING ({tenant_expression})
            WITH CHECK ({tenant_expression})'''
    )


def upgrade() -> None:
    bind = op.get_bind()
    core_head = bind.execute(
        sa.text("SELECT version_num FROM alembic_version")
    ).scalar_one_or_none()
    if core_head != "0005_portal_workflows":
        raise RuntimeError(
            "Compliance migration requires core schema head 0005_portal_workflows; "
            f"found {core_head!r}."
        )
    integration_table = bind.execute(
        sa.text("SELECT to_regclass('public.integration_connections')")
    ).scalar_one_or_none()
    if integration_table is None:
        raise RuntimeError(
            "Compliance migration requires canonical core integration tables."
        )

    _execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "carrier_compliance_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "authority_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "minimum_auto_liability",
            sa.Numeric(18, 2),
            nullable=False,
            server_default="1000000",
        ),
        sa.Column(
            "minimum_cargo",
            sa.Numeric(18, 2),
            nullable=False,
            server_default="100000",
        ),
        sa.Column(
            "currency",
            sa.String(length=3),
            nullable=False,
            server_default="USD",
        ),
        sa.Column(
            "expiry_buffer_days",
            sa.Integer(),
            nullable=False,
            server_default="7",
        ),
        sa.Column(
            "max_safety_age_days",
            sa.Integer(),
            nullable=False,
            server_default="90",
        ),
        sa.Column(
            "allowed_safety_ratings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text(
                "'[\"SATISFACTORY\",\"CONDITIONAL\",\"NOT_RATED\"]'::jsonb"
            ),
        ),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_by", sa.String(length=220), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "expiry_buffer_days BETWEEN 0 AND 365",
            name="ck_compliance_expiry_buffer",
        ),
        sa.CheckConstraint(
            "max_safety_age_days BETWEEN 1 AND 730",
            name="ck_compliance_safety_age",
        ),
        sa.CheckConstraint(
            "minimum_auto_liability >= 0 AND minimum_cargo >= 0",
            name="ck_compliance_minimum_limits",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "version",
            name="uq_compliance_policy_tenant_version",
        ),
    )
    op.create_index(
        "ix_compliance_policy_active",
        "carrier_compliance_policies",
        ["tenant_id", "enabled", "version"],
    )
    op.create_index(
        "ix_carrier_compliance_policies_tenant_id",
        "carrier_compliance_policies",
        ["tenant_id"],
    )
    _execute(
        "CREATE UNIQUE INDEX uq_compliance_policy_one_enabled "
        "ON carrier_compliance_policies (tenant_id) WHERE enabled"
    )

    op.create_table(
        "carrier_authority_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "carrier_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("carriers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("authority_type", sa.String(length=60), nullable=False),
        sa.Column("authority_number_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("issued_at", sa.Date()),
        sa.Column("expires_at", sa.Date()),
        sa.Column("source", sa.String(length=120), nullable=False),
        sa.Column("source_record_id", sa.String(length=220)),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_by", sa.String(length=220), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "carrier_id",
            "authority_type",
            "authority_number_hash",
            name="uq_carrier_authority_identity",
        ),
    )
    op.create_index(
        "ix_carrier_authority_ready",
        "carrier_authority_records",
        ["tenant_id", "carrier_id", "status", "expires_at"],
    )
    op.create_index(
        "ix_carrier_authority_records_tenant_id",
        "carrier_authority_records",
        ["tenant_id"],
    )
    op.create_index(
        "ix_carrier_authority_records_carrier_id",
        "carrier_authority_records",
        ["carrier_id"],
    )

    op.create_table(
        "carrier_insurance_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "carrier_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("carriers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("insurance_type", sa.String(length=60), nullable=False),
        sa.Column("policy_number_hash", sa.String(length=64), nullable=False),
        sa.Column("insurer_name", sa.String(length=220), nullable=False),
        sa.Column("limit_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column(
            "currency",
            sa.String(length=3),
            nullable=False,
            server_default="USD",
        ),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("effective_at", sa.Date(), nullable=False),
        sa.Column("expires_at", sa.Date(), nullable=False),
        sa.Column("evidence_document_id", postgresql.UUID(as_uuid=True)),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_by", sa.String(length=220), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "limit_amount >= 0",
            name="ck_carrier_insurance_limit",
        ),
        sa.CheckConstraint(
            "expires_at > effective_at",
            name="ck_carrier_insurance_window",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "carrier_id",
            "insurance_type",
            "policy_number_hash",
            name="uq_carrier_insurance_identity",
        ),
    )
    op.create_index(
        "ix_carrier_insurance_ready",
        "carrier_insurance_records",
        ["tenant_id", "carrier_id", "insurance_type", "status", "expires_at"],
    )
    op.create_index(
        "ix_carrier_insurance_records_tenant_id",
        "carrier_insurance_records",
        ["tenant_id"],
    )
    op.create_index(
        "ix_carrier_insurance_records_carrier_id",
        "carrier_insurance_records",
        ["carrier_id"],
    )

    op.create_table(
        "carrier_safety_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "carrier_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("carriers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rating", sa.String(length=60), nullable=False),
        sa.Column(
            "out_of_service",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("measured_at", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=120), nullable=False),
        sa.Column("source_record_id", sa.String(length=220), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_by", sa.String(length=220), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "carrier_id",
            "source",
            "source_record_id",
            name="uq_carrier_safety_source_record",
        ),
    )
    op.create_index(
        "ix_carrier_safety_latest",
        "carrier_safety_records",
        ["tenant_id", "carrier_id", "measured_at"],
    )
    op.create_index(
        "ix_carrier_safety_records_tenant_id",
        "carrier_safety_records",
        ["tenant_id"],
    )
    op.create_index(
        "ix_carrier_safety_records_carrier_id",
        "carrier_safety_records",
        ["carrier_id"],
    )

    op.create_table(
        "carrier_compliance_overrides",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "carrier_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("carriers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("reason_code", sa.String(length=120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.String(length=220), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_by", sa.String(length=220)),
        sa.CheckConstraint(
            "expires_at > starts_at",
            name="ck_compliance_override_window",
        ),
    )
    op.create_index(
        "ix_compliance_override_active",
        "carrier_compliance_overrides",
        ["tenant_id", "carrier_id", "action", "expires_at"],
    )
    op.create_index(
        "ix_carrier_compliance_overrides_tenant_id",
        "carrier_compliance_overrides",
        ["tenant_id"],
    )
    op.create_index(
        "ix_carrier_compliance_overrides_carrier_id",
        "carrier_compliance_overrides",
        ["carrier_id"],
    )

    op.create_table(
        "carrier_readiness_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "carrier_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("carriers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("ready", sa.Boolean(), nullable=False),
        sa.Column(
            "reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("policy_version", sa.Integer()),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("evaluated_by", sa.String(length=220), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=False),
        sa.Column(
            "evaluated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_readiness_decision_carrier",
        "carrier_readiness_decisions",
        ["tenant_id", "carrier_id", "evaluated_at"],
    )
    op.create_index(
        "ix_readiness_decision_action",
        "carrier_readiness_decisions",
        ["tenant_id", "action", "ready", "evaluated_at"],
    )
    op.create_index(
        "ix_carrier_readiness_decisions_tenant_id",
        "carrier_readiness_decisions",
        ["tenant_id"],
    )
    op.create_index(
        "ix_carrier_readiness_decisions_carrier_id",
        "carrier_readiness_decisions",
        ["carrier_id"],
    )

    for table_name in TENANT_TABLES:
        _tenant_policy(table_name)

    _execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
        + ", ".join(f'"{name}"' for name in TENANT_TABLES)
        + " TO freight_api"
    )

    _execute(
        r'''
CREATE OR REPLACE FUNCTION public.freight_carrier_readiness(
    p_tenant_id uuid,
    p_carrier_id uuid,
    p_action text
)
RETURNS TABLE (
    ready boolean,
    reasons jsonb,
    policy_version integer,
    input_hash text
)
LANGUAGE plpgsql
SECURITY DEFINER
STABLE
SET search_path = pg_catalog, public
AS $function$
DECLARE
    v_context_tenant uuid;
    v_policy public.carrier_compliance_policies%ROWTYPE;
    v_safety public.carrier_safety_records%ROWTYPE;
    v_carrier_active boolean;
    v_authority_ok boolean := false;
    v_auto_ok boolean := false;
    v_cargo_ok boolean := false;
    v_override_ids jsonb := '[]'::jsonb;
    v_reasons jsonb := '[]'::jsonb;
    v_input jsonb;
    v_hard_block boolean := false;
    v_override_available boolean := false;
BEGIN
    v_context_tenant := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
    IF v_context_tenant IS NULL OR v_context_tenant <> p_tenant_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'TENANT_CONTEXT_MISMATCH';
    END IF;

    SELECT c.is_active
    INTO v_carrier_active
    FROM public.carriers AS c
    WHERE c.id = p_carrier_id
      AND c.tenant_id = p_tenant_id;

    IF NOT FOUND THEN
        v_reasons := v_reasons || jsonb_build_array('CARRIER_NOT_FOUND');
    ELSIF NOT COALESCE(v_carrier_active, false) THEN
        v_reasons := v_reasons || jsonb_build_array('CARRIER_INACTIVE');
    END IF;

    SELECT policy.*
    INTO v_policy
    FROM public.carrier_compliance_policies AS policy
    WHERE policy.tenant_id = p_tenant_id
      AND policy.enabled
    ORDER BY policy.version DESC
    LIMIT 1;

    IF v_policy.id IS NULL THEN
        v_reasons := v_reasons || jsonb_build_array('NO_ACTIVE_POLICY');
    ELSE
        IF v_policy.authority_required THEN
            SELECT EXISTS (
                SELECT 1
                FROM public.carrier_authority_records AS authority
                WHERE authority.tenant_id = p_tenant_id
                  AND authority.carrier_id = p_carrier_id
                  AND upper(authority.status) = 'ACTIVE'
                  AND (
                      authority.expires_at IS NULL
                      OR authority.expires_at >= CURRENT_DATE + v_policy.expiry_buffer_days
                  )
            ) INTO v_authority_ok;
            IF NOT v_authority_ok THEN
                v_reasons := v_reasons || jsonb_build_array('MISSING_ACTIVE_AUTHORITY');
            END IF;
        ELSE
            v_authority_ok := true;
        END IF;

        SELECT EXISTS (
            SELECT 1
            FROM public.carrier_insurance_records AS insurance
            WHERE insurance.tenant_id = p_tenant_id
              AND insurance.carrier_id = p_carrier_id
              AND upper(insurance.insurance_type) = 'AUTO_LIABILITY'
              AND upper(insurance.status) = 'ACTIVE'
              AND insurance.effective_at <= CURRENT_DATE
              AND insurance.expires_at >= CURRENT_DATE + v_policy.expiry_buffer_days
              AND insurance.limit_amount >= v_policy.minimum_auto_liability
              AND upper(insurance.currency) = upper(v_policy.currency)
        ) INTO v_auto_ok;
        IF NOT v_auto_ok THEN
            v_reasons := v_reasons || jsonb_build_array('AUTO_LIABILITY_NOT_READY');
        END IF;

        SELECT EXISTS (
            SELECT 1
            FROM public.carrier_insurance_records AS insurance
            WHERE insurance.tenant_id = p_tenant_id
              AND insurance.carrier_id = p_carrier_id
              AND upper(insurance.insurance_type) = 'CARGO'
              AND upper(insurance.status) = 'ACTIVE'
              AND insurance.effective_at <= CURRENT_DATE
              AND insurance.expires_at >= CURRENT_DATE + v_policy.expiry_buffer_days
              AND insurance.limit_amount >= v_policy.minimum_cargo
              AND upper(insurance.currency) = upper(v_policy.currency)
        ) INTO v_cargo_ok;
        IF NOT v_cargo_ok THEN
            v_reasons := v_reasons || jsonb_build_array('CARGO_INSURANCE_NOT_READY');
        END IF;

        SELECT safety.*
        INTO v_safety
        FROM public.carrier_safety_records AS safety
        WHERE safety.tenant_id = p_tenant_id
          AND safety.carrier_id = p_carrier_id
        ORDER BY safety.measured_at DESC, safety.verified_at DESC
        LIMIT 1;

        IF v_safety.id IS NULL THEN
            v_reasons := v_reasons || jsonb_build_array('MISSING_SAFETY_RECORD');
        ELSE
            IF v_safety.out_of_service THEN
                v_reasons := v_reasons || jsonb_build_array('OUT_OF_SERVICE');
            END IF;
            IF upper(v_safety.rating) = 'UNSATISFACTORY' THEN
                v_reasons := v_reasons || jsonb_build_array('UNSATISFACTORY_SAFETY');
            ELSIF NOT (v_policy.allowed_safety_ratings ? upper(v_safety.rating)) THEN
                v_reasons := v_reasons || jsonb_build_array('DISALLOWED_SAFETY_RATING');
            END IF;
            IF v_safety.measured_at < CURRENT_DATE - v_policy.max_safety_age_days THEN
                v_reasons := v_reasons || jsonb_build_array('STALE_SAFETY_RECORD');
            END IF;
        END IF;
    END IF;

    SELECT COALESCE(jsonb_agg(override.id ORDER BY override.expires_at), '[]'::jsonb)
    INTO v_override_ids
    FROM public.carrier_compliance_overrides AS override
    WHERE override.tenant_id = p_tenant_id
      AND override.carrier_id = p_carrier_id
      AND override.active
      AND upper(override.action) = upper(p_action)
      AND override.starts_at <= timezone('utc', now())
      AND override.expires_at > timezone('utc', now())
      AND override.revoked_at IS NULL;

    v_override_available := jsonb_array_length(v_override_ids) > 0;
    v_hard_block :=
        v_reasons ? 'CARRIER_NOT_FOUND'
        OR v_reasons ? 'CARRIER_INACTIVE'
        OR v_reasons ? 'NO_ACTIVE_POLICY'
        OR v_reasons ? 'OUT_OF_SERVICE'
        OR v_reasons ? 'UNSATISFACTORY_SAFETY';

    IF v_override_available AND NOT v_hard_block AND jsonb_array_length(v_reasons) > 0 THEN
        v_reasons := jsonb_build_array('OVERRIDE_APPLIED');
    END IF;

    v_input := jsonb_build_object(
        'tenant_id', p_tenant_id,
        'carrier_id', p_carrier_id,
        'action', upper(p_action),
        'policy', CASE WHEN v_policy.id IS NULL THEN NULL ELSE jsonb_build_object(
            'id', v_policy.id,
            'version', v_policy.version,
            'authority_required', v_policy.authority_required,
            'minimum_auto_liability', v_policy.minimum_auto_liability,
            'minimum_cargo', v_policy.minimum_cargo,
            'currency', v_policy.currency,
            'expiry_buffer_days', v_policy.expiry_buffer_days,
            'max_safety_age_days', v_policy.max_safety_age_days,
            'allowed_safety_ratings', v_policy.allowed_safety_ratings
        ) END,
        'authority', (
            SELECT COALESCE(jsonb_agg(jsonb_build_object(
                'id', authority.id,
                'status', authority.status,
                'expires_at', authority.expires_at,
                'evidence_hash', authority.evidence_hash,
                'version', authority.version
            ) ORDER BY authority.verified_at), '[]'::jsonb)
            FROM public.carrier_authority_records AS authority
            WHERE authority.tenant_id = p_tenant_id
              AND authority.carrier_id = p_carrier_id
        ),
        'insurance', (
            SELECT COALESCE(jsonb_agg(jsonb_build_object(
                'id', insurance.id,
                'type', insurance.insurance_type,
                'status', insurance.status,
                'limit', insurance.limit_amount,
                'currency', insurance.currency,
                'effective_at', insurance.effective_at,
                'expires_at', insurance.expires_at,
                'evidence_hash', insurance.evidence_hash,
                'version', insurance.version
            ) ORDER BY insurance.verified_at), '[]'::jsonb)
            FROM public.carrier_insurance_records AS insurance
            WHERE insurance.tenant_id = p_tenant_id
              AND insurance.carrier_id = p_carrier_id
        ),
        'safety', CASE WHEN v_safety.id IS NULL THEN NULL ELSE jsonb_build_object(
            'id', v_safety.id,
            'rating', v_safety.rating,
            'out_of_service', v_safety.out_of_service,
            'measured_at', v_safety.measured_at,
            'evidence_hash', v_safety.evidence_hash
        ) END,
        'override_ids', v_override_ids,
        'reasons', v_reasons
    );

    RETURN QUERY SELECT
        jsonb_array_length(v_reasons) = 0 OR v_reasons = jsonb_build_array('OVERRIDE_APPLIED'),
        v_reasons,
        CASE WHEN v_policy.id IS NULL THEN NULL ELSE v_policy.version END,
        encode(digest(convert_to(v_input::text, 'UTF8'), 'sha256'), 'hex');
END;
$function$
'''
    )
    _execute(
        "REVOKE ALL ON FUNCTION public.freight_carrier_readiness(uuid, uuid, text) "
        "FROM PUBLIC"
    )
    _execute(
        "GRANT EXECUTE ON FUNCTION public.freight_carrier_readiness(uuid, uuid, text) "
        "TO freight_api, freight_worker"
    )

    _execute(
        r'''
CREATE OR REPLACE FUNCTION public.enforce_freight_carrier_readiness()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    v_tenant_id uuid;
    v_carrier_id uuid;
    v_status text;
    v_action text := upper(TG_ARGV[0]);
    v_required_status text := CASE
        WHEN TG_NARGS > 1 THEN upper(TG_ARGV[1])
        ELSE NULL
    END;
    v_ready boolean;
    v_reasons jsonb;
BEGIN
    v_tenant_id := NULLIF(to_jsonb(NEW)->>'tenant_id', '')::uuid;
    v_carrier_id := NULLIF(to_jsonb(NEW)->>'carrier_id', '')::uuid;
    v_status := upper(COALESCE(to_jsonb(NEW)->>'status', ''));

    IF v_tenant_id IS NULL OR v_carrier_id IS NULL THEN
        RETURN NEW;
    END IF;
    IF v_required_status IS NOT NULL AND v_status <> v_required_status THEN
        RETURN NEW;
    END IF;
    IF v_action IN ('TENDER', 'ASSIGN')
       AND v_status IN ('CANCELLED', 'REJECTED', 'EXPIRED', 'VOID') THEN
        RETURN NEW;
    END IF;

    SELECT result.ready, result.reasons
    INTO v_ready, v_reasons
    FROM public.freight_carrier_readiness(
        v_tenant_id,
        v_carrier_id,
        v_action
    ) AS result;

    IF NOT v_ready THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'CARRIER_NOT_READY',
            DETAIL = v_reasons::text,
            HINT = 'Record current authority, insurance and safety evidence or use an approved non-hard-block override.';
    END IF;
    RETURN NEW;
END;
$function$
'''
    )
    _execute(
        "REVOKE ALL ON FUNCTION public.enforce_freight_carrier_readiness() FROM PUBLIC"
    )

    _execute(
        r'''
DO $block$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'tenders'
          AND column_name = 'carrier_id'
    ) AND EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'tenders'
          AND column_name = 'tenant_id'
    ) THEN
        DROP TRIGGER IF EXISTS trg_tenders_carrier_readiness ON public.tenders;
        CREATE TRIGGER trg_tenders_carrier_readiness
        BEFORE INSERT OR UPDATE ON public.tenders
        FOR EACH ROW
        EXECUTE FUNCTION public.enforce_freight_carrier_readiness('TENDER');
    END IF;

    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'assignments'
          AND column_name = 'carrier_id'
    ) AND EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'assignments'
          AND column_name = 'tenant_id'
    ) THEN
        DROP TRIGGER IF EXISTS trg_assignments_carrier_readiness ON public.assignments;
        CREATE TRIGGER trg_assignments_carrier_readiness
        BEFORE INSERT OR UPDATE ON public.assignments
        FOR EACH ROW
        EXECUTE FUNCTION public.enforce_freight_carrier_readiness('ASSIGN');
    END IF;

    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'loads'
          AND column_name = 'carrier_id'
    ) AND EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'loads'
          AND column_name = 'tenant_id'
    ) AND EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'loads'
          AND column_name = 'status'
    ) THEN
        DROP TRIGGER IF EXISTS trg_loads_dispatch_readiness ON public.loads;
        CREATE TRIGGER trg_loads_dispatch_readiness
        BEFORE INSERT OR UPDATE ON public.loads
        FOR EACH ROW
        EXECUTE FUNCTION public.enforce_freight_carrier_readiness(
            'DISPATCH',
            'DISPATCHED'
        );
    END IF;
END;
$block$
'''
    )


def downgrade() -> None:
    _execute(
        r'''
DO $block$
BEGIN
    IF to_regclass('public.tenders') IS NOT NULL THEN
        DROP TRIGGER IF EXISTS trg_tenders_carrier_readiness ON public.tenders;
    END IF;
    IF to_regclass('public.assignments') IS NOT NULL THEN
        DROP TRIGGER IF EXISTS trg_assignments_carrier_readiness ON public.assignments;
    END IF;
    IF to_regclass('public.loads') IS NOT NULL THEN
        DROP TRIGGER IF EXISTS trg_loads_dispatch_readiness ON public.loads;
    END IF;
END;
$block$
'''
    )
    _execute("DROP FUNCTION IF EXISTS public.enforce_freight_carrier_readiness()")
    _execute(
        "DROP FUNCTION IF EXISTS public.freight_carrier_readiness(uuid, uuid, text)"
    )

    for table_name in reversed(TENANT_TABLES):
        _execute(
            f'DROP POLICY IF EXISTS "{table_name}_api_tenant" ON "{table_name}"'
        )

    op.drop_table("carrier_readiness_decisions")
    op.drop_table("carrier_compliance_overrides")
    op.drop_table("carrier_safety_records")
    op.drop_table("carrier_insurance_records")
    op.drop_table("carrier_authority_records")
    op.drop_table("carrier_compliance_policies")
