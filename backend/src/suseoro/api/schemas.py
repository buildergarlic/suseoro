"""Concrete public response schemas keyed by stable OpenAPI operation IDs."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PublicSchema(BaseModel):
    """Public JSON objects reject undocumented response fields in generated clients."""

    model_config = ConfigDict(extra="forbid")


class ApiErrorField(PublicSchema):
    field: str | None
    message: str | None = None
    value: str | int | float | bool | list[str | int | float | bool | None] | None = (
        None
    )


class ApiErrorDetail(PublicSchema):
    code: str
    message: str
    request_id: str
    fields: list[ApiErrorField]


class ApiErrorResponse(PublicSchema):
    detail: ApiErrorDetail


class HealthResponse(PublicSchema):
    version: str
    database: str
    worker: str


class UserResponse(PublicSchema):
    id: str
    school_id: str
    username: str
    display_name: str
    roles: list[str]


class WorkspaceResponse(PublicSchema):
    id: str
    name: str
    status: str
    row_version: int
    created_at: str
    updated_at: str


class WorkspacePage(PublicSchema):
    items: list[WorkspaceResponse]
    next_cursor: str | None


class WorkspaceStateResponse(PublicSchema):
    state: str
    row_version: int


class ComparisonJobResponse(PublicSchema):
    job_id: str
    status: str
    workspace_status: str
    row_version: int


class UploadItemError(PublicSchema):
    code: str
    message: str


class UploadItem(PublicSchema):
    filename: str
    status: str
    source_id: str | None
    error: UploadItemError | None
    repair_obligation_id: str | None = None
    repair_generation: int | None = None


class UploadResponse(PublicSchema):
    job_id: str | None
    items: list[UploadItem]


class UploadRepairObligation(PublicSchema):
    id: str
    filename: str
    error: UploadItemError
    status: str
    generation: int
    role: str
    resolved_source_id: str | None
    created_at: str
    updated_at: str


class UploadRepairPage(PublicSchema):
    items: list[UploadRepairObligation]
    next_cursor: str | None


class SourceResponse(PublicSchema):
    id: str
    filename: str
    sha256: str
    size_bytes: int
    role: str
    status: str
    detected_format: str | None
    mapping: dict[str, str]
    row_version: int
    created_at: str
    completed_at: str | None
    latest_job_id: str | None = None
    latest_result: JobFileResult | None = None


class SourcePage(PublicSchema):
    items: list[SourceResponse]
    next_cursor: str | None


class QueuedJobResponse(PublicSchema):
    job_id: str
    status: str


class CatalogVersionResponse(PublicSchema):
    id: str
    school_id: str
    source_type: str
    import_mode: str
    status: str
    item_count: int
    as_of_local_date: str | None
    created_at: str | None
    activated_at: str | None


class CatalogDeltaResponse(PublicSchema):
    applied: bool
    status: str
    catalog_version_id: str | None
    watermark_local_date: str | None
    idempotent: bool


class MappingRequired(PublicSchema):
    headers: list[str]
    preview_rows: list[list[str | int | float | bool | None]]
    suggested_mapping: dict[str, str | None]
    required_fields: list[str]
    confidence: float
    questions: list[str]


class JobFileResult(PublicSchema):
    source_document_id: str
    filename: str
    status: str
    total_rows: int
    processed_rows: int
    error: JobError | None
    mapping_required: MappingRequired | None


class JobError(PublicSchema):
    type: str | None = None
    code: str | None = None
    message: str | None = None


class JobResponse(PublicSchema):
    id: str
    workspace_id: str | None
    type: str
    status: str
    stage: str
    progress_current: int
    progress_total: int
    error: JobError | None
    retry_count: int
    items: list[JobFileResult]


class JobCommandResponse(PublicSchema):
    id: str
    workspace_id: str | None
    type: str
    status: str
    stage: str
    progress_current: int
    progress_total: int
    error: JobError | None
    retry_count: int


class JobPage(PublicSchema):
    items: list[JobResponse]
    next_cursor: str | None


class CandidateResponse(PublicSchema):
    id: str
    workspace_id: str
    title: str
    authors: list[str]
    isbn13: str | None
    edition: str | None
    outcome: str
    reason: str | None
    quantity: int
    unit_price: int | None
    row_version: int
    updated_at: str


class CandidateSummary(PublicSchema):
    total_count: int
    candidate_count: int
    needs_review_count: int
    excluded_count: int
    unresolved_count: int
    expected_total_won: int


class CandidatePage(PublicSchema):
    items: list[CandidateResponse]
    next_cursor: str | None
    total_count: int
    summary: CandidateSummary


class CandidateMutationResponse(PublicSchema):
    id: str
    outcome: str
    quantity: int
    unit_price: int | None
    row_version: int


class CandidateBulkItem(PublicSchema):
    id: str | None
    status: str
    code: str | None = None
    outcome: str | None
    row_version: int | None


class CandidateBulkResponse(PublicSchema):
    items: list[CandidateBulkItem]


class CandidateLockResponse(PublicSchema):
    candidate_id: str
    actor_id: str
    expires_at: str


class ApprovalSummary(PublicSchema):
    id: str
    revision_number: int
    budget_won: int
    expected_total_won: int
    sha256: str
    created_at: str


class ApprovalPage(PublicSchema):
    items: list[ApprovalSummary]
    next_cursor: str | None


class ApprovalCandidatePayload(PublicSchema):
    author: str
    candidate_id: str
    isbn13: str | None
    quantity: int
    title: str
    unit_price: int


class ApprovalPayload(PublicSchema):
    budget_won: int
    candidates: list[ApprovalCandidatePayload]
    expected_total_won: int


class ApprovalDetailResponse(PublicSchema):
    revision_id: str
    revision_number: int
    sha256: str
    payload: ApprovalPayload


class ApprovalRequestResponse(PublicSchema):
    revision_id: str
    revision_number: int
    sha256: str
    expected_total_won: int
    budget_won: int
    state: str
    row_version: int


class ApprovalCancellationResponse(PublicSchema):
    cancellation_id: str
    revision_id: str
    state: str
    row_version: int


class ApprovalDecisionResponse(PublicSchema):
    revision_id: str
    decision: str
    state: str
    row_version: int


class ApprovalCommentResponse(PublicSchema):
    comment_id: str
    content: str
    created_at: str
    state: str
    row_version: int


class QuoteReconciliation(PublicSchema):
    duplicate_isbns: list[str]
    price_conflicts: list[str]


class QuoteRowResponse(PublicSchema):
    quote_row_id: str
    approval_row_id: str | None
    match_status: str
    isbn13: str | None
    title: str
    author: str
    publisher: str | None
    edition: str | None
    quantity: int
    unit_price: int | None
    list_price: int | None
    out_of_stock: bool
    line_total_won: int


class QuoteSummary(PublicSchema):
    id: str
    vendor_name: str
    total_won: int
    budget_overrun_won: int
    requires_reapproval: bool
    reconciliation: QuoteReconciliation
    created_at: str


class QuotePage(PublicSchema):
    items: list[QuoteSummary]
    next_cursor: str | None


class QuoteResponse(PublicSchema):
    quote_id: str
    revision_number: int
    state: str
    row_version: int
    total_won: int
    list_total_won: int
    discount_won: int
    budget_overrun_won: int
    out_of_stock_count: int
    missing_price_count: int
    list_mismatch_count: int
    needs_review_count: int
    unmatched_count: int
    requires_reapproval: bool
    reconciliation: QuoteReconciliation
    rows: list[QuoteRowResponse]


class QuoteMatchResponse(PublicSchema):
    quote_id: str
    parent_quote_id: str
    revision_number: int
    state: str
    row_version: int
    rows: list[QuoteRowResponse]


class OrderArtifact(PublicSchema):
    vendor_name: str
    quote_id: str
    artifact_id: str
    path: str
    sha256: str
    size_bytes: int


class OrderAllocation(PublicSchema):
    candidate_id: str
    quote_id: str
    quote_row_id: str
    vendor_name: str
    quantity: int
    unit_price: int


class OrderOverAllocation(PublicSchema):
    candidate_id: str
    expected: int
    allocated: int


class OrderDiagnostics(PublicSchema):
    missing_candidate_ids: list[str]
    duplicate_candidate_ids: list[str]
    over_allocations: list[OrderOverAllocation]
    price_conflict_candidate_ids: list[str]
    unmapped_quote_row_ids: list[str]


class OrderResponse(PublicSchema):
    status: str
    state: str
    row_version: int
    revision_id: str | None = None
    revision_number: int | None = None
    artifact_id: str | None = None
    path: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    artifacts: list[OrderArtifact] | None = None
    allocations: list[OrderAllocation] | None = None
    validation_id: str | None = None
    diagnostics: OrderDiagnostics | None = None
    external_send_performed: bool | None = None


class OrderSentResponse(PublicSchema):
    transmission_id: str
    state: str
    row_version: int
    external_send_performed: bool


class DeliverySummary(PublicSchema):
    id: str
    order_revision_id: str
    delivery_number: int
    created_at: str
    sealed_at: str | None


class ReceivingDifferenceDetails(PublicSchema):
    expected: str | int | None = None
    received: str | int | None = None
    isbn13: str | None = None
    title: str | None = None
    scanned: int | None = None
    scanned_quantity: int | None = None


class ReceivingDifference(PublicSchema):
    id: str
    kind: str
    reference_key: str
    disposition: str | None
    active: bool
    row_version: int
    details: ReceivingDifferenceDetails
    created_at: str
    updated_at: str


class DeliveryDifferenceSummary(PublicSchema):
    id: str
    kind: str
    reference_key: str
    disposition: str | None
    row_version: int
    details_json: str


class DeliveryDifferenceResult(PublicSchema):
    id: str
    kind: str
    details: ReceivingDifferenceDetails


class DeliveryPage(PublicSchema):
    items: list[DeliverySummary]
    next_cursor: str | None
    differences: list[DeliveryDifferenceSummary]
    differences_truncated: bool


class ReceivingDifferencePage(PublicSchema):
    items: list[ReceivingDifference]
    next_cursor: str | None


class DeliveryResponse(PublicSchema):
    delivery_batch_id: str
    delivery_number: int
    received_quantity: int
    differences: list[DeliveryDifferenceResult]
    state: str
    row_version: int


class ScanSessionResponse(PublicSchema):
    session_id: str
    state: str
    row_version: int


class ScanResponse(PublicSchema):
    event_id: str
    code: str
    isbn13: str | None
    scanned_quantity: int
    order_row_id: str | None


class DifferenceDispositionResponse(PublicSchema):
    difference_id: str
    disposition: str
    row_version: int


class AuditSnapshot(PublicSchema):
    serialized_json: str


class AuditEvent(PublicSchema):
    id: str
    actor_id: str | None
    actor_name: str | None
    action: str
    entity_type: str
    entity_id: str
    before: AuditSnapshot | None
    after: AuditSnapshot | None
    request_id: str
    occurred_at: str


class AuditEventPage(PublicSchema):
    items: list[AuditEvent]
    next_cursor: str | None


class V1CatalogCandidate(PublicSchema):
    id: str
    source_copy_path: str
    sha256: str
    row_count: int
    status: str
    catalog_version_id: str | None
    created_at: str
    activated_at: str | None


class V1CatalogCandidatePage(PublicSchema):
    items: list[V1CatalogCandidate]
    next_cursor: str | None


class LegacyWorkspace(PublicSchema):
    id: str
    display_name: str
    source_copy_path: str
    sha256: str
    is_read_only: bool
    imported_at: str


class LegacyWorkspacePage(PublicSchema):
    items: list[LegacyWorkspace]
    next_cursor: str | None


class V1SchoolReport(PublicSchema):
    name: str
    catalog_candidate: str | None
    catalog_candidate_row_count: int | None
    requires_catalog_confirmation: bool
    legacy_workspaces: list[str]
    catalog_caches: list[str]


class V1MigrationReport(PublicSchema):
    source_root: str
    school_count: int
    legacy_workspace_count: int
    catalog_cache_count: int
    catalog_candidate_count: int
    missing_count: int
    duplicate_count: int
    read_error_count: int
    copied_count: int
    skipped_existing_count: int
    source_delete_recommended: bool
    source_retention_message: str
    schools: list[V1SchoolReport]


class V1ActivationResponse(PublicSchema):
    id: str
    status: str
    catalog_version_id: str
    confirmed_row_count: int


class SchemaMigration(PublicSchema):
    version: str
    checksum: str


class BackupManifestResponse(PublicSchema):
    id: str
    kind: str
    created_at: str
    database_file: str
    manifest_file: str
    sha256: str
    size_bytes: int
    schema_migrations: list[SchemaMigration]
    verified: bool


class BackupPage(PublicSchema):
    items: list[BackupManifestResponse]
    next_cursor: str | None


class RestoreResponse(PublicSchema):
    restored: BackupManifestResponse
    pre_restore_backup: BackupManifestResponse


# Every JSON-producing operation is deliberately bound. The OpenAPI builder fails
# closed when a new route is added without a concrete contract here.
OPERATION_RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "getHealth": HealthResponse,
    "login": UserResponse,
    "getCurrentUser": UserResponse,
    "createWorkspace": WorkspaceResponse,
    "listWorkspaces": WorkspacePage,
    "getWorkspace": WorkspaceResponse,
    "transitionWorkspace": WorkspaceStateResponse,
    "createComparisonJob": ComparisonJobResponse,
    "uploadSources": UploadResponse,
    "listUploadRepairs": UploadRepairPage,
    "listSources": SourcePage,
    "listWorkspaceJobs": JobPage,
    "getSource": SourceResponse,
    "updateSourceMapping": SourceResponse,
    "parseSource": QueuedJobResponse,
    "stageCatalogSnapshot": CatalogVersionResponse,
    "activateCatalogVersion": CatalogVersionResponse,
    "applyCatalogDelta": CatalogDeltaResponse,
    "getJob": JobResponse,
    "retryJob": JobCommandResponse,
    "cancelJob": JobCommandResponse,
    "listCandidates": CandidatePage,
    "getCandidate": CandidateResponse,
    "updateCandidate": CandidateMutationResponse,
    "bulkDecideCandidates": CandidateBulkResponse,
    "lockCandidate": CandidateLockResponse,
    "listApprovals": ApprovalPage,
    "getApproval": ApprovalDetailResponse,
    "requestApproval": ApprovalRequestResponse,
    "cancelApproval": ApprovalCancellationResponse,
    "approveApproval": ApprovalDecisionResponse,
    "requestApprovalChanges": ApprovalDecisionResponse,
    "commentApproval": ApprovalCommentResponse,
    "listQuotes": QuotePage,
    "createQuote": QuoteResponse,
    "matchQuoteRow": QuoteMatchResponse,
    "createOrder": OrderResponse,
    "markOrderSent": OrderSentResponse,
    "listDeliveries": DeliveryPage,
    "listReceivingDifferences": ReceivingDifferencePage,
    "createDelivery": DeliveryResponse,
    "startScanSession": ScanSessionResponse,
    "recordScan": ScanResponse,
    "setReceivingDisposition": DifferenceDispositionResponse,
    "completeReceiving": WorkspaceStateResponse,
    "listAuditEvents": AuditEventPage,
    "listV1CatalogCandidates": V1CatalogCandidatePage,
    "listV1LegacyWorkspaces": LegacyWorkspacePage,
    "inspectV1Migration": V1MigrationReport,
    "runV1Migration": V1MigrationReport,
    "activateV1CatalogCandidate": V1ActivationResponse,
    "createBackup": BackupManifestResponse,
    "listBackups": BackupPage,
    "restoreBackup": RestoreResponse,
}
