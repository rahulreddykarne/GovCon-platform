"""Readiness probes; synthetic records in a fresh disposable database only."""
import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests"))
os.environ.update(SAM_API_KEY="", COMPANY_SAM_API_KEY="", ANTHROPIC_API_KEY="",
                  OPENAI_API_KEY="", DEEPSEEK_API_KEY="", JEV_API_KEY="",
                  SMTP_HOST="", EMAIL_NOTIFICATIONS_ENABLED="false")

import psycopg
from psycopg import sql
from sqlalchemy import select

from govcon.company.registration import overlay_registration
from govcon.compliance.deterministic import sam_registration_known
from govcon.config import Settings, get_settings
from govcon.decision.signals import Signals, eligibility_signals
from govcon.models import CompanyRegistration, Opportunity, Vendor, Watchlist

results = {}
now = datetime.now(UTC)
uei = "AUDIT0000001"
registration = CompanyRegistration(uei=uei, registration_status="Active", refreshed_at=now-timedelta(days=30))
vendor = Vendor(uei=uei, registration_status="Active", fetched_at=now-timedelta(days=30),
                raw={"entityRegistration": {"registrationExpirationDate": (now-timedelta(days=20)).date().isoformat()}})


class FakeSession:
    def get(self, model, key):
        return registration if model is CompanyRegistration else vendor if model is Vendor else None


settings = Settings(_env_file=None, company_uei=uei, sam_api_key=None, company_facts_max_age_days=7)
facts = overlay_registration(FakeSession(), {"uei": uei, "sam_registration_status": "Active"}, settings=settings, now=now)
signals = Signals()
eligibility_signals(FakeSession(), Opportunity(id=1, status="open", response_deadline=now+timedelta(days=10)),
                    facts, {}, has_summary=False, settings=settings, signals=signals)
results["stale_registration"] = {"overlay_removed_status": "sam_registration_status" not in facts,
    "decision_sam_active": signals.values["sam_active"], "decision_provenance": signals.provenance["sam_active"]}
registration.registration_status = None
registration.refreshed_at = now
registration.expiration_date = None
facts = overlay_registration(FakeSession(), {"uei": uei, "sam_registration_status": "Active", "sam_expiration_date": "2099-12-31"}, settings=settings, now=now)
results["unknown_fresh_registration"] = {"retained_status": facts.get("sam_registration_status"),
    "retained_expiration": facts.get("sam_expiration_date"),
    "validator_status": sam_registration_known(facts, now+timedelta(days=10)).status}

