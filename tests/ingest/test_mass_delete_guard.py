"""Unit tests for the mass-delete guard (pure function, no database)."""

from recall_assistant.ingest.loader import check_mass_delete


def test_119_of_7633_allowed_at_default_threshold():
    """Removing 119 of 7,633 recalls (1.56%) is under the 10% default."""
    assert check_mass_delete(119, 7633) is False


def test_800_of_7633_blocked_at_default_threshold():
    """Removing 800 of 7,633 recalls (10.5%) exceeds the 10% default."""
    assert check_mass_delete(800, 7633) is True


def test_exactly_at_threshold_allowed():
    """Removing exactly 10% is not blocked (only >10% is)."""
    assert check_mass_delete(100, 1000) is False


def test_zero_removals():
    assert check_mass_delete(0, 7633) is False


def test_empty_database():
    assert check_mass_delete(0, 0) is False
