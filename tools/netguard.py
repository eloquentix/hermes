import asyncio
import ipaddress

import httpx


class BlockedAddress(Exception):
    pass


async def _require_public(request: httpx.Request) -> None:
    """Refuse requests (including each redirect hop) that resolve to a LAN,
    loopback, link-local or Tailscale address. Fetch URLs come from the model
    and from web pages, so a prompt injection could otherwise point the bot
    at the router, Syncthing, or other services on the Pi's network."""
    host = request.url.host
    infos = await asyncio.get_running_loop().getaddrinfo(host, request.url.port or 443)
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise BlockedAddress(f"Blocked non-public address for {host}")


def public_client(**kwargs) -> httpx.AsyncClient:
    """httpx.AsyncClient that only talks to public internet hosts."""
    return httpx.AsyncClient(event_hooks={"request": [_require_public]}, **kwargs)
