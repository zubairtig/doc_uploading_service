from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    app_name: str = "upload-doc-service"
    app_env: str = "development"
    app_debug: bool = True
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    # Used to build s3_url / s3_url_ocr_json values when storing documents.
    # Spec §15.1 decision: auth-proxy. The stored URL points back at this
    # service; clients fetch through /v1/documents/<id>/file. Also the base
    # for public upload-portal links and local file URLs.
    public_base_url: str = "http://localhost:8000"

    # Storage backend for document bytes: "s3" (default) or "local".
    # The collection-portal flow uses "local" explicitly regardless of this.
    storage_backend: str = "s3"
    local_storage_dir: str = "var/uploads"
    # When true, locally-stored files are served publicly (no auth) at /files.
    # Seam for later flipping to signed/expiring links (spec decision #5).
    public_files: bool = True

    # Collection portal: how long an unused upload link stays live.
    document_request_ttl_days: int = 7
    # Tenant the staff portal operates as. If unset, the single existing
    # tenant is used; ambiguous when more than one tenant exists.
    portal_tenant_name: Optional[str] = None

    # CORS: comma-separated list of allowed origins for browser calls
    # (e.g. an integrator frontend POSTing uploads directly). "*" = any.
    cors_allow_origins: str = "*"

    # Database
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/upload_doc"
    )

    # Redis / Celery (internal microservice queue — tenant brokers are separate)
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    # Rate-limit storage. Defaults to in-memory so tests + local dev don't
    # require Redis. Production / docker-compose override to a redis:// URI.
    rate_limit_storage_uri: str = "memory://"

    # AWS / S3
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_s3_region: str = "eu-north-1"
    aws_s3_bucket_name: str = ""
    s3_endpoint_url: Optional[str] = None

    # Presigned URL expiry for the `s3_url` exposed to consumers.
    # 7 days = SigV4 maximum. Consumers cache the URL in Redis between apps;
    # before this window expires we re-issue and re-publish (no-op for now —
    # we just regenerate on every read).
    s3_presigned_url_expires_seconds: int = 7 * 24 * 60 * 60

    # Upload constraints (spec §6.1). 50 MB to match the consuming backend's
    # cap so large scanned statements aren't rejected on the portal side.
    upload_max_bytes: int = 50 * 1024 * 1024
    upload_mime_allowlist: list[str] = [
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "image/jpeg",
        "image/png",
        "image/webp",
    ]

    # Call recordings (document_type == "creditor_liaison"): larger cap +
    # audio types. Recordings are stored under <case>/creditor_liaison/ and
    # are never OCR'd.
    recording_max_bytes: int = 200 * 1024 * 1024
    upload_recording_mime_allowlist: list[str] = [
        "audio/mpeg",
        "audio/mp3",
        "audio/wav",
        "audio/x-wav",
        "audio/wave",
        "audio/mp4",
        "audio/x-m4a",
        "audio/aac",
        "audio/ogg",
    ]

    # OCR provider (spec §10)
    ocr_api_key: str = ""
    ocr_default_language: str = "eng"
    # Include word x/y positions ("overlay") in the OCR JSON, so table-style
    # docs (payslips etc.) can be parsed by coordinates without re-OCRing.
    ocr_include_overlay: bool = True
    ocr_request_timeout_seconds: float = 60.0
    ocr_table_mode_document_types: list[str] = [
        "bank_statement",
        "creditor_statement",
        "creditor_report",
    ]
    ocr_max_retries: int = 3

    # External base64-storage service (parallel pipeline alongside S3).
    # When BASE64_SERVICE_URL is empty the push task records a permanent
    # failure so the misconfiguration is visible per-document.
    base64_service_url: str = ""
    base64_service_api_key: str = ""
    base64_service_timeout_seconds: float = 60.0
    base64_max_retries: int = 3

    # Bank-statement categorisation (LedgerFlow v2, /v1/statement-jobs).
    # When a document of one of `categoriser_document_types` lands on a case,
    # every such document on that case is sent to LedgerFlow as ONE job and
    # the categorised ledger (income, expenditure lines, transfers, gambling)
    # is stored in `categorisation_run`. Off unless switched on: bank
    # statements leave this service when it is.
    categoriser_enabled: bool = False
    categoriser_url: str = ""          # e.g. http://192.168.80.52:8320
    categoriser_api_key: str = ""      # one of LedgerFlow's SERVICE_API_KEYS
    categoriser_document_types: list[str] = ["bank_statement"]
    # A client often uploads several statements a minute apart, one link
    # each. Waiting this long after the last one sends them as one job
    # instead of one job per file.
    categoriser_debounce_seconds: float = 20.0
    categoriser_poll_seconds: float = 5.0
    # Give up on a job that has not finished in this long.
    categoriser_job_timeout_seconds: float = 30 * 60
    # Per HTTP call; the submit carries up to 30 statements.
    categoriser_request_timeout_seconds: float = 120.0
    # Times a job is re-sent after LedgerFlow lost it (restart, 404).
    categoriser_max_resubmits: int = 2

    # Income documents -> CAT (case assessment tool), for the income check.
    # Each document of a type in `cat_document_type_map` is uploaded to the
    # case in CAT (POST /api/applications/{case}/proof/upload/), CAT is asked
    # to read the case (proof/recalculate/), and CAT's "Proof of Income" rows
    # are stored in `cat_run`. Off unless switched on: documents leave this
    # service when it is. Nothing else talks to CAT from here.
    cat_enabled: bool = False
    cat_url: str = ""                 # e.g. http://192.168.80.52:2530 (CAT test)
    # Sign-in: a service Bearer key from the CAT team; until CAT issues one, a
    # CAT user's email + password (CAT's own screen API -- for testing only).
    cat_api_key: str = ""
    cat_email: str = ""
    cat_password: str = ""
    # "portal_ref:cat_case,..." where CAT knows a case by another number.
    # Unmapped references are sent as they are (CAT redirects a case reference
    # to its application id).
    cat_case_map: str = ""
    # This service's document_type -> CAT's document_type. Types not listed
    # are never sent to CAT. Override with CAT_DOCUMENT_TYPE_MAP='{"...":"..."}'.
    cat_document_type_map: dict[str, str] = {
        "income_proof": "income_proof",
        "cis_slip": "cis_slip",
        "credit_journal": "credit_journal",
        "pension_statement": "pension_statement",
        "tax_return": "tax_return",
        "hmrc_webchat": "other",
        "employment_contract": "other",
        "other": "other",
        "bank_statement": "bank_statement",
    }
    # Bank statements go to LedgerFlow for expenditure regardless. CAT also
    # needs them for its income check: benefits with no letter of their own
    # (PIP, Child Benefit) are only found on the statement. False = LedgerFlow only.
    cat_send_bank_statements: bool = True
    # Several uploads a minute apart are sent together, then read once.
    cat_debounce_seconds: float = 20.0
    cat_poll_seconds: float = 15.0
    # Give up waiting for CAT to read the case after this long.
    cat_read_timeout_seconds: float = 15 * 60
    # CAT reads one case at a time (409 while busy): ask again after this.
    cat_busy_retry_seconds: float = 30.0
    cat_request_timeout_seconds: float = 60.0

    # "Results ready" notice to Katie (AI call agent). When set, a signed POST
    # {"event", "owner_ref", "status"} is sent when CAT's income or
    # LedgerFlow's expenditure for a case is ready; Katie then reads
    # GET /v1/case-results. No figures travel in the notice itself.
    results_webhook_url: str = ""
    # HMAC-SHA256 of the raw body, sent as X-Signature: sha256=<hex>.
    results_webhook_secret: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
