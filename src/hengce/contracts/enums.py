from enum import StrEnum


class QualityStatus(StrEnum):
    VALID = "VALID"
    DERIVED = "DERIVED"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    CONFLICT = "CONFLICT"
    STALE = "STALE"
    UNVERIFIED = "UNVERIFIED"
    REJECTED = "REJECTED"


class ReviewStatus(StrEnum):
    APPROVED = "APPROVED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REJECTED = "REJECTED"


class RunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
