"""
Agent wiring for the proof-of-solution.

Same model, same MCP tools, same system prompt as proof-of-problem/agent.py
- the point is NOT that a smarter prompt fixes this (the blog spends a
whole section, "Why a Stronger System Prompt Is Not Enough," arguing the
opposite). The point is that the harness now sits between the model's
tool call and the tool's actual execution.

Two ideas from the blog are made concrete here:

1. Preserve Provenance (section 1): the harness tracks, outside the
   token stream, whether untrusted content has entered context this turn.
   The blog's example table:
       source = customer-controlled, trust = untrusted, authority = none
   vs.
       source = developer, trust = trusted, authority = policy
   ProvenanceState below is a minimal version of that distinction - a
   flag the model cannot edit by rephrasing a sentence, because it isn't
   represented in the prose at all.

2. Treat Tool Output as Data, Not Authority (section 4): fetch_ticket's
   return value is still handed to the model so it can summarize the
   ticket - reading is not restricted. What's restricted is letting that
   same content, however persuasive, authorize a later call to a gated
   tool. The model may still *propose* send_email. Whether it *executes*
   is decided in policy.py, not by the model.
"""

from pathlib import Path
from dataclasses import dataclass, field
from dotenv import load_dotenv
import os
load_dotenv()
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.prebuilt import create_react_agent

from policy import authorize_tool_call, GATED_ACTIONS

SERVER_SCRIPT = str(Path(__file__).parent / "mcp_server.py")

# Unchanged from proof-of-problem/agent.py on purpose - see this file's
# docstring. The blog's point is that the fix does not belong in the
# prompt, so the prompt is not where this version differs.
SYSTEM_PROMPT = (
    "You are a support assistant. Use the fetch_ticket tool to look up "
    "tickets and help summarize or resolve them for the human agent."
)


@dataclass
class ProvenanceState:
    """Tracks trust outside the model's context, per the blog's
    "authority should be represented structurally, not inferred from
    prose" principle. Starts 'trusted' (only developer + user have
    spoken); flips to 'untrusted' the moment customer-controlled content
    (a fetched ticket) enters the conversation, and the policy engine
    reads this - not the ticket text - when it decides on send_email.
    """
    trust: str = "trusted"
    blocked_calls: list = field(default_factory=list)


def _wrap_with_policy(tool, state: ProvenanceState):
    """Wrap one MCP tool so its execution passes through policy.py first.

    This is the harness enforcement point the blog describes in
    "Put Deterministic Policy Outside the Model":
        "The LLM can propose an action. The harness decides whether the
         action is authorized."
    The model still sees the tool, can still call it, and still gets a
    ToolMessage back - but for gated tools, that message may say "denied"
    instead of actually running the tool body.
    """
    original_coroutine = tool.coroutine

    async def policy_checked(*args, **kwargs):
        # fetch_ticket is how customer-controlled content enters context.
        # Flip provenance the moment it's called - not based on what the
        # ticket says, but based on the structural fact that untrusted
        # data was just retrieved. This is set BEFORE the tool runs so
        # the very first send_email attempt after a fetch is already
        # covered.
        if tool.name == "fetch_ticket":
            state.trust = "untrusted"

        if tool.name in GATED_ACTIONS:
            decision = authorize_tool_call(tool.name, kwargs, state.trust)
            if not decision.allowed:
                state.blocked_calls.append(
                    {"tool": tool.name, "args": kwargs, "reason": decision.reason}
                )
                # The tool body (the part that would actually write to
                # sent_emails.json) never runs. This is the difference
                # between "the model was persuaded to refuse" and
                # "the action violates policy" that the blog draws.
                return f"BLOCKED by policy: {decision.reason}"

        return await original_coroutine(*args, **kwargs)

    tool.coroutine = policy_checked
    return tool


async def build_agent():
    """Wire the agent to the MCP server with every gated tool wrapped by
    the policy engine before the model ever sees it. Returns both the
    agent and the shared ProvenanceState so main.py can inspect
    state.blocked_calls afterward as evidence the boundary held.
    """
    client = MultiServerMCPClient(
        {
            "support": {
                "command": "python",
                "args": [SERVER_SCRIPT],
                "transport": "stdio",
                "env": dict(os.environ),
            }
        }
    )
    tools = await client.get_tools()

    state = ProvenanceState()
    tools = [_wrap_with_policy(t, state) for t in tools]

    model = ChatGoogleGenerativeAI(model="gemini-flash-lite-latest", api_key=os.getenv("GOOGLE_API_KEY"))
    agent = create_react_agent(model, tools, prompt=SYSTEM_PROMPT)
    return agent, state


async def run(agent, user_request: str):
    """Send a single user request through the agent and return the full
    message transcript, same shape as proof-of-problem/agent.py so the
    two runs are directly comparable.
    """
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": user_request}]}
    )
    return result["messages"]