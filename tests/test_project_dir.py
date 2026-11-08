"""`_project_dir` names a cwd's transcript dir the way the Claude CLI does. Each row's
expected name is the dir the CLI itself created for that cwd."""

from __future__ import annotations

from pathlib import Path

import pytest

from tgforge.plugins.claude.driver import _project_dir

BASE = "/tmp/mybot/name_probe"  # 21 chars
BASE_NAME = "-tmp-mybot-name-probe"


@pytest.mark.parametrize(
    ("workspace", "name"),
    [
        (f"{BASE}/a_b.c d+é", f"{BASE_NAME}-a-b-c-d--"),  # every non-ASCII-alnum unit
        (f"{BASE}/e😀z", f"{BASE_NAME}-e--z"),  # an emoji is two UTF-16 units
        (f"{BASE}/{'y' * 178}", f"{BASE_NAME}-{'y' * 178}"),  # 200 chars: kept whole
        (f"{BASE}/{'y' * 179}", f"{BASE_NAME}-{'y' * 178}-sb5ejl"),  # 201: cut + hash
        (f"{BASE}/{'x' * 120}/{'x' * 120}", f"{BASE_NAME}-{'x' * 120}-{'x' * 57}-10k5l5"),
    ],
)
def test_project_dir_matches_the_cli(workspace, name):
    assert _project_dir(Path("/cfg"), Path(workspace)) == Path("/cfg/projects") / name
