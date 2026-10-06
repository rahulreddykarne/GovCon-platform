"""Preserve valid suggestions and reject contradictory award attribution."""
import pytest
from test_outcome_suggestions import (
    award_notice,
    run,
    submitted_bid,
    suggestions,
)
from test_outcome_suggestions import (
    db as db,
)
from test_outcome_suggestions import (
    our_uei as our_uei,
)

from govcon.config import get_settings
from govcon.scheduler.jobs import step_outcome_suggestions


@pytest.mark.parametrize("agency", [None, "Department of Defense / Defense Logistics Agency"])
def test_current_valid_notice_and_scheduler_result_are_preserved(db, our_uei, agency):
    opp = submitted_bid(db)
    notice = award_notice(db, opp, awardee_uei=our_uei)
    notice.agency_path = agency
    db.commit()
    result = step_outcome_suggestions(db, get_settings())
    db.commit()
    [suggestion] = suggestions(db, opp)
    assert suggestion.strength == "strong" and suggestion.suggested_outcome == "won"
    assert result.status == "succeeded" and result.extra["pursuits_checked"] >= 1
    run()
    assert len(suggestions(db, opp)) == 1


@pytest.mark.parametrize("conflict", ["agency", "source", "old_award", "future_award"])
def test_contradictory_notice_cannot_establish_an_outcome(db, our_uei, conflict):
    from datetime import timedelta

    from test_outcome_suggestions import SUBMITTED
    opp = submitted_bid(db)
    notice = award_notice(db, opp, awardee_uei="OTHERVENDOR1")
    if conflict == "agency":
        notice.agency_path = "Department of Veterans Affairs.UNRELATED Office"
    elif conflict == "source":
        notice.source = "dibbs"
    else:
        day = SUBMITTED.date() + timedelta(days=-500 if conflict == "old_award" else 500)
        notice.raw = {**notice.raw, "award": {**notice.raw["award"], "date": day.isoformat()}}
    db.commit()
    run()
    assert suggestions(db, opp) == []
