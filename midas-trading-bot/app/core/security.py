# app/core/security.py
from passlib.context import CryptContext
from cryptography.fernet import Fernet
import base64
from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Шифрование API ключей
# Ключ должен быть 32 байта, закодированные в base64
def get_fernet():
    key = settings.ENCRYPTION_KEY.encode()
    # Добиваем до 32 байт если нужно (упрощенно)
    if len(key) < 32:
        key = key.ljust(32, b'0')
    elif len(key) > 32:
        key = key[:32]
    
    fernet_key = base64.urlsafe_b64encode(key)
    return Fernet(fernet_key)

def encrypt_key(text: str) -> str:
    if not text:
        return ""
    f = get_fernet()
    return f.encrypt(text.encode()).decode()

def decrypt_key(token: str) -> str:
    if not token:
        return ""
    f = get_fernet()
    return f.decrypt(token.encode()).decode()

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)
