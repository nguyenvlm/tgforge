"""The registry scans decorator markers into command tables and aborts the launch
on any name/id conflict (a duplicate universal/launch, a dup command under a class,
two classes with one id), and merges an `@extend` extension's commands."""

from __future__ import annotations

import pytest

from tgforge.base.kernel import (
    Plugin,
    RegistryConflict,
    Topic,
    action,
    build_registry,
    command,
    launch,
    on_message,
    prefix,
    universal,
)
from tgforge.plugins.agy import Agy
from tgforge.plugins.claude import Claude
from tgforge.testing import TestClient


@launch("/shell", "open a shell")
class ShellTopic(Topic):
    id = "shell"

    @command("/c", "ctrl-c")
    async def interrupt(self, ctx): ...

    @on_message
    async def feed(self, ctx): ...

    @action("sh")
    async def button(self, ctx, arg): ...


class Shell(Plugin):
    id = "shell"
    topics = [ShellTopic]

    @prefix("!", "one-shot")
    async def bang(self, ctx): ...


class Gcloud(Plugin):
    id = "gcloud"

    @universal("/gcloud", "sign in")
    async def gcloud(self, ctx): ...


def test_scans_tables():
    reg = build_registry([Shell(), Gcloud()])
    assert "/gcloud" in reg.universals
    assert reg.launches["/shell"][0] == "shell"
    assert reg.launch_class["/shell"] is ShellTopic
    assert "!" in reg.prefixes
    entry = reg.classes["shell"]
    assert entry.commands["/c"][0] == "interrupt"
    assert entry.on_message == "feed"
    assert entry.actions["sh"] == "button"


def test_duplicate_universal_aborts():
    class A(Plugin):
        id = "a"

        @universal("/x", "")
        async def x(self, ctx): ...

    class B(Plugin):
        id = "b"

        @universal("/x", "")
        async def x(self, ctx): ...

    with pytest.raises(RegistryConflict):
        build_registry([A(), B()])


def test_launch_shadowing_universal_aborts():
    @launch("/dup", "")
    class DupTopic(Topic):
        id = "dup"

    class P(Plugin):
        id = "p"
        topics = [DupTopic]

        @universal("/dup", "")
        async def dup(self, ctx): ...

    with pytest.raises(RegistryConflict):
        build_registry([P()])


def test_duplicate_class_id_aborts():
    class T1(Topic):
        id = "same"

    class T2(Topic):
        id = "same"

    class P(Plugin):
        id = "p"
        topics = [T1, T2]

    with pytest.raises(RegistryConflict):
        build_registry([P()])


def test_extend_merges_commands():
    @launch("/e", "")
    class ETopic(Topic):
        id = "etopic"

    @ETopic.extend
    class Recorder:
        @command("/rec", "record")
        async def rec(self, ctx): ...

    class P(Plugin):
        id = "p"
        topics = [ETopic]

    reg = build_registry([P()])
    assert reg.classes["etopic"].commands["/rec"][0] == "rec"


def test_each_window_reads_its_own_agent_in_the_command_help(tmp_path):
    c = TestClient(Claude(), Agy(), home=str(tmp_path))
    claude = c.core.registry.classes["claude"].commands
    agy = c.core.registry.classes["agy"].commands
    assert claude["/account"][1] == "switch this topic's Claude account"
    assert claude["/mode"][1] == "set the Claude permission mode (app-wide)"
    assert agy["/account"][1] == "switch this topic's agy account"
    assert agy["/mode"][1] == "set the agy permission mode (app-wide)"
    assert claude["/clear"][1] == agy["/clear"][1]  # a text with no field, unchanged


@launch("/braces", "a window whose help fills a field and quotes braces")
class _BracesTopic(Topic):
    id = "braces"
    help_fields = {"agent": "demo"}

    @command("/show", 'show {agent} as JSON, e.g. {"k": 1}')
    async def show(self, ctx):
        pass


class _Braces(Plugin):
    id = "braces"
    topics = [_BracesTopic]


def test_a_help_text_with_literal_braces_fills_only_its_fields(tmp_path):
    c = TestClient(_Braces(), home=str(tmp_path))
    assert c.core.registry.classes["braces"].commands["/show"][1] == (
        'show demo as JSON, e.g. {"k": 1}'
    )
