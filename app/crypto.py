from itsdangerous import URLSafeSerializer

from app.config import get_settings


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().secret_key, salt="crm-mail")


def encrypt_str(value: str) -> str:
    if value is None:
        return ""
    return _serializer().dumps({"v": value})


def decrypt_str(value: str) -> str:
    if not value:
        return ""
    data = _serializer().loads(value)
    return str(data.get("v") or "")
