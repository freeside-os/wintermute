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

import datetime

from google.adk.agents import Agent
from google.adk.memory.memory_entry import MemoryEntry
from google.adk.models import Gemini
from google.adk.tools import ToolContext
from google.genai import types

# Define a summarizer agent that acts as a workflow block to extract knowledge
memory_summarizer_agent = Agent(
    name="memory_summarizer_agent",
    # Use flash model for fast and cost-effective summarization
    model=Gemini(
        model="gemini-3.5-flash",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=(
        "You are the Memory Summarizer Agent for Freeside OS.\n"
        "Your task is to analyze the history of a packaging/build session and extract any valuable lessons, "
        "build quirks, dependency workarounds, or successful compilation configurations.\n"
        "Guidelines:\n"
        "1. Let the nature of the issue dictate what information to capture and how to organize it. "
        "Include relevant error messages, root causes, workarounds, config files, or steps to reproduce/solve.\n"
        "2. If the session did not encounter any new compilation errors, quirks, or nuance (e.g. it was a simple check or review with no changes), "
        "return exactly 'NO_KNOWLEDGE_TO_SAVE'. Do not save trivial sessions."
    )
)


async def search_memory(query: str, tool_context: ToolContext) -> dict:
    """Searches the agent's long-term semantic memory for past Linux packaging sessions, build quirks, dependency workarounds, and resolution steps.

    Args:
        query: The semantic search query (e.g. "missing argp", "zlib build error").

    Returns:
        A dictionary containing a list of relevant memory entries.
    """
    try:
        res = await tool_context.search_memory(query)
    except Exception as e:
        return {"status": "fallback", "results": [], "message": f"Memory service unavailable: {e}"}
    memories = []

    # Handle both SearchMemoryResponse (which has memories attribute) and custom list subclasses
    mem_list = res.memories if hasattr(res, "memories") else res
    for m in mem_list:
        text = " ".join([p.text for p in m.content.parts if p.text]) if m.content and m.content.parts else ""
        memories.append({
            "id": m.id,
            "author": m.author,
            "timestamp": m.timestamp,
            "content": text
        })
    return {"status": "success", "results": memories}


async def save_session_to_memory(tool_context: ToolContext) -> dict:
    """Extracts and saves key packaging quirks and solutions from this session to long-term memory.

    Call this tool when a packaging milestone is achieved or a compilation quirk/build error is successfully resolved.

    Returns:
        A dictionary containing the status of the save action.
    """
    # 1. Format the transcript of the current session
    texts = []
    for event in tool_context.session.events:
        if event.content and event.content.parts:
            parts_text = [p.text for p in event.content.parts if p.text]
            if parts_text:
                texts.append(f"[{event.author}]: {' '.join(parts_text)}")

    transcript = "\n".join(texts).strip()
    if not transcript:
        return {"status": "ignored", "message": "Session is empty."}

    # 2. Run the summarizer agent to let the model extract knowledge open-endedly
    res = await tool_context.run_node(memory_summarizer_agent, node_input=transcript)
    summary_text = str(res).strip()

    if "NO_KNOWLEDGE_TO_SAVE" in summary_text:
        return {"status": "ignored", "message": "No new packaging knowledge detected in this session."}

    # 3. Save the summary directly to memory using the framework's add_memory method
    entry = MemoryEntry(
        content=types.Content(
            role="model",
            parts=[types.Part(text=summary_text)]
        ),
        author="memory_summarizer_agent",
        timestamp=datetime.datetime.now().isoformat()
    )

    try:
        await tool_context.add_memory(memories=[entry])
    except Exception as e:
        return {"status": "fallback", "message": f"Memory service unavailable: {e}", "summary": summary_text}
    return {"status": "success", "message": "Session summary saved to memory.", "summary": summary_text}
