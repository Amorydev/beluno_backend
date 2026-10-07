"""Read coordinates out of a Maps link without fetching anything.

Only Google Maps, Apple Maps, and OpenStreetMap links are understood, and only
the forms that carry coordinates in the URL itself. Anything else (short links
such as ``maps.app.goo.gl``, place IDs, search-only links) keeps the name the
person typed and stays ``pending``: the server never follows a link, so it
cannot be steered into fetching internal addresses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, unquote, urlsplit

GOOGLE_HOSTS = re.compile(r"^(www\.)?google\.[a-z.]{2,8}$|^maps\.google\.[a-z.]{2,8}$")
APPLE_HOSTS = frozenset({"maps.apple.com", "maps.apple"})
OSM_HOSTS = frozenset({"www.openstreetmap.org", "openstreetmap.org", "osm.org"})
# "@12.34,56.78" in a Google path, "12.34,56.78" in a query value.
AT_PAIR = re.compile(r"@(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)")
PAIR = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
# OSM "#map=zoom/lat/lon".
OSM_FRAGMENT = re.compile(r"map=\d{1,2}/(-?\d{1,2}(?:\.\d+)?)/(-?\d{1,3}(?:\.\d+)?)")
SIX_PLACES = Decimal("0.000001")


@dataclass(frozen=True)
class MapsLink:
    provider: str | None
    latitude: Decimal | None
    longitude: Decimal | None

    @property
    def parsed(self) -> bool:
        return self.latitude is not None


def read_maps_link(url: str) -> MapsLink:
    """The provider and coordinates a link states outright; nothing is guessed."""

    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return MapsLink(None, None, None)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return MapsLink(None, None, None)
    host = parts.hostname.lower()
    query = parse_qs(parts.query)
    if GOOGLE_HOSTS.match(host):
        found = AT_PAIR.search(unquote(parts.path)) or _first_pair(query, "q", "query", "ll")
        return _link("google", found)
    if host in APPLE_HOSTS:
        return _link("apple", _first_pair(query, "ll", "q", "sll"))
    if host in OSM_HOSTS:
        mlat, mlon = query.get("mlat", [None])[0], query.get("mlon", [None])[0]
        if mlat is not None and mlon is not None:
            return _link("osm", (mlat, mlon))
        return _link("osm", OSM_FRAGMENT.search(parts.fragment))
    return MapsLink(None, None, None)


def _first_pair(query: dict[str, list[str]], *names: str) -> re.Match[str] | None:
    for name in names:
        for value in query.get(name, []):
            match = PAIR.match(value)
            if match:
                return match
    return None


def _link(provider: str, found: re.Match[str] | tuple[str, str] | None) -> MapsLink:
    if found is None:
        return MapsLink(provider, None, None)
    raw = found.groups() if isinstance(found, re.Match) else found
    try:
        latitude, longitude = (Decimal(value).quantize(SIX_PLACES) for value in raw)
        in_range = -90 <= latitude <= 90 and -180 <= longitude <= 180
    except (InvalidOperation, ValueError):
        return MapsLink(provider, None, None)
    if not in_range:
        return MapsLink(provider, None, None)
    return MapsLink(provider, latitude, longitude)
