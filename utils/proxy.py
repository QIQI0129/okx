from urllib.parse import urlparse
from typing import Optional, Tuple

def parse_http_proxy(proxy_url: str) -> Optional[Tuple[str, int, Optional[tuple]]]:
    """Parse proxy URL to websocket-client parameters.

    Recommended for websocket-client:
      - http://host:port
      - http://user:pass@host:port

    Notes:
      - websocket-client does NOT support 'https' proxy_type, so if user passes https://,
        we will still treat it as an HTTP proxy (CONNECT) and return host/port/auth.
      - socks proxies are not handled here; if you need socks4/socks5, extend this helper.
    """
    if not proxy_url:
        return None

    u = urlparse(proxy_url.strip())
    if not u.scheme:
        return None

    scheme = u.scheme.lower()
    if scheme not in ("http", "https"):
        # websocket-client only supports proxy_type: http/socks4/socks5
        return None

    host = u.hostname
    if not host:
        return None

    # Keep explicit port; if missing, default by scheme (rare)
    port = u.port or (443 if scheme == "https" else 80)

    auth = None
    if u.username:
        auth = (u.username, u.password or "")

    return host, port, auth
