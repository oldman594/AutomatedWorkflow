import pytest


@pytest.fixture(autouse=True)
def trusted_test_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOFLOW_SANDBOX_MODE", "host")
