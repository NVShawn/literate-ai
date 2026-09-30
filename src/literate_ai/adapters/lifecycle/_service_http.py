"""HTTP probes bound to a verifier-selected loopback service origin."""

import ipaddress
import urllib.error
import urllib.parse
import urllib.request


def _origin(url: str) -> tuple[str, str, int]:
    try:
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.hostname is None
            or not ipaddress.ip_address(parsed.hostname).is_loopback
            or parsed.port == 0
        ):
            raise ValueError("not a loopback HTTP origin")
        return parsed.scheme, parsed.hostname, parsed.port or 80
    except ValueError as exc:
        raise urllib.error.URLError(
            "acceptance HTTP probe requires a literal loopback origin"
        ) from exc


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        try:
            if _origin(new_url) != _origin(request.full_url):
                raise urllib.error.URLError(
                    "acceptance HTTP redirect escaped the selected loopback origin"
                )
        except urllib.error.URLError:
            response.close()
            raise
        return super().redirect_request(
            request, response, code, message, headers, new_url
        )


def open_service_request(request: urllib.request.Request | str, *, timeout: float):
    _origin(request if isinstance(request, str) else request.full_url)
    # A private opener avoids both ambient proxies and a process-global opener
    # installed by an unrelated caller. Redirects cannot substitute another server.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _SameOriginRedirect()
    )
    return opener.open(request, timeout=timeout)
