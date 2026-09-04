"""Validation shared by password-changing entry points."""

import string


PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128


class PasswordPolicyError(ValueError):
    """The proposed password does not meet the public password policy."""


def validate_new_password(password: str) -> str:
    """Return ``password`` when it satisfies the reset/change policy."""

    if not isinstance(password, str):
        raise PasswordPolicyError("Password does not meet the password policy")
    if not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
        raise PasswordPolicyError("Password does not meet the password policy")
    if not any(character in string.ascii_letters for character in password):
        raise PasswordPolicyError("Password does not meet the password policy")
    if not any(character in string.digits for character in password):
        raise PasswordPolicyError("Password does not meet the password policy")
    if not any(character in string.punctuation for character in password):
        raise PasswordPolicyError("Password does not meet the password policy")
    return password
