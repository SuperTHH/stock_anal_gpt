from hengce.industry import canonical_industry_l1


def test_legacy_exchange_industry_labels_share_one_canonical_scope() -> None:
    assert canonical_industry_l1("信息技术") == "信息传输、软件和信息技术服务业"
    assert canonical_industry_l1("运输仓储") == "交通运输、仓储和邮政业"
    assert canonical_industry_l1("批发零售") == "批发和零售业"


def test_canonical_industry_label_is_stable() -> None:
    label = "信息传输、软件和信息技术服务业"
    assert canonical_industry_l1(label) == label
