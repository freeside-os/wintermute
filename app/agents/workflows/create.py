from google.adk.agents import Agent
from google.adk.workflow import Workflow
from pydantic import ConfigDict

from app.agents.nodes import (
    auto_heal_step,
    compile_step,
    rebuild_step,
    verify_step,
)


class CreateWorkflow(Workflow):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    scaffold_agent: Agent
    refiner_agent: Agent
    builder_agent: Agent

    def model_post_init(self, context) -> None:
        self.edges = [
            ("START", self.scaffold_agent, self.refiner_agent, compile_step),
            (
                compile_step,
                {
                    "SUCCESS": verify_step,
                    "FAILURE": auto_heal_step,
                },
            ),
            (
                auto_heal_step,
                {
                    "REBUILD": rebuild_step,
                    "DELEGATE": self.builder_agent,
                },
            ),
            (
                rebuild_step,
                {
                    "SUCCESS": verify_step,
                    "FAILURE": self.builder_agent,
                },
            ),
            (self.builder_agent, verify_step),
        ]
        super().model_post_init(context)
