"""Per-client curl routing; never mutate the process-wide proxy environment."""
from urllib.parse import urlsplit


def validate_proxy_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        raise ValueError("proxy URL is invalid") from None
    if (not value or any(ord(c) < 32 for c in value)
            or parsed.scheme not in {"http", "https", "socks5", "socks5h"}
            or not parsed.hostname or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("proxy URL must use http, https, socks5 or socks5h and a valid host")
    return value


def curl_route_config(*, proxy_url: str | None = None, bypass_proxy: bool = False) -> list[str]:
    if proxy_url is not None and bypass_proxy:
        raise ValueError("explicit proxy and bypass_proxy are mutually exclusive")
    if bypass_proxy:
        return ['proxy = ""', 'noproxy = "*"']
    if proxy_url is not None:
        value = validate_proxy_url(proxy_url).replace("\\", "\\\\").replace('"', '\\"')
        # Explicit provider routing overrides ambient NO_PROXY for this client.
        return [f'proxy = "{value}"', 'noproxy = ""']
    return []


def curl_route_args(*, proxy_url: str | None = None, bypass_proxy: bool = False) -> list[str]:
    # -q must be curl's first option. User curlrc cannot override explicit routes.
    return ["--disable"] if proxy_url is not None or bypass_proxy else []
