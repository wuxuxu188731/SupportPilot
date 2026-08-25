import pytest

from app.auth.passwords import (
    PasswordPolicyError,
    hash_password,
    verify_password,
)


def test_password_hash_is_salted_and_verifiable():
    first = hash_password("correct-horse-42")
    second = hash_password("correct-horse-42")

    assert first != second
    assert "correct-horse-42" not in first
    assert verify_password("correct-horse-42", first) is True
    assert verify_password("wrong-password", first) is False


@pytest.mark.parametrize("password", ["short", "密" * 40])
def test_password_rejects_invalid_utf8_byte_length(password):
    with pytest.raises(PasswordPolicyError):
        hash_password(password)