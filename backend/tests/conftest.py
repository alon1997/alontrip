import pytest

from app.agents import tools


@pytest.fixture(autouse=True)
def _fresh_draft_budget():
    """Each test starts with a clean draft throttle window."""
    tools._draft_log.clear()
    yield
    tools._draft_log.clear()
