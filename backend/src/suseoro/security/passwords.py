"""Argon2id password hashing with the application's minimum password policy."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from argon2.low_level import Type

MINIMUM_PASSWORD_LENGTH = 12

_PASSWORD_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


def hash_password(password: str) -> str:
    """Validate and hash a password using Argon2id."""
    if len(password) < MINIMUM_PASSWORD_LENGTH:
        raise ValueError("password must be at least 12 characters")
    return _PASSWORD_HASHER.hash(password)


def verify_password(password: str, encoded_hash: str) -> bool:
    """Return whether a password matches without exposing verification failures."""
    try:
        return _PASSWORD_HASHER.verify(encoded_hash, password)
    except (InvalidHashError, VerifyMismatchError):
        return False
