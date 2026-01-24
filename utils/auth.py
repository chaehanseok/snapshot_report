import base64, json, hmac, hashlib, time, re
import streamlit as st


def b64url_decode(s: str) -> bytes:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode("utf-8"))


def verify_token(token: str) -> dict:
    secret = st.secrets.get("GATEWAY_SECRET", "")
    if not secret:
        raise ValueError("GATEWAY_SECRET not configured")

    try:
        payload_b64, sig_b64 = token.split(".", 1)
    except ValueError:
        raise ValueError("Invalid token format")

    payload_raw = b64url_decode(payload_b64)
    sig = b64url_decode(sig_b64)

    expected = hmac.new(
        secret.encode("utf-8"),
        payload_raw,
        hashlib.sha256,
    ).digest()

    if not hmac.compare_digest(sig, expected):
        raise ValueError("Invalid signature")

    payload = json.loads(payload_raw.decode("utf-8"))

    now = int(time.time())
    exp = int(payload.get("exp", 0))
    if now > exp:
        raise ValueError("Token expired")

    name = str(payload.get("name", "")).strip()
    phone = str(payload.get("phone", "")).strip()

    if not name or not phone:
        raise ValueError("Missing user info")

    return {
        "name": name,
        "phone": re.sub(r"\D", "", phone),
        "email": payload.get("email"),
        "org": payload.get("org", ""),
        "fc_code": payload.get("fc_code"),
        "role": payload.get("role", "fc"),  # 'fc' | 'admin'
        "id": payload.get("id"),
    }