name = "govcon_module_probe_" + uuid4().hex
configured = "postgresql+psycopg://govcon:govcon@127.0.0.1:55432/" + name
with psycopg.connect("postgresql://govcon:govcon@127.0.0.1:55432/govcon", autocommit=True, connect_timeout=5) as admin:
    admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    try:
        with tempfile.TemporaryDirectory(prefix="govcon-module-probes-") as temporary:
            os.environ.update(DATABASE_URL=configured, DATA_DIR=str(Path(temporary)/"data"),
                              OUTBOX_DIR=str(Path(temporary)/"outbox"), PROMPT_REQUIRE_BEHAVIORAL_EVALUATION="false")
            get_settings.cache_clear()
            from alembic import command
            from govcon.cli import alembic_config
            from govcon.db import session_scope, dispose_engines
            from govcon.collaboration.users import invite_user, create_session
            from govcon.web.app import create_app
            from web_client import CsrfTestClient
            command.upgrade(alembic_config(), "head")
            import shutil
            from govcon.prompting.registry import load_prompt, sync_prompts
            from govcon.prompting.renderer import render_system_prompt
            prompt_copy = Path(temporary) / "prompts"
            shutil.copytree(ROOT / "src/govcon/prompts", prompt_copy)
            with session_scope() as db:
                sync_prompts(db, prompt_copy)
            with session_scope() as db:
                asset = load_prompt(db, "solicitation_analysis", prompt_root=prompt_copy)
                before = render_system_prompt(asset, prompt_copy)
            missing = prompt_copy / "shared/source_security_rules_v1.md"
            assert missing.resolve().is_relative_to(Path(temporary).resolve())
            missing.unlink()
            with session_scope() as db:
                asset = load_prompt(db, "solicitation_analysis", prompt_root=prompt_copy)
                after = render_system_prompt(asset, prompt_copy)
            results["missing_shared_prompt"] = {"load_succeeded": True, "render_succeeded": True,
                "system_prompt_shortened": len(after) < len(before), "removed_characters": len(before)-len(after)}
            with session_scope() as db:
                user = invite_user(db, email="audit@example.test", display_name="Synthetic auditor",
                                   password="synthetic audit password", role="owner")
                token = create_session(db, user)
            with CsrfTestClient(create_app(), raise_server_exceptions=False, follow_redirects=False) as client:
                client.cookies.set("govcon_session", token)
                for key, fields in [
                    ("invalid_money", {"min_value": "not-a-number"}),
                    ("inverted_range", {"min_value": "100", "max_value": "10"}),
                    ("invalid_deadline", {"min_deadline_days": "not-an-integer"}),
                ]:
                    response = client.post("/watchlists/new", data={"name": key, **fields})
                    with session_scope() as db:
                        row = db.scalar(select(Watchlist).where(Watchlist.name == key))
                        results[key] = {"http_status": response.status_code, "stored": row is not None,
                            "min_value": str(row.min_value) if row and row.min_value is not None else None,
                            "max_value": str(row.max_value) if row and row.max_value is not None else None}
            from concurrent.futures import ThreadPoolExecutor
            from threading import Barrier
            from unittest.mock import patch
            from govcon.models import Match, Pursuit
            from govcon.workflow.app_settings import AUTO_PURSUE, set_setting
            import govcon.matching.auto_pursue as auto
            with session_scope() as db:
                set_setting(db, AUTO_PURSUE, {"enabled": True, "min_score": 90, "min_days": 3, "max_per_day": 1}, actor=user)
                watchlist = Watchlist(name="concurrent synthetic auto pursuit", enabled=True, psc_codes=["6515"])
                db.add(watchlist)
                db.flush()
                ids = []
                for index in range(2):
                    opportunity = Opportunity(source="sam", source_id="audit-"+uuid4().hex, title="Synthetic cap probe",
                        status="open", raw={}, psc_code="6515", response_deadline=now+timedelta(days=30))
                    db.add(opportunity)
                    db.flush()
                    ids.append(opportunity.id)
                    db.add(Match(opportunity_id=opportunity.id, watchlist_id=watchlist.id, status="new", active=True,
                        rank_score=100, matched_on={"active_groups": ["psc"], "passing_groups": ["psc"]}, rank_factors=[
                            {"name": "rule_match", "weight": 30, "available": True, "raw": 1},
                            {"name": "value", "weight": 15, "available": True, "raw": 1},
                            {"name": "time_remaining", "weight": 10, "available": True, "raw": 1},
                            {"name": "semantic_similarity", "weight": 20, "available": True, "raw": 1},
                            {"name": "competition", "weight": 10, "available": True, "raw": 1},
                            {"name": "similar_to_wins", "weight": 15, "available": False, "raw": None},
                        ]))
            barrier = Barrier(2)
            real_count = auto.pursued_today

            def synchronized_count(db, clock):
                value = real_count(db, clock)
                barrier.wait(timeout=10)
                return value

            def run_auto():
                with session_scope() as db:
                    db.execute(__import__("sqlalchemy").text("SET LOCAL lock_timeout = '10s'"))
                    return auto.run_auto_pursue(db, now=now).pursued

            with patch.object(auto, "pursued_today", synchronized_count), ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(run_auto) for _ in range(2)]
                created = [future.result(timeout=25) for future in futures]
            with session_scope() as db:
                actual = list(db.scalars(select(Pursuit.opportunity_id).where(Pursuit.opportunity_id.in_(ids))))
            results["concurrent_auto_cap"] = {"max_per_day": 1, "runs_created": created, "actual_pursuits": len(actual)}
            # This is a service-level probe only. Both supported scheduler paths
            # use the same opportunity_ingest advisory lock, so there is no
            # confirmed product trigger for this interleaving.
            dispose_engines()
    finally:
        from govcon.db import dispose_engines
        dispose_engines()
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))

(Path(__file__).parent/"probe-results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
print(json.dumps(results, indent=2))
