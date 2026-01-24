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

    payload = json.loads(payload_raw.decode())

    role = payload.get("role", "fc")

    name = payload.get("name")
    if not name:
        raise ValueError("Missing name")

    if role == "fc":
        phone = payload.get("phone")
        fc_code = payload.get("fc_code")
        if not phone or not fc_code:
            raise ValueError("Missing FC fields")
    else:  # admin
        phone = payload.get("phone")  # optional
        fc_code = None

    return {
        "name": name,
        "phone": phone,
        "fc_code": fc_code,
        "email": payload.get("email"),
        "org": payload.get("org"),
        "role": role,
        "id": payload.get("id"),
    }
