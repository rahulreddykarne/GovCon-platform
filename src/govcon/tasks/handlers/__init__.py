"""Built-in task handlers; importing this package registers them."""

from govcon.tasks.handlers import (  # noqa: F401
    analysis,
    bots,
    chain,
    market_prices,
    notification_email,
    proposal,
    quote_extraction,
)
from govcon.workflow import (
    attachment_download,  # noqa: F401  (registers attachment_download)
    preparation,  # noqa: F401  (registers opportunity_preparation)
)
