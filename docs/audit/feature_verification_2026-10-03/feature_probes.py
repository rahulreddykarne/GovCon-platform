from pathlib import Path
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
import json
import sys
import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'tests'))
from test_web_ui import _make_user
from web_client import CsrfTestClient
from govcon.web.app import create_app
from govcon.db import session_scope
from govcon.models import (Notification, User, Opportunity, Supplier, SupplierQuote, Task, ComplianceRun,
    OutcomeSuggestion, OutcomeFeedback, ReviewAssignment, ReviewSession, AppSetting)
from govcon.config import get_settings
RESULTS = []
def evidence(name, **values):
    RESULTS.append({'probe': name, **values})
    Path(__file__).with_name('probe-results.json').write_text(json.dumps(RESULTS, indent=2, default=str), encoding='utf-8')
    print(json.dumps(RESULTS[-1], default=str))
@pytest.fixture
def db(upgraded_engine):
    with Session(upgraded_engine) as s:
        yield s
@pytest.fixture
def actor(db):
    return _make_user(db, f'audit-{uuid4().hex}@example.test', 'owner')
@pytest.fixture
def client(upgraded_engine, actor):
    with CsrfTestClient(create_app(), follow_redirects=False, raise_server_exceptions=False) as c:
        c.cookies.set('govcon_session', actor[1])
        yield c
@pytest.fixture
def opp(db):
    row=Opportunity(source='sam',source_id=uuid4().hex,title='Nitrile gloves',status='open',
        quantity=Decimal('500'),unit='PR',nsn='6515-01-519-8818',raw={},links={},
        response_deadline=datetime.now(UTC)+timedelta(days=30))
    db.add(row); db.commit()
    return row

def test_catalog_overflow_row_is_unhandled(client):
    r=client.post('/suppliers',data={'name':f'Audit {uuid4().hex}'},
        files={'catalog_file':('catalog.csv',b'part_number,list_price\nGOOD,2\nBAD,3,unexpected\n','text/csv')})
    evidence('catalog_surplus_fields', http_status=r.status_code)
    assert r.status_code == 500

def test_corrupt_xlsx_is_unhandled(client,opp):
    r=client.post(f'/workspace/{opp.id}/quotes',data={'supplier_name':'Corrupt Workbook'},
        files={'quote_file':('quote.xlsx',b'not a ZIP workbook','application/octet-stream')})
    evidence('corrupt_xlsx', http_status=r.status_code)
    assert r.status_code == 500

def test_nan_total_is_unhandled(client,opp):
    r=client.post(f'/workspace/{opp.id}/quotes',data={'supplier_name':'Nonfinite','total_price':'NaN'})
    evidence('nan_quote_total', http_status=r.status_code)
    assert r.status_code == 500

def test_quote_double_upload_duplicates(client,opp,db):
    statuses=[]
    for _ in range(2):
        r=client.post(f'/workspace/{opp.id}/quotes',data={'supplier_name':'Same document'},
            files={'quote_file':('quote.csv',b'description,quantity,unit_price\nGloves,500,2\n','text/csv')})
        statuses.append(r.status_code)
    db.expire_all()
    quotes=list(db.scalars(select(SupplierQuote).where(SupplierQuote.opportunity_id==opp.id)))
    evidence('quote_double_upload', http_statuses=statuses, quote_count=len(quotes),same_sha=quotes[0].source_sha256==quotes[1].source_sha256)
    assert len(quotes)==2 and quotes[0].source_sha256==quotes[1].source_sha256

def test_quote_quantity_mismatch_undercosts(client,opp,db):
    from govcon.intelligence.ai_analyses import _REQUESTS
    r=client.post(f'/workspace/{opp.id}/quotes',data={'supplier_name':'Partial quantity'},
        files={'quote_file':('quote.csv',b'description,quantity,unit,unit_price\nGloves,5,PR,2\n','text/csv')})
    with session_scope() as s:
        inputs=_REQUESTS['pricing'](s,opp.id,get_settings())['variables']['PRICING_INPUTS_JSON']
    evidence('pricing_quantity_mismatch',requested_quantity=500,quoted_quantity=5,quoted_unit_price=2,pricing_inputs=inputs)
    assert inputs['sourcing_cost_total']==10 and inputs['unit_cost']==0.02

