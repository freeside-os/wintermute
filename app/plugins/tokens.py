from typing import Optional
from typing_extensions import override
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_response import LlmResponse

class TokenTrackingPlugin(BasePlugin):
    def __init__(self):
        super().__init__("token_tracker")

    @override
    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> Optional[LlmResponse]:
        if llm_response.usage_metadata:
            ic = callback_context._invocation_context
            state = ic.session.state

            # Use dictionary keys to track totals
            state["total_input_tokens"] = state.get("total_input_tokens", 0) + llm_response.usage_metadata.prompt_token_count
            state["total_output_tokens"] = state.get("total_output_tokens", 0) + llm_response.usage_metadata.candidates_token_count

        return None
