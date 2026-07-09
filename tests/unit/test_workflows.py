import pytest
from google.adk.agents import Agent
from google.adk.models import Gemini

from app.agents.workflows.create import CreateWorkflow
from app.agents.workflows.fix import FixWorkflow


def test_workflow_initialization() -> None:
    create_wf = CreateWorkflow(name="test_create")
    assert create_wf.name == "test_create"
    assert create_wf.scaffold_agent is not None
    assert create_wf.refiner_agent is not None
    assert create_wf.builder_agent is not None

    fix_wf = FixWorkflow(name="test_fix")
    assert fix_wf.name == "test_fix"
    assert fix_wf.refiner_agent is not None
    assert fix_wf.builder_agent is not None