def test_wrong_agency_notice_suggests_false_loss(db,client,monkeypatch):
    from test_outcome_suggestions import submitted_bid, award_notice
    from govcon.learning.award_matching import suggest_outcomes
    monkeypatch.setenv('COMPANY_UEI','OURUEI123456'); get_settings.cache_clear()
    bid=submitted_bid(db)
    notice=award_notice(db,bid,awardee_uei='OTHERVENDOR1')
    notice.agency_path='DEPARTMENT OF VETERANS AFFAIRS.UNRELATED OFFICE'; db.commit()
    with session_scope() as s: suggest_outcomes(s)
    db.expire_all()
    suggestion=db.scalar(select(OutcomeSuggestion).where(OutcomeSuggestion.opportunity_id==bid.id))
    r=client.post(f'/workspace/{bid.id}/outcome-suggestions/{suggestion.id}/confirm')
    db.expire_all()
    feedback=db.scalar(select(OutcomeFeedback).where(OutcomeFeedback.opportunity_id==bid.id))
    evidence('unrelated_award_notice',bid_agency=bid.agency_path,notice_agency=notice.agency_path,
        strength=suggestion.strength,suggested=suggestion.suggested_outcome,confirm_status=r.status_code,recorded_outcome=feedback.outcome if feedback else None)
    assert suggestion.strength=='strong' and feedback.outcome=='lost'

def test_cancelled_preparation_still_commits_compliance(opp,db,actor,client,monkeypatch):
    from govcon.workflow.preparation import queue_preparation, PREPARATION_TASK
    from govcon.tasks.worker import run_once
    from govcon.compliance.pipeline import run_compliance_pipeline
    with session_scope() as s:
        task,_=queue_preparation(s,opportunity_id=opp.id,actor_user_id=actor[0].id)
        task_id=task.id
    cancel_status=[]
    def cancel_then_run(session, opportunity_id, **kwargs):
        r=client.post(f'/ops/tasks/{task_id}/cancel')
        cancel_status.append(r.status_code)
        return run_compliance_pipeline(session,opportunity_id,**kwargs)
    monkeypatch.setattr('govcon.compliance.pipeline.run_compliance_pipeline',cancel_then_run)
    result=run_once(task_id=task_id,task_types=[PREPARATION_TASK])
    db.expire_all()
    task=db.get(Task,task_id)
    runs=db.scalar(select(func.count()).select_from(ComplianceRun).where(ComplianceRun.opportunity_id==opp.id))
    evidence('preparation_late_commit_after_cancel',worker_result=result,task_status=task.status,
        cancel_http_status=cancel_status,committed_compliance_runs=runs,checkpoint=task.checkpoint)
    assert task.status=='cancelled' and runs>0

def test_preparation_rerun_erases_completed_review(opp,db,actor):
    from govcon.workflow.preparation import queue_preparation,PREPARATION_TASK,STEPS
    from govcon.tasks.worker import run_once
    from govcon.workflow.app_settings import set_setting,REVIEWER_ASSIGNMENT
    from govcon.collaboration.review_sessions import ensure_review_session
    reviewer,_=_make_user(db,f'reviewer-{uuid4().hex}@example.test','reviewer')
    with session_scope() as s:
        review=ensure_review_session(s,opportunity_id=opp.id)
        review.status='under_review'
        assignment=ReviewAssignment(opportunity_id=opp.id,user_id=reviewer.id,status='complete',completed_at=datetime.now(UTC))
        s.add(assignment); s.flush(); assignment_id=assignment.id
        set_setting(s,REVIEWER_ASSIGNMENT,{'mode':'named','user_ids':[reviewer.id]},actor=s.get(User,actor[0].id))
        task,_=queue_preparation(s,opportunity_id=opp.id,actor_user_id=actor[0].id)
        task.checkpoint={'completed_steps':list(STEPS[:-1])}; task_id=task.id
    result=run_once(task_id=task_id,task_types=[PREPARATION_TASK])
    db.expire_all(); assignment=db.get(ReviewAssignment,assignment_id)
    evidence('preparation_review_reset',worker_result=result,review_status=db.scalar(select(ReviewSession.status).where(ReviewSession.opportunity_id==opp.id)),assignment_status=assignment.status,completed_at=assignment.completed_at)
    assert assignment.status=='assigned' and assignment.completed_at is None

