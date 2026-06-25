from google.adk.agents.invocation_context import InvocationContext

_orig_model_copy = InvocationContext.model_copy

def _patched_model_copy(self, *, update=None, deep=False):
    copied = _orig_model_copy(self, update=update, deep=deep)
    copied._event_queue = self._event_queue
    copied._invocation_cost_manager = self._invocation_cost_manager
    return copied

InvocationContext.model_copy = _patched_model_copy

from .agent import app  # noqa: E402

__all__ = ["app"]
