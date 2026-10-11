"""Smoke test — verifies the package is importable."""


def test_version_exists():
    import recall_assistant

    assert hasattr(recall_assistant, "__version__")
    assert isinstance(recall_assistant.__version__, str)
