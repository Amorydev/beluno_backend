from __future__ import annotations

from decimal import Decimal

import pytest

from beluno.modules.planning.maps_links import read_maps_link


@pytest.mark.parametrize(
    ("url", "provider", "latitude", "longitude"),
    [
        (
            "https://www.google.com/maps/place/Ben+Thanh/@10.7725,106.6980,17z/data=!3m1",
            "google",
            "10.772500",
            "106.698000",
        ),
        ("https://maps.google.com/?q=21.0285,105.8542", "google", "21.028500", "105.854200"),
        (
            "https://www.google.co.jp/maps?query=35.6586,139.7454",
            "google",
            "35.658600",
            "139.745400",
        ),
        ("https://maps.apple.com/?ll=48.8584,2.2945&q=Eiffel", "apple", "48.858400", "2.294500"),
        (
            "https://www.openstreetmap.org/?mlat=-33.8568&mlon=151.2153#map=17/-33.8568/151.2153",
            "osm",
            "-33.856800",
            "151.215300",
        ),
        ("https://www.openstreetmap.org/#map=15/51.5007/-0.1246", "osm", "51.500700", "-0.124600"),
    ],
)
def test_coordinates_written_in_the_link_are_read(
    url: str, provider: str, latitude: str, longitude: str
) -> None:
    link = read_maps_link(url)
    assert (link.provider, link.latitude, link.longitude) == (
        provider,
        Decimal(latitude),
        Decimal(longitude),
    )
    assert link.parsed


@pytest.mark.parametrize(
    ("url", "provider"),
    [
        ("https://maps.app.goo.gl/AbCdEf123", None),  # a short link: never followed
        ("https://www.google.com/maps/place/Ben+Thanh+Market", "google"),
        ("https://maps.apple.com/?q=Eiffel+Tower", "apple"),
        ("https://www.google.com/maps?q=99.5,10", "google"),  # latitude out of range
        ("http://169.254.169.254/latest/meta-data", None),
        ("javascript:alert(1)", None),
        ("not a url", None),
    ],
)
def test_anything_else_keeps_the_typed_name(url: str, provider: str | None) -> None:
    link = read_maps_link(url)
    assert (link.provider, link.latitude, link.longitude, link.parsed) == (
        provider,
        None,
        None,
        False,
    )
