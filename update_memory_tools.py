import re

with open("app/tools/memory.py", "r") as f:
    content = f.read()

# Remove the import of Agent and Gemini
content = re.sub(r'from google\.adk\.agents import Agent\n', '', content)
content = re.sub(r'from google\.adk\.models import Gemini\n', '', content)
content = re.sub(r'from app\.consts import MODEL_MEM_SUMMARY, MODEL_RETRIES\n', '', content)

# Remove memory_summarizer_agent
content = re.sub(r'# Define a summarizer agent that acts as a workflow block to extract knowledge\nmemory_summarizer_agent = Agent\([\s\S]*?\)\n\n', '', content)

# Add logging setup
logging_setup = """import logging

logger = logging.getLogger(__name__)

"""
content = content.replace("import datetime\n\n", "import datetime\n" + logging_setup)


# Replace search_memory to include logging
search_memory_replacement = """async def search_memory(query: str, tool_context: ToolContext) -> dict:
    \"\"\"Searches the agent's long-term semantic memory for past Linux packaging sessions, build quirks, dependency workarounds, and resolution steps.

    Args:
        query: The semantic search query (e.g. "missing argp", "zlib build error").

    Returns:
        A dictionary containing a list of relevant memory entries.
    \"\"\"
    try:
        res = await tool_context.search_memory(query)
    except Exception as e:
        logger.error(f"[MEMORY_TRACKING] action=search query=\\"{query}\\" results_count=0 error=\\"{e}\\"")
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

    logger.info(f"[MEMORY_TRACKING] action=search query=\\"{query}\\" results_count={len(memories)}")
    return {"status": "success", "results": memories}"""

content = re.sub(r'async def search_memory.*?(?=\n\nasync def)', search_memory_replacement, content, flags=re.DOTALL)

# Replace save_session_to_memory
save_memory_note_replacement = """async def save_memory_note(note: str, tool_context: ToolContext) -> dict:
    \"\"\"Extracts and saves key packaging quirks and solutions to long-term memory.

    Call this tool explicitly when a packaging milestone is achieved or a compilation quirk/build error is successfully resolved.

    Args:
        note: The exact error encountered and the precise fix that resolved it.

    Returns:
        A dictionary containing the status of the save action.
    \"\"\"
    if not note or not note.strip():
        return {"status": "ignored", "message": "Note is empty."}

    # Save the note directly to memory using the framework's add_memory method
    entry = MemoryEntry(
        content=types.Content(
            role="model",
            parts=[types.Part(text=note.strip())]
        ),
        author=tool_context.agent_name if hasattr(tool_context, "agent_name") else "unknown_agent",
        timestamp=datetime.datetime.now().isoformat()
    )

    try:
        await tool_context.add_memory(memories=[entry])
        logger.info(f"[MEMORY_TRACKING] action=save note_length={len(note)}")
    except Exception as e:
        logger.error(f"[MEMORY_TRACKING] action=save note_length={len(note)} error=\\"{e}\\"")
        return {"status": "fallback", "message": f"Memory service unavailable: {e}"}
    return {"status": "success", "message": "Memory note saved."}"""

content = re.sub(r'async def save_session_to_memory[\s\S]*', save_memory_note_replacement, content)


with open("app/tools/memory.py", "w") as f:
    f.write(content)
