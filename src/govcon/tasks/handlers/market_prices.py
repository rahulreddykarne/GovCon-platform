"""``market_price_research``: re-run the web price search for one opportunity.

Preparation runs the same step automatically for pursued product
opportunities; this task is the Products tab's "Search web prices again".
See ``govcon.sourcing.market_prices`` for the search itself.
"""

from __future__ import annotations

from govcon.sourcing import market_prices
from govcon.tasks.registry import Step, TaskHandler, register

register(TaskHandler(task_type=market_prices.MARKET_PRICE_TASK, steps=[
    Step("search", prepare=market_prices.prepare_step, execute=market_prices.execute_step,
         publish=market_prices.publish_step, timeout_seconds=900),
]))
