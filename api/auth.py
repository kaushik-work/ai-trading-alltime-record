import os
import hashlib
import hmac
from datetime import datetime, timedelta
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt

SECRET_KEY = os.getenv("DASHBOARD_SECRET", "tradingbot-secret-change-in-prod")
ALGORITHM  = "HS256"
TOKEN_TTL  = 60 * 24  # 24 hours

DASHBOARD_USER = os.getenv("DASHBOARD_USER", "crazyheads")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASS", "crazyheadworks")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")


def verify_password(plain: str) -> bool:
    _assert_secure_auth_config()
    return hmac.compare_digest(
        hashlib.sha256(plain.encode()).hexdigest(),
        hashlib.sha256(DASHBOARD_PASS.encode()).hexdigest(),
    )


def create_token(username: str) -> str:
    _assert_secure_auth_config()
    expire = datetime.utcnow() + timedelta(minutes=TOKEN_TTL)
    return jwt.encode({"sub": username, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


def _assert_secure_auth_config() -> None:
    """Fail closed in live mode if dashboard auth still uses defaults."""
    try:
        import config
        live = not config.IS_PAPER
    except Exception:
        live = os.getenv("TRADING_MODE") == "live"
    if live and (
        SECRET_KEY == "tradingbot-secret-change-in-prod"
        or DASHBOARD_PASS == "crazyheadworks"
    ):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Set DASHBOARD_SECRET and DASHBOARD_PASS before live trading",
        )


def get_current_user(token: str = Depends(oauth2_scheme)) -> str:
    return decode_token(token)


def decode_token(token: str) -> str:
    _assert_secure_auth_config()
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user = payload.get("sub")
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        return user
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
