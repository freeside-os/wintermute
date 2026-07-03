def update_task_status(status_message: str) -> None:
    """Updates the current task status message.

    This function prints the status message prefixed with '[status]' to standard output.

    Args:
        status_message: The message describing the current status/progress.
    """
    print(f"[status] {status_message}")
