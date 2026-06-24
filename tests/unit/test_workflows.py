# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

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
