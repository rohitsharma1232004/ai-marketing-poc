import unittest
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from review_tokens import (
    ExpiredReviewToken,
    InvalidReviewToken,
    create_review_token,
    derive_csrf_token,
    generate_session_token,
    hash_session_token,
    hash_token,
    normalize_expires_at,
    verify_csrf_token,
    verify_review_token,
)


SECRET = "test-signing-secret-that-is-at-least-32-characters"
OTHER_SECRET = "different-signing-secret-that-is-also-long-enough"
REQUEST_ID = "79ec484f-ed63-4fb0-9aac-88c4496c85d6"
EXPIRY = 1_788_000_000


class ReviewTokenTests(unittest.TestCase):
    def test_token_is_deterministic_url_safe_and_has_only_minimal_claims(self):
        first = create_review_token(REQUEST_ID, EXPIRY, SECRET)
        second = create_review_token(REQUEST_ID, EXPIRY, SECRET)

        self.assertEqual(first, second)
        self.assertRegex(first, r"^[A-Za-z0-9_.-]+$")
        version, request_id, expiry, signature = first.split(".")
        self.assertEqual(version, "rv1")
        self.assertEqual(request_id, REQUEST_ID)
        self.assertEqual(expiry, str(EXPIRY))
        self.assertEqual(len(signature), 43)
        self.assertNotIn("campaign", first.lower())
        self.assertNotIn("client", first.lower())

    def test_verify_returns_typed_claims_and_iso_expiry(self):
        token = create_review_token(REQUEST_ID, EXPIRY, SECRET)
        claims = verify_review_token(token, SECRET, now=EXPIRY - 1)

        self.assertEqual(claims.version, "rv1")
        self.assertEqual(claims.review_request_id, REQUEST_ID)
        self.assertEqual(claims.expires_at, EXPIRY)
        self.assertEqual(claims.expires_at_utc.tzinfo, timezone.utc)
        self.assertTrue(claims.expires_at_iso.endswith("Z"))

    def test_store_iso_expiry_normalizes_and_reconstructs_same_token(self):
        stored_iso = "2026-08-20T12:34:56.987000Z"
        normalized = normalize_expires_at(stored_iso)
        aware = datetime.fromisoformat(stored_iso.replace("Z", "+00:00"))

        self.assertEqual(normalized, int(aware.timestamp()))
        self.assertEqual(
            create_review_token(REQUEST_ID, stored_iso, SECRET),
            create_review_token(REQUEST_ID, normalized, SECRET),
        )
        self.assertEqual(normalize_expires_at(aware), normalized)
        self.assertEqual(
            normalize_expires_at(aware.astimezone(timezone(timedelta(hours=5, minutes=30)))),
            normalized,
        )

    def test_tampered_uuid_expiry_and_signature_are_rejected(self):
        token = create_review_token(REQUEST_ID, EXPIRY, SECRET)
        version, request_id, expiry, signature = token.split(".")
        replacements = [
            f"{version}.{uuid4()}.{expiry}.{signature}",
            f"{version}.{request_id}.{EXPIRY + 1}.{signature}",
            f"{version}.{request_id}.{expiry}.{'A' + signature[1:]}",
        ]
        for tampered in replacements:
            with self.subTest(tampered=tampered[:20]):
                with self.assertRaises(InvalidReviewToken):
                    verify_review_token(tampered, SECRET, now=EXPIRY - 10)

    def test_wrong_secret_is_rejected(self):
        token = create_review_token(REQUEST_ID, EXPIRY, SECRET)
        with self.assertRaises(InvalidReviewToken):
            verify_review_token(token, OTHER_SECRET, now=EXPIRY - 1)

    def test_expiry_boundary_and_expired_error(self):
        token = create_review_token(REQUEST_ID, EXPIRY, SECRET)
        verify_review_token(token, SECRET, now=EXPIRY - 1)
        with self.assertRaises(ExpiredReviewToken):
            verify_review_token(token, SECRET, now=EXPIRY)
        with self.assertRaises(ExpiredReviewToken):
            verify_review_token(token, SECRET, now=EXPIRY + 100)

    def test_malformed_and_oversized_tokens_are_rejected_without_echo(self):
        valid = create_review_token(REQUEST_ID, EXPIRY, SECRET)
        signature = valid.rsplit(".", 1)[1]
        malformed = [
            "",
            "not-a-token",
            valid + ".extra",
            valid.replace("rv1", "rv2", 1),
            f"rv1.{REQUEST_ID.upper()}.{EXPIRY}.{signature}",
            f"rv1.00000000-0000-0000-0000-000000000000.{EXPIRY}.{signature}",
            f"rv1.{REQUEST_ID}.0{EXPIRY}.{signature}",
            f"rv1.{REQUEST_ID}.{EXPIRY}.{signature}=",
            "x" * 257,
            "rv1.é",
            None,
        ]
        for candidate in malformed:
            with self.subTest(candidate=repr(candidate)[:40]):
                with self.assertRaises(InvalidReviewToken) as raised:
                    verify_review_token(candidate, SECRET, now=EXPIRY - 1)
                if isinstance(candidate, str) and candidate:
                    self.assertNotIn(candidate, str(raised.exception))

    def test_invalid_uuid_expiry_and_short_secret_are_rejected_on_create(self):
        with self.assertRaises(ValueError):
            create_review_token("not-a-uuid", EXPIRY, SECRET)
        with self.assertRaises(ValueError):
            create_review_token(str(UUID(int=0)), EXPIRY, SECRET)
        with self.assertRaises(ValueError):
            create_review_token(REQUEST_ID, 0, SECRET)
        with self.assertRaises(ValueError):
            create_review_token(REQUEST_ID, EXPIRY, "short")
        with self.assertRaises(ValueError):
            normalize_expires_at("2026-08-20T12:00:00")
        with self.assertRaises(ValueError):
            normalize_expires_at("x" * 65)

    def test_review_token_hash_is_stable_fixed_length_and_distinct(self):
        token = create_review_token(REQUEST_ID, EXPIRY, SECRET)
        digest = hash_token(token)

        self.assertEqual(digest, hash_token(token))
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        other = create_review_token(str(uuid4()), EXPIRY, SECRET)
        self.assertNotEqual(digest, hash_token(other))
        with self.assertRaises(ValueError):
            hash_token("x" * 257)


