"""
Encryption layer for all sensitive credentials and trade data.
Uses Fernet symmetric encryption (AES-128-CBC + HMAC).
Master key is derived from a passphrase using PBKDF2.
"""
import os
import base64
import hashlib
import logging
from pathlib import Path
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

logger = logging.getLogger(__name__)

KEY_FILE = Path(__file__).parent.parent / "db" / ".keyfile"
SALT_FILE = Path(__file__).parent.parent / "db" / ".salt"


def _generate_salt() -> bytes:
    salt = os.urandom(16)
    SALT_FILE.write_bytes(salt)
    return salt


def _load_or_create_salt() -> bytes:
    if SALT_FILE.exists():
        return SALT_FILE.read_bytes()
    return _generate_salt()


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=390000,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def get_or_create_fernet(passphrase: str = None) -> Fernet:
    """
    Returns a Fernet instance using:
    - A passphrase (from env BOT_PASSPHRASE or argument)
    - Or auto-generates and stores a key if no passphrase set
    """
    if passphrase is None:
        passphrase = os.getenv("BOT_PASSPHRASE", "")

    if passphrase:
        salt = _load_or_create_salt()
        key = _derive_key(passphrase, salt)
    else:
        # Auto-mode: generate/load a machine-local key
        if KEY_FILE.exists():
            key = KEY_FILE.read_bytes()
        else:
            key = Fernet.generate_key()
            KEY_FILE.write_bytes(key)
            KEY_FILE.chmod(0o600)
            logger.info("Encryption key generated and stored at %s", KEY_FILE)

    return Fernet(key)


_fernet: Fernet = None


def get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = get_or_create_fernet()
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a string. Returns base64-encoded ciphertext."""
    return get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a previously encrypted string."""
    return get_fernet().decrypt(ciphertext.encode()).decode()


def encrypt_dict(data: dict) -> dict:
    """Encrypt all string values in a dict (e.g., credentials)."""
    return {k: encrypt(v) if isinstance(v, str) and v else v for k, v in data.items()}


def decrypt_dict(data: dict) -> dict:
    """Decrypt all string values in a dict."""
    result = {}
    for k, v in data.items():
        if isinstance(v, str) and v:
            try:
                result[k] = decrypt(v)
            except Exception:
                result[k] = v  # not encrypted, return as-is
        else:
            result[k] = v
    return result


def secure_log(message: str) -> str:
    """Mask sensitive patterns in log messages."""
    import re
    patterns = [
        (r'sk-ant-[A-Za-z0-9_\-]+', 'sk-ant-***REDACTED***'),
        (r'(password["\s:=]+)[^\s"&,]+', r'\1***'),
        (r'(api[_-]?key["\s:=]+)[^\s"&,]+', r'\1***'),
        (r'(api[_-]?secret["\s:=]+)[^\s"&,]+', r'\1***'),
        (r'(access[_-]?token["\s:=]+)[^\s"&,]+', r'\1***'),
        (r'(totp[_-]?secret["\s:=]+)[^\s"&,]+', r'\1***'),
    ]
    for pattern, replacement in patterns:
        message = re.sub(pattern, replacement, message, flags=re.IGNORECASE)
    return message


class SecureFilter(logging.Filter):
    """Logging filter that masks credentials in all log output."""
    def filter(self, record):
        record.msg = secure_log(str(record.msg))
        return True


def setup_secure_logging():
    """Apply credential masking to the root logger."""
    root = logging.getLogger()
    root.addFilter(SecureFilter())
    logger.info("Secure logging filter applied.")
