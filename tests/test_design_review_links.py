from senior_review_links import (
    build_design_review_url,
    build_review_url,
    generate_review_token,
    hash_design_review_token,
    hash_review_token,
)


def test_design_review_hash_uses_separate_security_domain():
    token = generate_review_token()
    assert len(hash_design_review_token(token)) == 64
    assert hash_design_review_token(token) != hash_review_token(token)


def test_design_review_url_uses_separate_query_parameter():
    token = generate_review_token()
    url = build_design_review_url("https://marketing.example.com", token)
    assert "?design_review=" in url
    assert token in url
    assert "?review=" not in url


def test_content_review_url_contract_stays_unchanged():
    token = generate_review_token()
    url = build_review_url("https://marketing.example.com", token)
    assert "?review=" in url
