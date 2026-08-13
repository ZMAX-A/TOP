from __future__ import annotations

import unittest

from testops.api.security import (
    DUMMY_PASSWORD_HASH,
    hash_password,
    issue_session_token,
    session_token_hash,
    verify_password,
)


class SecurityPrimitiveTests(unittest.TestCase):
    def test_password_hash_is_salted_and_verifiable(self) -> None:
        first = hash_password("correct-horse-battery-staple")
        second = hash_password("correct-horse-battery-staple")

        self.assertNotEqual(first, second)
        self.assertTrue(verify_password("correct-horse-battery-staple", first))
        self.assertFalse(verify_password("wrong-password", first))
        self.assertFalse(verify_password("anything", "not-a-password-hash"))
        self.assertFalse(verify_password("wrong-password", DUMMY_PASSWORD_HASH))

    def test_session_tokens_are_opaque_and_only_the_digest_is_stable(self) -> None:
        first = issue_session_token()
        second = issue_session_token()

        self.assertNotEqual(first, second)
        self.assertEqual(session_token_hash(first), session_token_hash(first))
        self.assertNotEqual(session_token_hash(first), session_token_hash(second))
        self.assertNotIn(first, session_token_hash(first))


if __name__ == "__main__":
    unittest.main()
