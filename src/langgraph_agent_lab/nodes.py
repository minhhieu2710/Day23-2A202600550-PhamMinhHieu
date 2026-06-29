# ruff: noqa: E501
"""Node functions for the LangGraph workflow.

Each function receives AgentState and returns a partial state update dict.
Do NOT mutate input state — return new values only.

LLM REQUIREMENT:
- classify_node MUST use a real LLM call (structured output for intent classification)
- answer_node MUST use a real LLM call (grounded response generation)
- evaluate_node SHOULD use LLM-as-judge (bonus points; heuristic acceptable for base score)
"""

from __future__ import annotations

import os
from typing import cast

from langgraph.types import interrupt
from pydantic import BaseModel, Field

from .llm import get_llm
from .state import AgentState, make_event


# ─── EXAMPLE: working node (provided for reference) ──────────────────
def intake_node(state: AgentState) -> dict:
    """Normalize raw query. This node is provided as a working example."""
    query = state.get("query", "").strip()
    return {
        "query": query,
        "messages": [f"intake:{query[:40]}"],
        "events": [make_event("intake", "completed", "query normalized")],
    }


# ─── TODO(student): implement ALL nodes below ────────────────────────


class Classification(BaseModel):
    route: str = Field(description="The chosen route: 'simple', 'tool', 'missing_info', 'risky', or 'error'")
    risk_level: str = Field(description="'high' if the route is risky, 'low' otherwise")

class Evaluation(BaseModel):
    result: str = Field(description="'needs_retry' if there was an error/failure, or 'success' if the tool successfully retrieved data")
    reason: str = Field(description="Reasoning for the evaluation")


def classify_node(state: AgentState) -> dict:
    """Classify the query into a route using an LLM.

    *** MUST use a real LLM call — keyword-only heuristics will lose points. ***

    Use .with_structured_output() or equivalent to get reliable enum classification.
    The LLM should classify into one of: simple, tool, missing_info, risky, error.
    """
    llm = get_llm(temperature=0.0)
    structured_llm = llm.with_structured_output(Classification)
    
    prompt = f"""You are a support-ticket classification assistant.
Analyze the user support ticket query and classify it into exactly one of these categories:
- 'risky': Actions with side effects (e.g. refund, delete customer account, send verification email, cancel subscription).
- 'tool': Information lookups (e.g. order status, tracking lookup, search queries).
- 'missing_info': Vague, short, or incomplete queries lacking actionable context (e.g. "Can you fix it?", "help me").
- 'error': System/technical failures (e.g. timeout, service crash, system error).
- 'simple': General questions answerable without tools or actions (e.g. "How do I reset my password?", "What are your hours?").

Priority: risky > tool > missing_info > error > simple. If multiple categories apply, choose the highest priority one.
If the route is 'risky', set risk_level to 'high'. Otherwise, set risk_level to 'low'.

User support ticket query:
{state.get("query", "")}"""

    classification = cast(Classification, structured_llm.invoke(prompt))
    route = classification.route
    risk_level = classification.risk_level
    
    return {
        "route": route,
        "risk_level": risk_level,
        "events": [make_event("classify", "completed", f"classified query as route={route}, risk_level={risk_level}")],
    }


def tool_node(state: AgentState) -> dict:
    """Execute a mock tool call."""
    attempt = state.get("attempt", 0)
    route = state.get("route")
    if route == "error" and attempt < 2:
        result = "ERROR: Transient tool failure"
    else:
        result = f"Success: Lookup result for query '{state.get('query')}'"
    
    return {
        "tool_results": [result],
        "events": [make_event("tool", "completed", f"executed tool with result: {result[:40]}")]
    }


def evaluate_node(state: AgentState) -> dict:
    """Evaluate tool results — the retry-loop gate."""
    results = state.get("tool_results", [])
    latest_result = results[-1] if results else ""
    heuristic_res = "needs_retry" if "ERROR" in latest_result else "success"
    
    try:
        llm = get_llm(temperature=0.0)
        structured_llm = llm.with_structured_output(Evaluation)
        prompt = f"""You are an LLM-as-judge evaluating a tool execution result.
Tool Result to evaluate:
{latest_result}

Determine if this result is a failure (needs retry) or a success.
Return 'needs_retry' if there is an error, timeout, or failure. Otherwise, return 'success'."""
        eval_output = cast(Evaluation, structured_llm.invoke(prompt))
        eval_res = eval_output.result
    except Exception:
        eval_res = heuristic_res
        
    return {
        "evaluation_result": eval_res,
        "events": [make_event("evaluate", "completed", f"evaluation result (LLM-as-judge): {eval_res}")]
    }


