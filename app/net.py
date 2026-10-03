"""
Client-IP resolution with proxy trust (OWASP A04 / A09).

`X-Forwarded-For` is attacker-controlled: a client can send any value it likes.
Trusting it blindly lets a caller spoof its source IP to slip past the per-IP
rate limit and to poison the audit trail. So we only read XFF when the request
actually arrived from a proxy we trust (nginx on the box, the vFirewall / LB) —
otherwise the socket peer is the client.

`settings.TRUSTED_PROXIES` is a comma-separated list of CIDRs or bare IPs.
"""
import ipaddress
import logging

from fastapi import Request

from app.config import settings

logger = logging.getLogger(__name__)


def _parse_networks(spec: str) -> list[ipaddress._BaseNetwork]:
    nets: list[ipaddress._BaseNetwork] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            nets.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            logger.warning("TRUSTED_PROXIES: ignoring unparseable entry %r", token)
    return nets


_TRUSTED: list[ipaddress._BaseNetwork] = _parse_networks(settings.TRUSTED_PROXIES)


def _is_trusted(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _TRUSTED)


def client_ip(request: Request) -> str:
    """The best available client IP. Reads the first X-Forwarded-For hop only
    when the direct peer is a trusted proxy; otherwise the socket peer."""
    peer = request.client.host if request.client else ""
    if peer and _is_trusted(peer):
        xff = request.headers.get("x-forwarded-for", "")
        first = xff.split(",")[0].strip()
        if first:
            return first
    return peer or "unknown"
