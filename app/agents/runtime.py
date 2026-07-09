import asyncio
import click
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.services import memory_factory


def run_workflow_sync(workflow_node, pkg_name: str, state_vars: dict) -> dict:
    """Synchronously executes a workflow agent node using InMemorySessionService."""

    async def _run():
        session_service = InMemorySessionService()

        # Prepare initial state with package name and custom state vars
        initial_state = {"pkg_name": pkg_name}
        for k, v in state_vars.items():
            initial_state[k] = v

        await session_service.create_session(
            app_name="app",
            user_id="cli_user",
            session_id="s1",
            state=initial_state,
        )

        runner = Runner(
            agent=workflow_node,
            app_name="app",
            session_service=session_service,
            memory_service=memory_factory("memory://"),
        )

        async for event in runner.run_async(
            user_id="cli_user",
            session_id="s1",
            new_message=types.Content(
                role="user", parts=[types.Part.from_text(text="Start workflow")]
            ),
        ):
            # Print text content (thoughts / progress / answers) from agents
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        author = event.author if event.author else "workflow"
                        if author not in ("user", "root_agent"):
                            click.echo(
                                click.style(f"[{author}] ", fg="cyan", bold=True)
                                + part.text
                            )
                        else:
                            click.echo(part.text)

            # Print function calls (tools being triggered)
            for fc in event.get_function_calls():
                args_str = (
                    ", ".join(f"{k}={v}" for k, v in fc.args.items())
                    if fc.args
                    else ""
                )
                click.echo(
                    click.style(
                        f"  🔧 Calling tool '{fc.name}' ({args_str})...",
                        fg="yellow",
                    )
                )

            # Print function responses (tool completions)
            for fr in event.get_function_responses():
                status = "success"
                response_val = fr.response
                if isinstance(response_val, dict):
                    status = response_val.get("status", "completed")
                    if status == "error":
                        click.echo(
                            click.style(
                                f"  ✗ Tool '{fr.name}' failed: {response_val.get('message', 'Unknown error')}",
                                fg="red",
                                bold=True,
                            )
                        )
                        continue
                click.echo(
                    click.style(
                        f"  ✓ Tool '{fr.name}' completed with status: {status}",
                        fg="green",
                    )
                )

        updated_session = await session_service.get_session(
            app_name="app", user_id="cli_user", session_id="s1"
        )
        return updated_session.state

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
            return pool.submit(asyncio.run, _run()).result()
    else:
        return asyncio.run(_run())
