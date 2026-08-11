from hengce.contracts.enums import QualityStatus
from hengce.services.pilot_pipeline import _coverage_quality_status


def test_coverage_quality_status_distinguishes_missing_partial_and_complete() -> None:
    assert _coverage_quality_status(covered=0, target=30) is QualityStatus.MISSING
    assert _coverage_quality_status(covered=1, target=30) is QualityStatus.PARTIAL
    assert _coverage_quality_status(covered=29, target=30) is QualityStatus.PARTIAL
    assert _coverage_quality_status(covered=30, target=30) is QualityStatus.VALID
