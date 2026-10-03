"""Built-in task handlers; importing this package registers them."""

from govcon.tasks.handlers import analysis, chain, notification_email, proposal, quote_extraction  # noqa: F401
from govcon.workflow import preparation  # noqa: F401  (registers opportunity_preparation)
