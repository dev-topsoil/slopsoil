"""Tests for cogs/tv.py — timezone formatting helpers."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from cogs.tv import _fmt_time, _get_display_tz


# ---------------------------------------------------------------------------
# _get_display_tz
# ---------------------------------------------------------------------------


def test_get_display_tz_valid_timezone(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "America/New_York")
    tz = _get_display_tz()
    assert str(tz) == "America/New_York"


def test_get_display_tz_unset_falls_back_to_local(monkeypatch):
    monkeypatch.delenv("TIMEZONE", raising=False)
    tz = _get_display_tz()
    # Should be a valid tzinfo — exactly what the local clock would give
    assert tz is not None
    assert hasattr(tz, "utcoffset")


def test_get_display_tz_empty_string_falls_back_to_local(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "")
    tz = _get_display_tz()
    assert tz is not None
    assert hasattr(tz, "utcoffset")


def test_get_display_tz_invalid_name_falls_back_to_local(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "Not/AReal_Zone")
    tz = _get_display_tz()
    # Falls back — should still return something usable
    assert tz is not None
    assert hasattr(tz, "utcoffset")


def test_get_display_tz_whitespace_only_falls_back(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "   ")
    tz = _get_display_tz()
    assert tz is not None


# ---------------------------------------------------------------------------
# _fmt_time
# ---------------------------------------------------------------------------


def test_fmt_time_known_utc_timestamp(monkeypatch):
    """2000-01-01 00:00 UTC → midnight in UTC timezone."""
    monkeypatch.setenv("TIMEZONE", "UTC")
    ts = datetime(2000, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp()
    result = _fmt_time(ts)
    assert result == "12:00 AM"


def test_fmt_time_noon_utc(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "UTC")
    ts = datetime(2000, 6, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    assert _fmt_time(ts) == "12:00 PM"


def test_fmt_time_leading_zero_stripped(monkeypatch):
    """Times like '07:30 PM' should display as '7:30 PM'."""
    monkeypatch.setenv("TIMEZONE", "UTC")
    ts = datetime(2000, 1, 1, 19, 30, 0, tzinfo=timezone.utc).timestamp()
    assert _fmt_time(ts) == "7:30 PM"


def test_fmt_time_respects_timezone_offset(monkeypatch):
    """Same UTC instant reads differently in two different timezones."""
    # 2000-01-01 20:00 UTC = 3:00 PM in America/Chicago (UTC-6 in winter)
    ts = datetime(2000, 1, 1, 21, 0, 0, tzinfo=timezone.utc).timestamp()

    monkeypatch.setenv("TIMEZONE", "UTC")
    utc_result = _fmt_time(ts)

    monkeypatch.setenv("TIMEZONE", "America/New_York")
    ny_result = _fmt_time(ts)

    assert utc_result != ny_result
