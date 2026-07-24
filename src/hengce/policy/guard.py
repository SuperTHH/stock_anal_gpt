import time
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import uuid4

from hengce.contracts.enums import ReviewStatus
from hengce.contracts.policy import SourcePolicy
from hengce.contracts.run import RefusalRecord
from hengce.state.repository import StateRepository


class PolicyDenied(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class PolicyGuard:
    def __init__(
        self,
        repository: StateRepository,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.repository = repository
        self.clock = clock
        self.sleeper = sleeper

    def authorize(self, source_id: str, url: str, purpose: str, module: str) -> None:
        policy = self.repository.get_policy(source_id)
        reason = self._deny_reason(policy, url, purpose)
        if reason is not None:
            parsed = urlparse(url)
            self.repository.record_refusal(
                RefusalRecord(
                    refusal_id=str(uuid4()),
                    requested_url=url,
                    resolved_domain=(parsed.hostname or "").lower(),
                    requested_purpose=purpose,
                    policy_rule=source_id,
                    refused_at=datetime.now(UTC),
                    reason_code=reason,
                    requesting_module=module,
                )
            )
            raise PolicyDenied(reason)
        assert policy is not None
        delay = self.repository.reserve_rate_slot(
            source_id,
            requested_at=self.clock(),
            rate_limit_per_minute=policy.rate_limit_per_minute,
        )
        if delay:
            self.sleeper(delay)

    @staticmethod
    def _deny_reason(
        policy: SourcePolicy | None,
        url: str,
        purpose: str,
    ) -> str | None:
        if policy is None:
            return "SOURCE_POLICY_MISSING"
        if policy.review_status is not ReviewStatus.APPROVED:
            return "SOURCE_REVIEW_REQUIRED"
        if not policy.enabled:
            return "SOURCE_DISABLED"

        parsed = urlparse(url)
        if parsed.scheme == "http" and not (
            policy.source_id == "tushare"
            and (parsed.hostname or "").lower() == "api.tushare.pro"
        ):
            return "HTTP_ENDPOINT_NOT_ALLOWED"
        if parsed.scheme not in policy.allowed_schemes or not parsed.hostname:
            return "SCHEME_NOT_ALLOWED"

        hostname = parsed.hostname.lower()
        if not any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in policy.allowed_domains
        ):
            return "DOMAIN_NOT_ALLOWED"
        if purpose not in policy.allowed_purposes:
            return "PURPOSE_NOT_ALLOWED"
        return None
