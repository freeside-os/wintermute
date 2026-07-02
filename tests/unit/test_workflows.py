import pytest
from google.adk.agents import Agent
from google.adk.models import Gemini

from app.workflows.create import CreateWorkflow
from app.workflows.fix import FixWorkflow


def test_workflow_initialization() -> None:
    # Set up dummy agents to initialize the workflows
    dummy_model = Gemini(model="gemini-3.5-flash")
    refiner = Agent(name="refiner", model=dummy_model, instruction="")
    builder = Agent(name="builder", model=dummy_model, instruction="")

    create_wf = CreateWorkflow(
        name="test_create",
        scaffold_agent=refiner,
        refiner_agent=refiner,
        builder_agent=builder
    )
    assert create_wf.name == "test_create"
    assert create_wf.refiner_agent == refiner
    assert create_wf.builder_agent == builder

    fix_wf = FixWorkflow(
        name="test_fix",
        refiner_agent=refiner,
        builder_agent=builder
    )
    assert fix_wf.name == "test_fix"
    assert fix_wf.refiner_agent == refiner
    assert fix_wf.builder_agent == builder


@pytest.mark.asyncio
async def test_workflow_package_override_if_exists() -> None:
    from unittest.mock import MagicMock, patch

    from google.adk.agents import Context
    from google.adk.events import Event
    from google.genai import types

    from app.agent import Workflow

    # Set up dummy agents to initialize the workflows
    dummy_model = Gemini(model="gemini-3.5-flash")
    triage_mock = Agent(name="triage", model=dummy_model, instruction="")
    refiner_mock = Agent(name="refiner", model=dummy_model, instruction="")
    builder_mock = Agent(name="builder", model=dummy_model, instruction="")
    scaffold_mock = Agent(name="scaffold", model=dummy_model, instruction="")

    async def mock_triage_run(ctx):
        ctx.session.state["triage_done"] = True
        ctx.session.state["triage_output"] = '{"pkg_name": "zlib", "action": "import", "version": "1.3.1", "group": "extra", "is_security_update": false}'
        yield Event(author="triage", content=types.Content(parts=[types.Part(text="Triage complete")]))

    mock_review_event = Event(author="review_workflow", content=types.Content(parts=[types.Part(text="Reviewing...")]))

    coordinator = Workflow(
        name="coordinator",
        triage_agent=triage_mock,
        refiner_agent=refiner_mock,
        builder_agent=builder_mock,
        scaffold_agent=scaffold_mock,
        audit_agent=Agent(name="audit", model=dummy_model, instruction="")
    )

    mock_ic = MagicMock()
    mock_ic.session.state = {
        "triage_done": False,
        "pending_approval": False,
    }
    mock_ic.session.events = []

    mock_ctx = MagicMock(spec=Context)
    mock_ctx.get_invocation_context.return_value = mock_ic

    with patch("google.adk.agents.Agent.run_async") as mock_triage_run_method:
        mock_triage_run_method.side_effect = mock_triage_run
        with patch("app.tools.list_workspace_packages", return_value={"status": "success", "packages": ["zlib"]}):
            with patch("app.workflows.review.ReviewWorkflow.run_async") as mock_review_run:
                async def dummy_review_run(*args, **kwargs):
                    yield mock_review_event
                mock_review_run.side_effect = dummy_review_run

                events = []
                async for ev in coordinator._run_impl(ctx=mock_ctx, node_input=None):
                    events.append(ev)

                assert mock_ic.session.state["action"] == "review"
                mock_review_run.assert_called_once()
