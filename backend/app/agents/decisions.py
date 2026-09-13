"""Human-in-the-loop decision gate (agent mode).

The product rule: the agent runs autonomously and only surfaces when there's
a real decision to make. `ask_traveller` is the single gate for that — the
system prompt restricts it to closing-miss / over-pace / over-budget /
empty-day decisions, and Strands interrupts pause the agent loop until the
traveller's answer arrives.
"""

from __future__ import annotations

from strands import tool
from strands.types.tools import ToolContext


@tool(context=True)
def ask_traveller(
    tool_context: ToolContext,
    question: str,
    options: list[str],
    context: str = "",
) -> str:
    """Pause and ask the traveller to decide. Use ONLY for real decisions:
    a spot that can't be reached before closing, a day over the comfortable
    pace, a budget over what the traveller said, or an empty day that needs
    a purpose. Never for trivia you can decide yourself.

    Args:
        question: one concrete question, decision-ready.
        options: 2-4 concrete, mutually exclusive choices.
        context: one line of facts the traveller needs (times, prices).

    Returns the traveller's chosen option verbatim.
    """
    answer = tool_context.interrupt(
        "trip-decision",
        reason={"question": question, "options": options, "context": context},
    )
    return str(answer)
