from unittest.mock import patch

from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agent import root_agent


async def mock_generate_content_async(*args, **kwargs):
    yield LlmResponse(
        content=types.Content(
            role="model",
            parts=[
                types.Part.from_text(
                    text='{"pkg_name": null, "action": "out_of_scope", "version": null, "group": null, "is_security_update": false}'
                )
            ],
        )
    )


@patch("google.adk.models.Gemini.generate_content_async", side_effect=mock_generate_content_async)
def test_agent_stream(mock_gen) -> None:
    """
    Integration test for the agent stream functionality.
    Tests that the agent returns valid streaming responses.
    """

    session_service = InMemorySessionService()

    session = session_service.create_session_sync(user_id="test_user", app_name="test")
    runner = Runner(agent=root_agent, session_service=session_service, app_name="test")

    message = types.Content(
        role="user", parts=[types.Part.from_text(text="Why is the sky blue?")]
    )

    events = list(
        runner.run(
            new_message=message,
            user_id="test_user",
            session_id=session.id,
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        )
    )
    assert len(events) > 0, "Expected at least one message"

    has_expected_text = False
    expected_text = (
        "I am the Wintermute packaging agent, designed to create, import, fix, "
        "upgrade, review, or audit packages for Freeside OS. This request "
        "seems out of scope. Please ask a package management or OS maintenance query."
    )
    for event in events:
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text and expected_text in part.text:
                    has_expected_text = True
                    break
    assert has_expected_text, "Expected out of scope rejection message"
