"""GovCon MCP server — safe read/write tools over implemented services."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from functools import wraps
from typing import Any

from fastmcp import FastMCP

from govcon.db import session_scope
from govcon.mcp.serialize import failure
from govcon.mcp import operations as ops
from govcon.collaboration.users import PermissionDenied

mcp = FastMCP(
    "GovCon",
    instructions=(
        "Operate the local GovCon opportunity and bid workflow. "
        "Read tools return compact structured JSON. "
        "Write tools echo the changed record. "
        "Submission tools never auto-submit to government portals."
    ),
)


def _tool(operation: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Register an operations.* function (session-first) as an MCP tool."""
    signature = inspect.signature(operation)
    params = list(signature.parameters.values())
    if not params or params[0].name != "session":
        raise TypeError(f"{operation.__name__} must accept session as the first argument")
    public_signature = signature.replace(parameters=params[1:])

    public_name = operation.__name__.removeprefix("op_")

    @wraps(operation)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            bound = public_signature.bind_partial(*args, **kwargs)
            bound.apply_defaults()
            with session_scope() as session:
                return operation(session, **bound.arguments)
        except TypeError as exc:
            return failure("validation_error", str(exc))
        except ValueError as exc:
            return failure("validation_error", str(exc))
        except (PermissionError, PermissionDenied) as exc:
            return failure("permission_denied", str(exc))
        except Exception as exc:  # noqa: BLE001
            return failure("operation_failed", f"{exc.__class__.__name__}: {exc}")

    wrapper.__name__ = public_name
    wrapper.__qualname__ = public_name
    wrapper.__signature__ = public_signature  # type: ignore[attr-defined]
    return mcp.tool(wrapper)


# Read tools
search_opportunities = _tool(ops.op_search_opportunities)
get_opportunity = _tool(ops.op_get_opportunity)
get_opportunity_history = _tool(ops.op_get_opportunity_history)
price_history = _tool(ops.op_price_history)
vendor_profile = _tool(ops.op_vendor_profile)
competitor_summary = _tool(ops.op_competitor_summary)
list_matches = _tool(ops.op_list_matches)
pipeline_summary = _tool(ops.op_pipeline_summary)
get_bid_analysis = _tool(ops.op_get_bid_analysis)
get_compliance_matrix = _tool(ops.op_get_compliance_matrix)
get_proposal = _tool(ops.op_get_proposal)
submission_status = _tool(ops.op_submission_status)
learning_summary = _tool(ops.op_learning_summary)
similar_opportunities = _tool(ops.op_similar_opportunities)

# Write tools
update_match = _tool(ops.op_update_match)
add_pursuit = _tool(ops.op_add_pursuit)
update_pursuit = _tool(ops.op_update_pursuit)
record_human_bid_decision = _tool(ops.op_record_human_bid_decision)
assign_reviewer = _tool(ops.op_assign_reviewer)
add_review_comment = _tool(ops.op_add_review_comment)
complete_review = _tool(ops.op_complete_review)
request_ai_comment_validation = _tool(ops.op_request_ai_comment_validation)
approve_to_bid = _tool(ops.op_approve_to_bid)
update_requirement_status = _tool(ops.op_update_requirement_status)
create_proposal_version = _tool(ops.op_create_proposal_version)
set_submission_ready = _tool(ops.op_set_submission_ready)
record_submission_confirmation = _tool(ops.op_record_submission_confirmation)
record_outcome = _tool(ops.op_record_outcome)


def main() -> None:
    """Run the MCP server on stdio (default local transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