def answer_node(state: AgentState) -> dict:
    """Generate a final response using an LLM.

    *** MUST use a real LLM call — hardcoded strings will lose points. ***
    """
    llm = get_llm(temperature=0.0)
    
    query = state.get("query", "")
    tool_results = state.get("tool_results", [])
    approval = state.get("approval")
    
    prompt = f"""You are a helpful customer support agent.
Generate a polite, clear, and grounded response to the user's support ticket.

Original User Query: {query}
Tool Execution Results: {tool_results}
Human-in-the-loop Approval Status: {approval}

Base your answer strictly on the tool execution results and approval details provided. Do not hallucinate or make up any details."""
    
    response = llm.invoke(prompt)
    final_answer = response.content
    
    return {
        "final_answer": final_answer,
        "events": [make_event("answer", "completed", "generated final answer")],
    }


def ask_clarification_node(state: AgentState) -> dict:
    """Ask for missing information instead of hallucinating."""
    llm = get_llm(temperature=0.0)
    query = state.get("query", "")
    prompt = f"""The user support ticket query is vague or missing information: "{query}"
Generate a polite question to ask the user for the missing details required to help them."""
    response = llm.invoke(prompt)
    question = response.content
    return {
        "pending_question": question,
        "final_answer": question,
        "events": [make_event("clarify", "completed", f"asked clarification: {question[:40]}")],
    }


def risky_action_node(state: AgentState) -> dict:
    """Prepare a risky action for human approval."""
    query = state.get("query", "")
    proposed_action = f"Requesting approval to execute risky action: '{query}'"
    return {
        "proposed_action": proposed_action,
        "events": [make_event("risky_action", "completed", f"prepared risky action: {proposed_action[:40]}")]
    }


def approval_node(state: AgentState) -> dict:
    """Human-in-the-loop approval step."""
    approval = state.get("approval")
    if approval is not None:
        return {
            "events": [make_event("approval", "completed", f"resumed with approval: {approval.get('approved')}")]
        }

    if os.getenv("LANGGRAPH_INTERRUPT") == "true":
        user_input = interrupt({
            "action": "approve_risky_action",
            "proposed_action": state.get("proposed_action"),
        })
        if isinstance(user_input, dict):
            decision = {
                "approved": user_input.get("approved", False),
                "reviewer": user_input.get("reviewer", "human-in-the-loop"),
                "comment": user_input.get("comment", ""),
            }
        elif isinstance(user_input, bool):
            decision = {
                "approved": user_input,
                "reviewer": "human-in-the-loop",
                "comment": "",
            }
        else:
            decision = {
                "approved": False,
                "reviewer": "human-in-the-loop",
                "comment": str(user_input),
            }
    else:
        decision = {
            "approved": True,
            "reviewer": "mock-reviewer",
            "comment": "Auto-approved mock",
        }

    return {
        "approval": decision,
        "events": [make_event("approval", "completed", f"approval decision: {decision['approved']}")]
    }


def retry_or_fallback_node(state: AgentState) -> dict:
    """Record a retry attempt."""
    attempt = state.get("attempt", 0) + 1
    err_msg = f"Attempt {attempt} failed: Transient error occurred"
    return {
        "attempt": attempt,
        "errors": [err_msg],
        "events": [make_event("retry", "completed", f"incremented attempt to {attempt}")]
    }


def dead_letter_node(state: AgentState) -> dict:
    """Handle unresolvable failures after max retries exceeded."""
    msg = "Unable to process your request at this time. The system encountered multiple failures and has escalated the issue to human administrators."
    return {
        "final_answer": msg,
        "events": [make_event("dead_letter", "completed", "max retries exceeded, escalated to dead letter")]
    }


def finalize_node(state: AgentState) -> dict:
    """Emit a final audit event. All routes must pass through here before END."""
    return {
        "events": [make_event("finalize", "completed", "workflow finished")]
    }
