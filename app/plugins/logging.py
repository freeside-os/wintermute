
from __future__ import annotations

from typing import Any
from typing import Optional
from typing import TYPE_CHECKING

from google.genai import types
from typing_extensions import override

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.events.event import Event
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.adk.plugins.base_plugin import BasePlugin

if TYPE_CHECKING:
  from google.adk.agents.invocation_context import InvocationContext


def create_logger():
  import logging
  logger = logging.getLogger("windetmute")
  logger.propagate = False  # Bypasses the parent hierarchy

  log_file = "/tmp/wintermute.log"

  # Create handler and format it on the fly
  handler = logging.FileHandler(log_file)
  handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
  logger.addHandler(handler)
  logger.setLevel(logging.INFO)
  return logger

class LoggingPlugin(BasePlugin):
  def __init__(self):
    super().__init__("wintermute dbg")
    self.logger = create_logger()
    self._log("---------- START ----------")

  @override
  async def on_user_message_callback(
      self,
      *,
      invocation_context: InvocationContext,
      user_message: types.Content,
  ) -> Optional[types.Content]:
    """Log user message and invocation start."""
    self._log(f"🚀 USER MESSAGE RECEIVED")
    self._log(f"   Session ID: {invocation_context.session.id}")
    self._log(
        "   Root Agent:"
        f" {invocation_context.agent.name if hasattr(invocation_context.agent, 'name') else 'Unknown'}"
    )
    self._log(f"   User Content: {self._format_content(user_message)}")
    if invocation_context.branch:
      self._log(f"   Branch: {invocation_context.branch}")
    return None

  @override
  async def before_run_callback(
      self, *, invocation_context: InvocationContext
  ) -> Optional[types.Content]:
    """Log invocation start."""
    self._log(f"🏃 INVOCATION STARTING")
    self._log(
        "   Starting Agent:"
        f" {invocation_context.agent.name if hasattr(invocation_context.agent, 'name') else 'Unknown'}"
    )
    return None

  @override
  async def on_event_callback(
      self, *, invocation_context: InvocationContext, event: Event
  ) -> Optional[Event]:
    """Log events yielded from the runner."""
    self._log(f"📢 EVENT YIELDED")
    self._log(f"   Author: {event.author}")
    self._log(f"   Content: {self._format_content(event.content)}")

    if event.get_function_calls():
      func_calls = [fc.name for fc in event.get_function_calls()]
      self._log(f"   Function Calls: {func_calls}")

    if event.get_function_responses():
      func_responses = [fr.name for fr in event.get_function_responses()]
      self._log(f"   Function Responses: {func_responses}")

    if event.long_running_tool_ids:
      self._log(f"   Long Running Tools: {list(event.long_running_tool_ids)}")

    return None

  @override
  async def after_run_callback(
      self, *, invocation_context: InvocationContext
  ) -> Optional[None]:
    """Log invocation completion."""
    self._log(f"✅ INVOCATION COMPLETED")
    return None

  @override
  async def before_agent_callback(
      self, *, agent: BaseAgent, callback_context: CallbackContext
  ) -> Optional[types.Content]:
    """Log agent execution start."""
    self._log(f"🤖 AGENT STARTING")
    self._log(f"   Agent Name: {callback_context.agent_name}")
    if callback_context._invocation_context.branch:
      self._log(f"   Branch: {callback_context._invocation_context.branch}")
    return None

  @override
  async def after_agent_callback(
      self, *, agent: BaseAgent, callback_context: CallbackContext
  ) -> Optional[types.Content]:
    """Log agent execution completion."""
    self._log(f"🤖 AGENT COMPLETED")
    self._log(f"   Agent Name: {callback_context.agent_name}")
    return None

  @override
  async def before_model_callback(
      self, *, callback_context: CallbackContext, llm_request: LlmRequest
  ) -> Optional[LlmResponse]:
    """Log LLM request before sending to model."""
    self._log(f"💭 LLM REQUEST")
    self._log(f"   Model: {llm_request.model or 'default'}")
    self._log(f"   Agent: {callback_context.agent_name}")

    # Log available tools
    if llm_request.tools_dict:
      tool_names = list(llm_request.tools_dict.keys())
      self._log(f"   Available Tools: {tool_names}")

    return None

  @override
  async def after_model_callback(
      self, *, callback_context: CallbackContext, llm_response: LlmResponse
  ) -> Optional[LlmResponse]:
    """Log LLM response after receiving from model."""
    self._log(f"💭 LLM RESPONSE")
    self._log(f"   Agent: {callback_context.agent_name}")

    if llm_response.error_code:
      self._log(f"   ❌ ERROR - Code: {llm_response.error_code}")
      self._log(f"   Error Message: {llm_response.error_message}")
    else:
      self._log(f"   Content: {self._format_content(llm_response.content)}")
      if llm_response.partial:
        self._log(f"   Partial: {llm_response.partial}")
      if llm_response.turn_complete is not None:
        self._log(f"   Turn Complete: {llm_response.turn_complete}")

    # Log usage metadata if available
    if llm_response.usage_metadata:
      self._log(
          "   Token Usage - Input:"
          f" {llm_response.usage_metadata.prompt_token_count}, Output:"
          f" {llm_response.usage_metadata.candidates_token_count}"
      )

    return None

  def _get_tool_prefix(self, tool: BaseTool) -> str:
    """Determine the log prefix for a tool based on its module."""
    func = getattr(tool, "func", None)
    if func and hasattr(func, "__module__"):
        module = func.__module__
    else:
        module = tool.__class__.__module__

    if module and module.startswith("app.tools"):
        if "app.tools.memory" in module:
            return "🧠 USING MEMORY TOOL"
        elif "app.tools.compilation" in module:
            return "🏗️ USING COMPILATION TOOL"
        elif "app.tools.dependency" in module:
            return "🔗 USING DEPENDENCY TOOL"
        elif "app.tools.feeds" in module:
            return "📡 USING FEEDS TOOL"
        elif "app.tools.package_io" in module:
            return "📦 USING PACKAGE IO TOOL"
        else:
            return "⚙️ USING INTERNAL TOOL"

    return "🔧 TOOL"

  @override
  async def before_tool_callback(
      self,
      *,
      tool: BaseTool,
      tool_args: dict[str, Any],
      tool_context: ToolContext,
  ) -> Optional[dict]:
    """Log tool execution start."""
    prefix = self._get_tool_prefix(tool)
    self._log(f"{prefix} STARTING")
    self._log(f"   Tool Name: {tool.name}")
    self._log(f"   Agent: {tool_context.agent_name}")
    self._log(f"   Arguments: {self._format_args(tool_args)}")
    return None

  @override
  async def after_tool_callback(
      self,
      *,
      tool: BaseTool,
      tool_args: dict[str, Any],
      tool_context: ToolContext,
      result: dict,
  ) -> Optional[dict]:
    """Log tool execution completion."""
    prefix = self._get_tool_prefix(tool)
    self._log(f"{prefix} COMPLETED")
    self._log(f"   Tool Name: {tool.name}")
    self._log(f"   Agent: {tool_context.agent_name}")
    self._log(f"   Result: {self._format_args(result)}")
    return None

  @override
  async def on_model_error_callback(
      self,
      *,
      callback_context: CallbackContext,
      llm_request: LlmRequest,
      error: Exception,
  ) -> Optional[LlmResponse]:
    """Log LLM error."""
    self._log(f"⛔ LLM ERROR")
    self._log(f"   Agent: {callback_context.agent_name}")
    self._log(f"   Error: {error}")

    return None

  @override
  async def on_tool_error_callback(
      self,
      *,
      tool: BaseTool,
      tool_args: dict[str, Any],
      tool_context: ToolContext,
      error: Exception,
  ) -> Optional[dict]:
    """Log tool error."""
    prefix = self._get_tool_prefix(tool)
    self._log(f"{prefix} ERROR")
    self._log(f"   Tool Name: {tool.name}")
    self._log(f"   Agent: {tool_context.agent_name}")
    self._log(f"   Arguments: {self._format_args(tool_args)}")
    self._log(f"   Error: {error}")
    return None

  def _log(self, message: str) -> None:
    self.logger.info(message)

  def _format_content(
      self, content: Optional[types.Content], max_length: int = 200
  ) -> str:
    """Format content for logging, truncating if too long."""
    if not content or not content.parts:
      return "None"

    parts = []
    for part in content.parts:
      if part.text:
        text = part.text.strip()
        if len(text) > max_length:
          text = text[:max_length] + "..."
        parts.append(f"text: '{text}'")
      elif part.function_call:
        parts.append(f"function_call: {part.function_call.name}")
      elif part.function_response:
        parts.append(f"function_response: {part.function_response.name}")
      elif part.code_execution_result:
        parts.append("code_execution_result")
      else:
        parts.append("other_part")

    return " | ".join(parts)

  def _format_args(self, args: dict[str, Any], max_length: int = 300) -> str:
    """Format arguments dictionary for logging."""
    if not args:
      return "{}"

    formatted = str(args)
    if len(formatted) > max_length:
      formatted = formatted[:max_length] + "...}"
    return formatted
