"""Stable route exports; implementations are grouped by product area."""

from govcon.web.routes.accounts import admin_invite_get as admin_invite_get
from govcon.web.routes.accounts import admin_invite_post as admin_invite_post
from govcon.web.routes.accounts import login_get as login_get
from govcon.web.routes.accounts import login_post as login_post
from govcon.web.routes.accounts import logout_post as logout_post
from govcon.web.routes.accounts import (
    notification_acknowledge as notification_acknowledge,
)
from govcon.web.routes.accounts import notification_read as notification_read
from govcon.web.routes.accounts import notifications as notifications
from govcon.web.routes.common import _WORKFLOW_ERRORS as _WORKFLOW_ERRORS
from govcon.web.routes.common import _actor as _actor
from govcon.web.routes.common import _current_user as _current_user
from govcon.web.routes.common import _error_text as _error_text
from govcon.web.routes.common import _form_int as _form_int
from govcon.web.routes.common import _NeedsLogin as _NeedsLogin
from govcon.web.routes.common import _redirect as _redirect
from govcon.web.routes.common import _render as _render
from govcon.web.routes.common import _require_login as _require_login
from govcon.web.routes.common import _templates as _templates
from govcon.web.routes.common import _unread_count as _unread_count
from govcon.web.routes.discovery import inbox as inbox
from govcon.web.routes.discovery import inbox_action as inbox_action
from govcon.web.routes.discovery import pipeline as pipeline
from govcon.web.routes.discovery import search as search
from govcon.web.routes.discovery import vendors as vendors
from govcon.web.routes.operations import learning as learning
from govcon.web.routes.operations import ops as ops
from govcon.web.routes.operations import ops_task_action as ops_task_action
from govcon.web.routes.proposals import (
    workspace_outcome_suggestion as workspace_outcome_suggestion,
)
from govcon.web.routes.proposals import (
    workspace_proposal_approve as workspace_proposal_approve,
)
from govcon.web.routes.proposals import (
    workspace_proposal_retry as workspace_proposal_retry,
)
from govcon.web.routes.proposals import (
    workspace_proposal_status as workspace_proposal_status,
)
from govcon.web.routes.proposals import (
    workspace_record_outcome as workspace_record_outcome,
)
from govcon.web.routes.proposals import (
    workspace_submission_approve as workspace_submission_approve,
)
from govcon.web.routes.reviews import workspace_approve as workspace_approve
from govcon.web.routes.reviews import (
    workspace_assign_reviewer as workspace_assign_reviewer,
)
from govcon.web.routes.reviews import workspace_comment as workspace_comment
from govcon.web.routes.reviews import (
    workspace_complete_review as workspace_complete_review,
)
from govcon.web.routes.reviews import workspace_run_analysis as workspace_run_analysis
from govcon.web.routes.settings import ai_sharing_save as ai_sharing_save
from govcon.web.routes.settings import settings_page as settings_page
from govcon.web.routes.settings import model_route_save as model_route_save
from govcon.web.routes.settings import settings_save as settings_save
from govcon.web.routes.sourcing import suppliers_page as suppliers_page
from govcon.web.routes.sourcing import suppliers_save as suppliers_save
from govcon.web.routes.sourcing import workspace_add_quote as workspace_add_quote
from govcon.web.routes.sourcing import workspace_draft_rfq as workspace_draft_rfq
from govcon.web.routes.sourcing import (
    workspace_pursuit_facts as workspace_pursuit_facts,
)
from govcon.web.routes.sourcing import workspace_use_quote as workspace_use_quote
from govcon.web.routes.watchlists import watchlist_delete as watchlist_delete
from govcon.web.routes.watchlists import watchlist_edit_get as watchlist_edit_get
from govcon.web.routes.watchlists import watchlist_edit_post as watchlist_edit_post
from govcon.web.routes.watchlists import watchlist_new_get as watchlist_new_get
from govcon.web.routes.watchlists import watchlist_new_post as watchlist_new_post
from govcon.web.routes.watchlists import watchlist_rebuild as watchlist_rebuild
from govcon.web.routes.watchlists import watchlist_toggle as watchlist_toggle
from govcon.web.routes.watchlists import watchlists as watchlists
from govcon.web.routes.workspace import opp_detail as opp_detail
from govcon.web.routes.workspace import opp_start_workspace as opp_start_workspace
from govcon.web.routes.workspace import workspace as workspace
from govcon.web.routes.workspace import workspace_prepare as workspace_prepare