def test_reopened_review_never_escalates_again(opp,db,actor):
    from govcon.collaboration.review_sessions import ensure_review_session
    from govcon.collaboration.escalation import run_review_escalations
    from govcon.workflow.invalidation import reopen_review
    reviewer,_=_make_user(db,f'escalate-{uuid4().hex}@example.test','reviewer')
    now=datetime.now(UTC)
    with session_scope() as s:
        review=ensure_review_session(s,opportunity_id=opp.id); review.status='under_review'
        assignment=ReviewAssignment(opportunity_id=opp.id,user_id=reviewer.id,status='assigned',assigned_at=now-timedelta(days=10))
        s.add(assignment); s.flush(); assignment_id=assignment.id
        first=run_review_escalations(s,now=now)
        assignment.status='complete'; assignment.completed_at=now
        review.status='review_complete'
    with session_scope() as s:
        reopen_review(s,opp.id,reason='material amendment')
    with session_scope() as s:
        later=run_review_escalations(s,now=now+timedelta(days=5))
    db.expire_all(); assignment=db.get(ReviewAssignment,assignment_id)
    evidence('reopened_escalation',first_overdue=first.overdue,later_overdue=later.overdue,assignment_status=assignment.status,escalated_at=assignment.escalated_at,reopened_at=assignment.reopened_at)
    with session_scope() as s:
        notifications=list(s.scalars(select(Notification).where(Notification.opportunity_id==opp.id,Notification.notification_type=='review_overdue_escalation',Notification.user_id==actor[0].id)))
        evidence('reopened_escalation_actual_notifications', count=len(notifications), originally_escalated_at=assignment.escalated_at)
    assert len(notifications)==1



def test_read_only_actor_can_write_quotes_through_cli(opp,db,tmp_path):
    from govcon.cli import app
    from typer.testing import CliRunner
    reader,_=_make_user(db,f'readonly-{uuid4().hex}@example.test','read_only')
    path=tmp_path/'quote.csv'; path.write_bytes(b'description,quantity,unit_price\nGloves,500,2\n')
    result=CliRunner().invoke(app,['sourcing','add-quote','--opportunity-id',str(opp.id),
        '--supplier',f'CLI supplier {uuid4().hex}','--file',str(path),'--actor-email',reader.email])
    db.expire_all()
    quotes=list(db.scalars(select(SupplierQuote).where(SupplierQuote.opportunity_id==opp.id)))
    evidence('read_only_cli_quote_write', actor_role=reader.role, exit_code=result.exit_code,quote_count=len(quotes),
        entered_by_matches_reader=bool(quotes and quotes[0].entered_by_user_id==reader.id))
    assert result.exit_code==0 and len(quotes)==1 and quotes[0].entered_by_user_id==reader.id

def test_all_registered_cli_commands_have_working_help():
    import ast
    from govcon.cli import app
    from typer.testing import CliRunner
    tree=ast.parse((REPO/'src/govcon/cli.py').read_text('utf-8'))
    groups={}
    for node in ast.walk(tree):
        if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='add_typer' and ast.unparse(node.func.value)=='app':
            groups[ast.unparse(node.args[0])]=next(k.value.value for k in node.keywords if k.arg=='name')
    tested=[]; failed=[]
    for node in ast.walk(tree):
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                if isinstance(deco,ast.Call) and isinstance(deco.func,ast.Attribute) and deco.func.attr=='command':
                    group=ast.unparse(deco.func.value); command=deco.args[0].value
                    args=([groups[group]] if group!='app' else [])+[command,'--help']
                    result=CliRunner().invoke(app,args)
                    tested.append(' '.join(args[:-1]))
                    if result.exit_code!=0: failed.append({'command':args[:-1],'exit_code':result.exit_code,'error':str(result.exception)})
    evidence('all_cli_help',commands_checked=len(tested), failures=failed)
    assert len(tested)==96 and not failed

