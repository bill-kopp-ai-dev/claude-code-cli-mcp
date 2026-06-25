from __future__ import annotations

from loguru import logger


def setup_logfire(*, token: str | None, service_name: str = "claude-code-cli-mcp") -> bool:
    """Configure logfire. Returns True if active, False if disabled/unavailable.
    Never raises.
    """
    if not token:
        return False

    try:
        import logfire
    except ImportError:
        logger.warning("logfire not installed; observability disabled")
        return False
    except Exception as e:
        logger.warning(f"failed to import logfire: {e}")
        return False

    try:
        logfire.configure(token=token, service_name=service_name)
        return True
    except Exception as e:
        logger.warning(f"failed to configure logfire: {e}")
        return False
