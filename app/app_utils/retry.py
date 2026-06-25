import functools
import logging
import time
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar

from app.consts import (
    NETWORK_RETRY_BACKOFF,
    NETWORK_RETRY_DELAY,
    NETWORK_RETRY_TRIES,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")
P = ParamSpec("P")


def retry(
    tries: int = NETWORK_RETRY_TRIES,
    delay: float = NETWORK_RETRY_DELAY,
    backoff: float = NETWORK_RETRY_BACKOFF,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """A retry decorator with exponential backoff."""

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            attempt = 0
            current_delay = delay
            while attempt < tries:
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    attempt += 1
                    func_name = getattr(func, "__name__", str(func))
                    if attempt >= tries:
                        logger.error(
                            f"Function {func_name} failed after {tries} attempts: {e}"
                        )
                        raise
                    logger.warning(
                        f"Function {func_name} failed with {e}. Retrying in {current_delay:.2f}s (attempt {attempt}/{tries})..."
                    )
                    time.sleep(current_delay)
                    current_delay *= backoff
            raise RuntimeError("Unreachable")

        return wrapper

    return decorator


def retry_call(
    func: Callable[..., T],
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
    tries: int = NETWORK_RETRY_TRIES,
    delay: float = NETWORK_RETRY_DELAY,
    backoff: float = NETWORK_RETRY_BACKOFF,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> T:
    """Helper to execute a function with retry logic directly."""
    if kwargs is None:
        kwargs = {}
    attempt = 0
    current_delay = delay
    while attempt < tries:
        try:
            return func(*args, **kwargs)
        except exceptions as e:
            attempt += 1
            func_name = getattr(func, "__name__", str(func))
            if attempt >= tries:
                logger.error(
                    f"Function {func_name} failed after {tries} attempts: {e}"
                )
                raise
            logger.warning(
                f"Function {func_name} failed with {e}. Retrying in {current_delay:.2f}s (attempt {attempt}/{tries})..."
            )
            time.sleep(current_delay)
            current_delay *= backoff
    raise RuntimeError("Unreachable")
