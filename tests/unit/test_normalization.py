import pytest

from app.core.normalization import normalize_broadcaster_id_or_login


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", ""),
        ("12345", "12345"),
        ("@streamer", "streamer"),
        ("https://www.twitch.tv/streamer", "streamer"),
        ("https://twitch.tv/streamer/videos", "streamer"),
        ("https://twitch.tv/streamer?foo=bar", "streamer"),
        ("streamer/videos", "streamer"),
    ],
)
def test_normalize_broadcaster_id_or_login(raw, expected):
    assert normalize_broadcaster_id_or_login(raw) == expected
