import asyncio
import sys


def configure_windows_event_loop_policy() -> bool:
    """Use SelectorEventLoop on Windows to avoid Proactor shutdown noise."""
    if sys.platform != "win32":
        return False

    policy_cls = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy_cls is None:
        return False

    current_policy = asyncio.get_event_loop_policy()
    if isinstance(current_policy, policy_cls):
        return False

    asyncio.set_event_loop_policy(policy_cls())
    return True