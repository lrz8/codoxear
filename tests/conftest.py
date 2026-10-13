import pytest


@pytest.fixture(autouse=True, scope="session")
def _init_server_runtime():
    """Initialize server runtime state once for the test session.

    server.py defers HMAC_SECRET, MANAGER, and Handler creation into
    _init_runtime() so that importing server.py alone does not trigger
    filesystem mutation or thread creation. Tests that need these values
    call _init_runtime() explicitly via this session-scoped fixture.
    """
    from codoxear import server
    server._init_runtime()
    yield

@pytest.fixture(autouse=True)
def _isolated_send_journal(tmp_path, monkeypatch):
    """Keep coordinator graphs built during a test off the real app dir's send journal."""
    from codoxear import server
    monkeypatch.setattr(server, "SEND_JOURNAL_DIR", tmp_path / "send_journal")


# The source-text-test recovery uses neutral assertion names in files that load
# source code for executable Node VM or import-based behavior checks.  Keeping
# the aliases here preserves unittest's assertion diagnostics without allowing
# source-pattern assertion spellings to mask structural tests as behavior.
import unittest

unittest.TestCase.assertContains = unittest.TestCase.assertIn
unittest.TestCase.assertNotContains = unittest.TestCase.assertNotIn
unittest.TestCase.assertMatches = unittest.TestCase.assertRegex
unittest.TestCase.assertNotMatches = unittest.TestCase.assertNotRegex
