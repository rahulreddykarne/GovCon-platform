"""Build and install a wheel, then start it outside the checkout."""

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
from conftest import NO_DB
from psycopg import sql
from sqlalchemy.engine import make_url

from govcon.paths import repo_root


def test_clean_installed_wheel_startup_and_database_upgrade(tmp_path, database_url):
    wheel_dir = tmp_path / "wheels"
    installed = tmp_path / "installed"
    subprocess.run([sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--no-build-isolation",
                    "--wheel-dir", str(wheel_dir)], cwd=repo_root(), check=True, capture_output=True, text=True)
    wheel = next(wheel_dir.glob("govcon-*.whl"))
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(installed), str(wheel)],
                   check=True, capture_output=True, text=True)
    script = r'''
import sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
import govcon
assert Path(govcon.__file__).resolve().is_relative_to(root)
from fastapi.testclient import TestClient
from govcon.config import Settings
from govcon.web.app import create_app
from govcon.cli import alembic_config
from govcon.paths import migration_root
from alembic.script import ScriptDirectory
settings = Settings(_env_file=None)
assert settings.resolved_prompt_root().is_relative_to(root)
assert migration_root().is_relative_to(root)
assert ScriptDirectory.from_config(alembic_config()).get_current_head()
with TestClient(create_app(settings)) as client:
    assert client.get('/login').status_code == 200
    assert client.get('/static/govcon.css').status_code == 200
from govcon.compliance.regression import default_fixture_root
assert (default_fixture_root() / 'baseline_metrics.json').is_file()
if sys.argv[2] == 'database':
    from alembic import command
    command.upgrade(alembic_config(), 'head')
    from govcon.db import session_scope
    from govcon.prompting.registry import sync_prompts, load_prompt
    with session_scope(settings) as session:
        synced = sync_prompts(session, settings.resolved_prompt_root())
        assert 'solicitation_analysis' in synced
        assert load_prompt(session, 'solicitation_analysis', prompt_root=settings.resolved_prompt_root()).name == 'solicitation_analysis'
    # Load the acceptance harness by filename while all govcon imports remain
    # bound to the installed wheel outside the checkout.
    import importlib.util
    spec = importlib.util.spec_from_file_location('release_acceptance', sys.argv[3])
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    from govcon.db import make_engine
    engine = make_engine(settings)
    try:
        harness.run_release_lifecycle(engine, Path.cwd() / 'release_artifacts')
    finally:
        engine.dispose()
print('Installed wheel startup and bootstrap passed')
'''
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("GOVCON_ALEMBIC_URL", None)
    mode = "startup" if NO_DB else "database"
    admin = None
    database_name = "govcon_wheel_smoke_" + uuid4().hex[:12]
    try:
        if not NO_DB:
            url = make_url(database_url)
            admin = psycopg.connect(url.set(drivername="postgresql").render_as_string(hide_password=False),
                                    autocommit=True, connect_timeout=5)
            admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database_name)))
            env["DATABASE_URL"] = url.set(database=database_name).render_as_string(hide_password=False)
        result = subprocess.run([sys.executable, "-I", "-c", script, str(installed), mode, str(Path(__file__).with_name("test_release_lifecycle.py"))],
                                cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        if admin is not None:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database_name)))
            admin.close()
