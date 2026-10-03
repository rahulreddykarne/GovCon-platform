"""Include checkout-owned deployment resources in wheels and sdists."""

import shutil
from pathlib import Path

from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def run(self):
        # Never reuse a checkout build/lib snapshot from an earlier audit/build.
        self.force = True
        super().run()
        root = Path(__file__).resolve().parents[2]
        package = Path(self.build_lib) / "govcon"
        migrations = package / "migrations"
        # Removed/renamed migrations must disappear from subsequent wheels.
        # Refuse an output path overlapping the source package before deleting
        # these build-owned resource directories (including symlink targets).
        source_package = root / "src" / "govcon"
        for target in (migrations, package / "compliance" / "benchmark"):
            resolved = target.resolve()
            if target.is_symlink() or resolved.is_relative_to(source_package.resolve()) or not resolved.is_relative_to(package.resolve()):
                raise RuntimeError("build resources must be inside a separate build output directory")
            if target.exists():
                shutil.rmtree(target)
        shutil.copytree(root / "alembic", migrations, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copyfile(root / "alembic.ini", migrations / "alembic.ini")
        shutil.copytree(root / "tests" / "fixtures" / "compliance", package / "compliance" / "benchmark",
                        dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
