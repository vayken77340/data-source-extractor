"""The README makes checkable claims, so they are checked.

Documentation that drifts is worse than none: it costs a reader time before it costs them
trust. These pin the handful of statements that go stale on their own.
"""

from __future__ import annotations

import re

import pytest

from api_extractor.cli import app
from api_extractor.config.validate import EXPECTED_CHECK_IDS
from api_extractor.providers import registry
from tests.conftest import REPO_ROOT

README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")


def test_the_check_count_is_right():
    stated = int(re.search(r"`validate` runs (\d+) checks", README).group(1))
    assert stated == len(EXPECTED_CHECK_IDS)


def test_every_command_is_documented():
    commands = {command.name or command.callback.__name__ for command in app.registered_commands}
    missing = [name for name in commands if f"api_extractor {name}" not in README]
    assert not missing, f"commands absent from the README: {missing}"


def test_every_builtin_provider_is_documented():
    names = {entry.name for entry in registry.registered()}
    assert not [name for name in names if name not in README]


def test_the_dependency_files_are_documented():
    assert "requirements.txt" in README
    assert "requirements-dev.txt" in README


@pytest.mark.parametrize("path", ["config/TEMPLATE.yaml", "providers/"])
def test_linked_paths_exist(path):
    assert path in README
    assert (REPO_ROOT / path).exists()
