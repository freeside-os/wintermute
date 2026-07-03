import asyncio


def search_packaging_resources(query: str) -> str:
    """Searches official packaging resources for compilation guides, issues, or package specifications.

    Args:
        query: The search query string.

    Returns:
        The search results text from Google Search.
    """
    from google.adk.agents import Agent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.adk.tools import google_search
    from google.genai import types

    from app.consts import MODEL_TRIAGE

    domains = [
        "alpinelinux.org",
        "archlinux.org",
        "gentoo.org",
        "musl-libc.org",
        "github.com/systemd/systemd",
        "linuxfromscratch.org",
        "stackoverflow.com",
        "unix.stackexchange.com"
    ]
    site_query = " OR ".join(f"site:{domain}" for domain in domains)
    wrapped_query = f"({query}) ({site_query})"

    agent = Agent(
        name="web_search_agent",
        model=MODEL_TRIAGE,
        instruction="You are a web search assistant. Use the google_search tool to perform search and return the relevant results.",
        tools=[google_search]
    )

    async def run_search():
        session_service = InMemorySessionService()
        await session_service.create_session(app_name="search_app", user_id="user", session_id="s1")
        runner = Runner(agent=agent, app_name="search_app", session_service=session_service)

        ans = ""
        async for event in runner.run_async(
            user_id="user",
            session_id="s1",
            new_message=types.Content(role="user", parts=[types.Part.from_text(text=f"Please search for: {wrapped_query}")]),
        ):
            if event.is_final_response() and event.content and event.content.parts:
                ans = event.content.parts[0].text
        return ans

    try:
        import nest_asyncio
        nest_asyncio.apply()
    except Exception:
        pass

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, run_search()).result()
    else:
        return asyncio.run(run_search())
