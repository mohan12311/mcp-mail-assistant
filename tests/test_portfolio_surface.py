"""Portfolio surface smoke tests.

These tests intentionally avoid real network accounts. They verify that the
sanitized public modules can be imported together without missing internal
dependencies.
"""

def test_core_runtime_modules_import() -> None:
    import core
    import db
    import daemon.main_loop
    import watcher.processor

    assert core is not None
    assert db is not None


def test_agent_and_mcp_modules_import() -> None:
    import mail_operator.main
    import mail_operator.handlers.approval
    import mail_operator.handlers.new_email
    import mail_operator.handlers.slack_conversation
    import mcp_server.server

    assert mail_operator.main is not None
    assert mcp_server.server is not None