class ReviewSessionAndCsrfTests(unittest.TestCase):
    def test_session_tokens_are_random_url_safe_and_hashable(self):
        first = generate_session_token()
        second = generate_session_token()

        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^sv1\.[A-Za-z0-9_-]{43}$")
        self.assertRegex(hash_session_token(first), r"^[0-9a-f]{64}$")
        self.assertEqual(hash_session_token(first), hash_session_token(first))
        self.assertNotEqual(hash_session_token(first), hash_session_token(second))

    def test_session_entropy_bounds_are_enforced(self):
        for invalid in (0, 31, 65, True, 32.0):
            with self.subTest(invalid=invalid):
                with self.assertRaises((TypeError, ValueError)):
                    generate_session_token(invalid)
        self.assertRegex(generate_session_token(64), r"^sv1\.[A-Za-z0-9_-]{86}$")
        with self.assertRaises(ValueError):
            hash_session_token("sv1.too-short")

    def test_csrf_is_deterministic_session_bound_and_constant_shape(self):
        session = generate_session_token()
        csrf = derive_csrf_token(session, SECRET)

        self.assertEqual(csrf, derive_csrf_token(session, SECRET))
        self.assertRegex(csrf, r"^cv1\.[A-Za-z0-9_-]{43}$")
        self.assertTrue(verify_csrf_token(csrf, session, SECRET))
        self.assertFalse(verify_csrf_token(csrf, generate_session_token(), SECRET))
        self.assertFalse(verify_csrf_token(csrf, session, OTHER_SECRET))

    def test_csrf_tamper_and_malformed_values_fail_closed(self):
        session = generate_session_token()
        csrf = derive_csrf_token(session, SECRET)
        tampered = csrf[:-1] + ("A" if csrf[-1] != "A" else "B")

        for candidate in (tampered, "", "cv2." + csrf[4:], csrf + "=", "x" * 65, None):
            with self.subTest(candidate=repr(candidate)):
                self.assertFalse(verify_csrf_token(candidate, session, SECRET))
        with self.assertRaises(ValueError):
            verify_csrf_token(csrf, "invalid-session", SECRET)
        with self.assertRaises(ValueError):
            derive_csrf_token(session, "short")


if __name__ == "__main__":
    unittest.main()
