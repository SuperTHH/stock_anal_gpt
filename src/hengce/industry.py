"""Canonical first-level industry labels used for cross-exchange percentiles."""

INDUSTRY_CLASSIFICATION_VERSION = "capco-industry-l1-aliases-v1"

_LEGACY_ALIASES = {
    "农林牧渔": "农、林、牧、渔业",
    "水电煤气": "电力、热力、燃气及水生产和供应业",
    "批发零售": "批发和零售业",
    "运输仓储": "交通运输、仓储和邮政业",
    "住宿餐饮": "住宿和餐饮业",
    "信息技术": "信息传输、软件和信息技术服务业",
    "房地产": "房地产业",
    "商务服务": "租赁和商务服务业",
    "科研服务": "科学研究和技术服务业",
    "公共环保": "水利、环境和公共设施管理业",
    "居民服务": "居民服务、修理和其他服务业",
    "教育文化": "教育",
    "文化传播": "文化、体育和娱乐业",
    "卫生": "卫生和社会工作",
}


def canonical_industry_l1(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip()
    return _LEGACY_ALIASES.get(normalized, normalized)
