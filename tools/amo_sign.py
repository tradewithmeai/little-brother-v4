"""Sign a Firefox extension via AMO unlisted channel.

Uses server time from AMO's Date header to avoid JWT clock-drift rejection.

Usage:
    python tools/amo_sign.py <zip_path> <jwt_issuer> <jwt_secret>

Example:
    python tools/amo_sign.py extension/web-ext-artifacts/little_brother-1.0.2.zip \
        user:17021741:355 <secret>
"""
import hashlib
import hmac
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import urlsafe_b64encode
from email.utils import parsedate_to_datetime
from pathlib import Path

AMO_BASE = "https://addons.mozilla.org/api/v5"
ADDON_ID = "little-brother@solvx.local"


# --- Minimal JWT (HS256, no external deps) ---

def _b64(data: bytes) -> str:
    return urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(issuer: str, secret: str, iat: int) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({"iss": issuer, "jti": str(iat), "iat": iat, "exp": iat + 300}).encode())
    msg = f"{header}.{payload}".encode()
    sig = _b64(hmac.new(secret.encode(), msg, hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


# --- AMO server time (avoids local clock skew) ---

def _server_iat() -> int:
    req = urllib.request.Request(f"{AMO_BASE}/accounts/profile/", headers={"User-Agent": "lb-signer/1"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as e:
        date_hdr = e.headers.get("Date", "")
    except Exception:
        return int(time.time()) - 5
    else:
        date_hdr = ""
    if date_hdr:
        try:
            return int(parsedate_to_datetime(date_hdr).timestamp()) - 2
        except Exception:
            pass
    return int(time.time()) - 5


# --- HTTP helpers ---

def _auth_header(issuer: str, secret: str) -> dict:
    token = make_jwt(issuer, secret, _server_iat())
    return {"Authorization": f"JWT {token}", "User-Agent": "lb-signer/1"}


def _post_multipart(url: str, field: str, filename: str, data: bytes, headers: dict, extra_fields: dict = None) -> dict:
    boundary = b"----LBSignBoundary"
    parts = b""
    for fname, fval in (extra_fields or {}).items():
        parts += (
            b"--" + boundary + b"\r\n"
            b'Content-Disposition: form-data; name="' + fname.encode() + b'"\r\n\r\n'
            + fval.encode() + b"\r\n"
        )
    parts += (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="' + field.encode() + b'"; filename="' + filename.encode() + b'"\r\n'
        b"Content-Type: application/zip\r\n\r\n"
        + data + b"\r\n"
        b"--" + boundary + b"--\r\n"
    )
    req_headers = {**headers, "Content-Type": f"multipart/form-data; boundary={boundary.decode()}"}
    req = urllib.request.Request(url, data=parts, headers=req_headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def _post_json(url: str, payload: dict, headers: dict) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _get_json(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _poll(url: str, issuer: str, secret: str, key: str, done_values: set, interval: int = 10, retries: int = 36):
    for attempt in range(retries):
        time.sleep(interval)
        try:
            data = _get_json(url, _auth_header(issuer, secret))
        except urllib.error.HTTPError as e:
            print(f"  Poll HTTP {e.code} — retrying")
            continue
        val = data.get(key)
        print(f"  [{attempt+1}] {key} = {val}")
        if val in done_values:
            return data
    raise RuntimeError(f"Timed out waiting for {key} to reach {done_values}")


# --- Main flow ---

def sign(zip_path: str, issuer: str, secret: str):
    zip_bytes = Path(zip_path).read_bytes()
    zip_name = Path(zip_path).name

    print(f"[1/4] Uploading {zip_name} ({len(zip_bytes)} bytes)…")
    try:
        upload = _post_multipart(
            f"{AMO_BASE}/addons/upload/",
            "upload", zip_name, zip_bytes,
            {**_auth_header(issuer, secret), "Accept": "application/json"},
            extra_fields={"channel": "unlisted"},
        )
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {e.read().decode()}")
        sys.exit(1)
    upload_uuid = upload["uuid"]
    print(f"  uuid = {upload_uuid}")

    print("[2/4] Waiting for validation…")
    upload_data = _poll(
        f"{AMO_BASE}/addons/upload/{upload_uuid}/",
        issuer, secret,
        key="processed", done_values={True},
    )
    if not upload_data.get("valid"):
        print("  Validation errors:", upload_data.get("validation"))
        sys.exit(1)
    print("  Validation passed")

    print("[3/4] Creating version…")
    version_payload = {"upload": upload_uuid, "channel": "unlisted"}
    try:
        version = _post_json(
            f"{AMO_BASE}/addons/addon/{ADDON_ID}/versions/",
            version_payload,
            {**_auth_header(issuer, secret), "Accept": "application/json"},
        )
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  HTTP {e.code}: {body}")
        sys.exit(1)

    version_id = version["id"]
    print(f"  version_id = {version_id}")

    print("[4/4] Waiting for signing…")
    ver_url = f"{AMO_BASE}/addons/addon/{ADDON_ID}/versions/{version_id}/"
    for attempt in range(24):
        time.sleep(15)
        try:
            ver_data = _get_json(ver_url, _auth_header(issuer, secret))
        except urllib.error.HTTPError as e:
            print(f"  Poll HTTP {e.code} — retrying")
            continue
        file_info = ver_data.get("file") or {}
        status = file_info.get("status", "unknown") if isinstance(file_info, dict) else "unknown"
        print(f"  [{attempt+1}] file.status = {status}")
        if status == "public":
            download_url = file_info.get("url")
            print(f"  Signed! Downloading from {download_url}")
            out_path = Path(zip_path).parent / (Path(zip_path).stem + "-signed.xpi")
            req = urllib.request.Request(download_url, headers=_auth_header(issuer, secret))
            with urllib.request.urlopen(req, timeout=60) as resp:
                xpi_bytes = resp.read()
            out_path.write_bytes(xpi_bytes)
            print(f"  Saved {len(xpi_bytes)} bytes to {out_path}")
            return
    print("  Timed out waiting for signing. Check AMO dashboard.")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python tools/amo_sign.py <zip> <issuer> <secret>")
        sys.exit(1)
    sign(sys.argv[1], sys.argv[2], sys.argv[3])
