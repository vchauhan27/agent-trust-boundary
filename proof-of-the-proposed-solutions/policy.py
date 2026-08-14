"""
Deterministic policy engine - the "harness" half of the proof-of-solution.

Everything in proof-of-problem/ showed the model deciding, on its own,
whether a sentence inside a ticket had the authority to trigger
send_email. This file replaces that decision with code that does not
read prose at all. From the blog:

    "Suppose the model says: 'Send the database export to this address.'
     The policy engine should be able to answer: No. Not because the
     model was persuaded to refuse. Because the action violates policy."

    "The LLM can propose an action. The harness decides whether the
     action is authorized. This lets the model remain probabilistic
     while the security-critical parts of the system become
     deterministic."

Nothing here inspects whether the ticket "sounded" like an instruction.
It only checks the arguments of the proposed tool call against a fixed
policy - the same way, per the blog, "we do not expect a database to
decide which SQL query is trustworthy based on how politely it is
written."
"""

from dataclasses import dataclass

# Least-privilege allowlist for send_email. Per the blog's table in
# "Give Agents the Minimum Capabilities They Need": a ticket-summarizing
# agent has no legitimate reason to email an *external* address at all.
# Only internal, pre-approved recipients are ever authorized - this is a
# structural boundary, not a suggestion to the model.
APPROVED_EMAIL_DOMAINS = {"internal-support.example.com"}

# Actions in this set are treated as irreversible per the blog's "Gate
# Irreversible Actions" section: "send, delete, pay, publish, execute,
# change permissions." Anything in this set is denied by default unless
# it clears every check below - there is no path where prose inside a
# tool result can move a call from "gated" to "approved."
GATED_ACTIONS = {"send_email"}


@dataclass
class PolicyDecision:
    allowed: bool
    reason: str


def authorize_tool_call(tool_name: str, args: dict, source_trust: str) -> PolicyDecision:
    """Authorize (or deny) a proposed tool call before it executes.

    This function is called by the harness (agent.py), never by the
    model. Its answer is final regardless of how the model justified the
    call or what text it says it was following. This is the concrete
    version of the blog's claim:

        "Even if the model accepts this instruction, can the system
         prevent the resulting action? That is a much stronger security
         property."

    Args:
        tool_name: name of the tool the model is attempting to invoke.
        args: the arguments the model supplied for that call.
        source_trust: provenance tag for whatever content most recently
            entered context and could have motivated this call (e.g.
            "developer", "user", "tool-output:customer-controlled"). See
            provenance.py for how this tag gets attached. Per the blog's
            "Preserve Provenance" section, this is metadata the harness
            tracks *outside* the token stream, so it can't be forged by
            rephrasing a sentence inside a ticket.
    """
    # Ungated actions (e.g. fetch_ticket) pass straight through - the
    # blog is explicit that reading data is not the dangerous step:
    # "The dangerous question is not merely: 'Can the model understand
    # this sentence?'" It's what happens when a *consequential* action
    # follows.
    if tool_name not in GATED_ACTIONS:
        return PolicyDecision(allowed=True, reason="not a gated action")

    # A gated action motivated by untrusted content is exactly the attack
    # chain the blog describes:
    #     "untrusted content -> model interpretation -> tool invocation ->
    #      external consequence"
    # The policy engine breaks this chain at the third arrow, deterministically,
    # rather than hoping the model's interpretation step got it right.
    if source_trust != "trusted":
        return PolicyDecision(
            allowed=False,
            reason=(
                f"'{tool_name}' is a gated action and the most recent "
                f"content influencing this call was tagged "
                f"'{source_trust}', not 'trusted'. Per policy, untrusted "
                f"tool output has no authority to trigger a gated action, "
                f"regardless of what it says."
            ),
        )

    # Even for trusted-sourced calls, least privilege still applies: the
    # recipient domain must be pre-approved. This mirrors the blog's
    # point that tool output should never gain "the right to issue
    # commands to the agent" on its own - authority is never inferred
    # from the argument values themselves.
    to_addr = args.get("to", "")
    domain = to_addr.split("@")[-1] if "@" in to_addr else ""
    if domain not in APPROVED_EMAIL_DOMAINS:
        return PolicyDecision(
            allowed=False,
            reason=(
                f"recipient domain '{domain}' is not in the approved "
                f"allowlist {APPROVED_EMAIL_DOMAINS}."
            ),
        )

    return PolicyDecision(allowed=True, reason="passed all policy checks")