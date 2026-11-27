"""Secrets hygiene + test reporting: local config and outputs are never committed; every run leaves a JUnit report."""

from __future__ import annotations

import subprocess

import pytest

from app.config import PROJECT_ROOT


@pytest.mark.parametrize("path", [".env", "logs/x.log", "outputs/x", "reports/junit-1.xml",
                                  "config/tools.yaml", "config/models.yaml"])
def test_local_files_are_ignored(path: str) -> None:
    res = subprocess.run(["git", "check-ignore", "-q", path], cwd=PROJECT_ROOT)
    assert res.returncode == 0, f"{path} is not git-ignored"


@pytest.mark.parametrize("path", [".env.example", "config/models.example.yaml", "config/tools.example.yaml"])
def test_examples_stay_committable(path: str) -> None:
    res = subprocess.run(["git", "check-ignore", "-q", path], cwd=PROJECT_ROOT)
    assert res.returncode == 1, f"{path} must not be ignored"


def test_every_run_writes_a_junit_report(pytestconfig: pytest.Config) -> None:
    xml = pytestconfig.option.xmlpath
    assert xml and "reports" in xml.replace("\\", "/") and xml.endswith(".xml")
