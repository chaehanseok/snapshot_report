import base64
import json
import hmac
import hashlib
import time

# 🔑 Streamlit Cloud Secrets에 넣은 값과 반드시 동일해야 함
SECRET = "5f9f2e5e69605a9492613df1ed074672bf49f44621116cc1189a3816e98be9fc"

def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")

def make_token(name: str, phone: str, email: str = "", ttl_sec: int = 1800) -> str:
    payload = {
        "name": name,
        "phone": phone,
        "email": email,
        "exp": int(time.time()) + ttl_sec,
    }

    payload_raw = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":")
    ).encode("utf-8")

    sig = hmac.new(
        SECRET.encode("utf-8"),
        payload_raw,
        hashlib.sha256
    ).digest()

    return f"{b64url_encode(payload_raw)}.{b64url_encode(sig)}"

if __name__ == "__main__":
    token = make_token(
        name="채한석",
        phone="010-1234-5678",
        email="chae.hanseok@miraeasset.com"
    )
    print(token)
