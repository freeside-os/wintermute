from app.plugins.logging import LoggingPlugin

from app.agent import app


def test_app_plugins() -> None:
    # Ensure that LoggingPlugin is registered in the app's plugins
    logging_plugins = [p for p in app.plugins if isinstance(p, LoggingPlugin)]
    assert len(logging_plugins) == 1, "LoggingPlugin should be registered exactly once"
