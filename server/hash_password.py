"""Печатает строку ADMIN_PASSWORD_HASH для .env.

Запуск из корня проекта:  .venv/bin/python -m server.hash_password
"""
import getpass
import hashlib
import secrets


def make(password: str, iterations: int = 600_000) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations).hex()
    return f"pbkdf2:{iterations}:{salt}:{digest}"


if __name__ == "__main__":
    first = getpass.getpass("Пароль редактора: ")
    if len(first) < 8:
        raise SystemExit("Слишком короткий: нужно хотя бы 8 символов")
    if first != getpass.getpass("Ещё раз: "):
        raise SystemExit("Пароли не совпали")
    print("\nДобавь в .env строку:\n")
    print(f"ADMIN_PASSWORD_HASH={make(first)}")
