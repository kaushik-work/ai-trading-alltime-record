import os
import hashlib
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
    return hashlib.sha256(plain.encode()).hexdigest() == hashlib.sha256(DASHBOARD_PASS.encode()).hexdigest()


def create_token(username: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=TOKEN_TTL)
    return jwt.encode({"sub": username, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(token: str = Depends(oauth2_scheme)) -> str:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user = payload.get("sub")
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        return user
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
