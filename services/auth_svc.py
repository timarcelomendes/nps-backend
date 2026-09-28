# backend/services/auth.py
import os
from datetime import datetime, timedelta, timezone
from jose import jwt, JWTError, ExpiredSignatureError
from passlib.context import CryptContext
from fastapi.security import OAuth2PasswordBearer
from fastapi import Depends, HTTPException, status

# 🎯 CONFIGURAÇÕES CENTRALIZADAS (Independente do main.py)
SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("ERRO CRÍTICO: JWT_SECRET_KEY não configurada.")

ALGORITHM = "HS256"
# Aumentado para 8 horas para evitar deslogar durante o uso do Chat
ACCESS_TOKEN_EXPIRE_MINUTES = 480 

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
# 🎯 Defina o esquema aqui para que o auth.py não precise do main.py
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/login")

import bcrypt as _bcrypt

def hash_password(password: str):
    # bcrypt direto (o passlib é incompatível com bcrypt >= 4.1)
    return _bcrypt.hashpw(password.encode("utf-8")[:72], _bcrypt.gensalt()).decode("utf-8")

def verify_password(plain_password, hashed_password):
    try:
        return _bcrypt.checkpw(plain_password.encode("utf-8")[:72], hashed_password.encode("utf-8"))
    except Exception:
        return False

def create_access_token(data: dict, expires_delta: timedelta = None):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire.timestamp()}) # 🎯 Timestamp para compatibilidade JWT
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user_token_data(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("sub") is None:
            raise HTTPException(status_code=401, detail="Token inválido.")
        return payload
    except (ExpiredSignatureError, JWTError):
        raise HTTPException(status_code=401, detail="Sessão expirada.")

async def get_current_user(token_data: dict = Depends(get_current_user_token_data)):
    return token_data.get("sub")

def exigir_admin(token_data: dict = Depends(get_current_user_token_data)):
    """Protege a rota exigindo o cargo de Admin"""
    if token_data.get("tipo") != "Admin":
        raise HTTPException(status_code=403, detail="Acesso negado. Apenas Administradores.")
    return token_data.get("sub")

def exigir_manager(token_data: dict = Depends(get_current_user_token_data)):
    """Protege a rota exigindo o cargo de Manager ou Admin"""
    if token_data.get("tipo") not in ["Admin", "Manager"]:
        raise HTTPException(status_code=403, detail="Acesso negado. Requer nível Manager ou superior.")
    return token_data.get("sub")
