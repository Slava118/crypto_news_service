import json
import re
import urllib.error
import urllib.request


class ServiceError(Exception):
    """An operation failed with a message safe to include in logs."""


class HTTPStatusError(ServiceError):
    def __init__(self, service, code, status):
        super().__init__(f"{service} returned HTTP {code}{status}")
        self.code = code


def fetch(url, *, service="HTTP", payload=None, headers=None):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"User-Agent": "EssentialCryptoNewsBot/1.0", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            data = response.read(8_000_001)
    except urllib.error.HTTPError as exc:
        # Read only the vendor status code, never a URL or body that could contain a key.
        status = ""
        try:
            code = json.loads(exc.read(32768)).get("error", {}).get("status", "")
            if isinstance(code, str) and re.fullmatch(r"[A-Z_]{1,40}", code):
                status = f" ({code})"
        except (AttributeError, OSError, ValueError):
            pass
        raise HTTPStatusError(service, exc.code, status) from None
    except (OSError, ValueError):
        raise ServiceError(f"{service} connection failed") from None
    if len(data) > 8_000_000:
        raise ServiceError(f"{service} response exceeded size limit")
    return data


def request_json(url, *, service, payload, headers=None):
    try:
        return json.loads(fetch(
            url, service=service, payload=payload,
            headers={"Content-Type": "application/json", **(headers or {})},
        ))
    except ValueError:
        raise ServiceError(f"{service} returned invalid JSON") from None
