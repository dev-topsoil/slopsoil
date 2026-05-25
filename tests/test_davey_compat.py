"""Tests for pure data classes and logic in davey_compat.py."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# ProposalsOperationType constants
# ---------------------------------------------------------------------------


def test_proposals_operation_type_values():
    from davey_compat import ProposalsOperationType

    assert ProposalsOperationType.append == 0
    assert ProposalsOperationType.revoke == 1


# ---------------------------------------------------------------------------
# SessionStatus constants
# ---------------------------------------------------------------------------


def test_session_status_values():
    from davey_compat import SessionStatus

    assert SessionStatus.inactive == 0
    assert SessionStatus.pending == 1
    assert SessionStatus.active == 3


# ---------------------------------------------------------------------------
# CommitWelcome
# ---------------------------------------------------------------------------


def test_commit_welcome_stores_commit():
    from davey_compat import CommitWelcome

    data = b"\x01\x02\x03"
    cw = CommitWelcome(data)
    assert cw.commit == data


def test_commit_welcome_welcome_is_none():
    from davey_compat import CommitWelcome

    cw = CommitWelcome(b"anything")
    assert cw.welcome is None


def test_commit_welcome_empty_bytes():
    from davey_compat import CommitWelcome

    cw = CommitWelcome(b"")
    assert cw.commit == b""


# ---------------------------------------------------------------------------
# _EncryptionStats
# ---------------------------------------------------------------------------


def test_encryption_stats_reads_attributes():
    from davey_compat import _EncryptionStats

    raw = MagicMock()
    raw.encrypt_attempts = 10
    raw.encrypt_success_count = 8
    raw.encrypt_failure_count = 2

    stats = _EncryptionStats(raw)
    assert stats.attempts == 10
    assert stats.successes == 8
    assert stats.failures == 2


def test_encryption_stats_missing_attributes_default_zero():
    from davey_compat import _EncryptionStats

    raw = object()  # no encrypt_* attributes
    stats = _EncryptionStats(raw)
    assert stats.attempts == 0
    assert stats.successes == 0
    assert stats.failures == 0


# ---------------------------------------------------------------------------
# process_proposals optype byte prepending logic
# (tests the byte-reassembly without calling into libdave)
# ---------------------------------------------------------------------------


def test_process_proposals_prepends_append_byte():
    """append optype → byte 0x00 prepended before the proposals payload."""
    from davey_compat import DaveSession, ProposalsOperationType

    with patch("davey_compat.dave") as mock_dave:
        mock_session = MagicMock()
        mock_session.process_proposals.return_value = None
        mock_dave.Session.return_value = mock_session
        mock_dave.Encryptor.return_value = MagicMock()
        mock_dave.Codec = MagicMock()

        ds = DaveSession(1, 10, 20)
        proposals_payload = b"\xAB\xCD"
        ds.process_proposals(ProposalsOperationType.append, proposals_payload)

        called_data = mock_session.process_proposals.call_args[0][0]
        assert called_data[0] == 0  # append byte
        assert called_data[1:] == proposals_payload


def test_process_proposals_prepends_revoke_byte():
    """revoke optype → byte 0x01 prepended."""
    from davey_compat import DaveSession, ProposalsOperationType

    with patch("davey_compat.dave") as mock_dave:
        mock_session = MagicMock()
        mock_session.process_proposals.return_value = None
        mock_dave.Session.return_value = mock_session
        mock_dave.Encryptor.return_value = MagicMock()
        mock_dave.Codec = MagicMock()

        ds = DaveSession(1, 10, 20)
        proposals_payload = b"\x01\x02"
        ds.process_proposals(ProposalsOperationType.revoke, proposals_payload)

        called_data = mock_session.process_proposals.call_args[0][0]
        assert called_data[0] == 1  # revoke byte
        assert called_data[1:] == proposals_payload


def test_process_proposals_returns_commit_welcome_when_result():
    from davey_compat import CommitWelcome, DaveSession, ProposalsOperationType

    with patch("davey_compat.dave") as mock_dave:
        mock_session = MagicMock()
        mock_session.process_proposals.return_value = b"\xDE\xAD"
        mock_dave.Session.return_value = mock_session
        mock_dave.Encryptor.return_value = MagicMock()
        mock_dave.Codec = MagicMock()

        ds = DaveSession(1, 10, 20)
        result = ds.process_proposals(ProposalsOperationType.append, b"\x00")
        assert isinstance(result, CommitWelcome)
        assert result.commit == b"\xDE\xAD"


def test_process_proposals_returns_none_when_no_result():
    from davey_compat import DaveSession, ProposalsOperationType

    with patch("davey_compat.dave") as mock_dave:
        mock_session = MagicMock()
        mock_session.process_proposals.return_value = None
        mock_dave.Session.return_value = mock_session
        mock_dave.Encryptor.return_value = MagicMock()
        mock_dave.Codec = MagicMock()

        ds = DaveSession(1, 10, 20)
        result = ds.process_proposals(ProposalsOperationType.append, b"\x00")
        assert result is None


def test_process_proposals_exception_returns_none():
    from davey_compat import DaveSession, ProposalsOperationType

    with patch("davey_compat.dave") as mock_dave:
        mock_session = MagicMock()
        mock_session.process_proposals.side_effect = RuntimeError("boom")
        mock_dave.Session.return_value = mock_session
        mock_dave.Encryptor.return_value = MagicMock()
        mock_dave.Codec = MagicMock()

        ds = DaveSession(1, 10, 20)
        result = ds.process_proposals(ProposalsOperationType.append, b"\x00")
        assert result is None


# ---------------------------------------------------------------------------
# DaveSession.encrypt_opus passthrough when result is None
# ---------------------------------------------------------------------------


def test_encrypt_opus_passthrough_when_no_key():
    from davey_compat import DaveSession

    with patch("davey_compat.dave") as mock_dave:
        mock_encryptor = MagicMock()
        mock_encryptor.encrypt.return_value = None  # no key yet → passthrough
        mock_dave.Session.return_value = MagicMock()
        mock_dave.Encryptor.return_value = mock_encryptor
        mock_dave.Codec = MagicMock()
        mock_dave.MediaType = MagicMock()

        ds = DaveSession(1, 10, 20)
        raw = b"\xAA\xBB"
        assert ds.encrypt_opus(raw) is raw


def test_encrypt_opus_returns_encrypted_when_key_ready():
    from davey_compat import DaveSession

    with patch("davey_compat.dave") as mock_dave:
        encrypted = b"\xFF\xFE"
        mock_encryptor = MagicMock()
        mock_encryptor.encrypt.return_value = encrypted
        mock_dave.Session.return_value = MagicMock()
        mock_dave.Encryptor.return_value = mock_encryptor
        mock_dave.Codec = MagicMock()
        mock_dave.MediaType = MagicMock()

        ds = DaveSession(1, 10, 20)
        assert ds.encrypt_opus(b"\x01") == encrypted


# ---------------------------------------------------------------------------
# DaveSession repr
# ---------------------------------------------------------------------------


def test_dave_session_repr():
    from davey_compat import DaveSession

    with patch("davey_compat.dave") as mock_dave:
        mock_dave.Session.return_value = MagicMock()
        mock_dave.Encryptor.return_value = MagicMock()
        mock_dave.Codec = MagicMock()

        ds = DaveSession(1, 10, 20)
        r = repr(ds)
        assert "DaveSession" in r
        assert "epoch=None" in r
        assert "ready=False" in r
