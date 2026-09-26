"""Audit payloads drop passwords, hashes, and raw tokens before insert."""

from __future__ import annotations

from govcon.audit import scrub


def test_scrub_removes_secrets_and_keeps_identity_fields() -> None:
    password = "correct-horse-battery"
    password_hash = "$argon2id$v=19$m=65536,t=3,p=4$salt$hash"
    raw_token = "session-token-value"
    cleaned = scrub(
        {
            "email": "owner@example.com",
            "role": "owner",
            "user_id": 7,
            "password": password,
            "Password": password,
            "password_hash": password_hash,
            "token": raw_token,
            "token_hash": "abc123",
            "session_token": raw_token,
            "api_key": "sk-live",
            "mfa_secret": "otp",
            "cookie": "sid=1",
            "authorization": "Bearer raw",
            "nested": [{"smtp_password": password, "id": 3}],
        }
    )
    rendered = str(cleaned)
    assert cleaned == {
        "email": "owner@example.com",
        "role": "owner",
        "user_id": 7,
        "nested": [{"id": 3}],
    }
    assert "password" not in cleaned
    assert "password_hash" not in cleaned
    assert password not in rendered
    assert password_hash not in rendered
    assert raw_token not in rendered
    assert "sk-live" not in rendered
