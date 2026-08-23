import base64

def base64url_decode(data: str) -> bytes:
    """Safely decodes a base64url string."""
    padding = b'=' * (4 - (len(data) % 4))
    return base64.urlsafe_b64decode(data.encode('utf-8') + padding)