import pytest
from google.adk.agents import Agent
from google.adk.models import Gemini

from app.agents.workflows.create import CreateWorkflow
from app.agents.workflows.fix import FixWorkflow


def test_workflow_initialization() -> None:
    # Set up dummy agents to initialize the workflows
    dummy_model = Gemini(model="gemini-3.5-flash")
    scaffold = Agent(name="scaffold", model=dummy_model, instruction="")
    refiner = Agent(name="refiner", model=dummy_model, instruction="")
    builder = Agent(name="builder", model=dummy_model, instruction="")

    create_wf = CreateWorkflow(
        name="test_create",
        scaffold_agent=scaffold,
        refiner_agent=refiner,
        builder_agent=builder
    )
    assert create_wf.name == "test_create"
    assert create_wf.scaffold_agent == scaffold
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



