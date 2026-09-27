"""Unit tests never write into the real app folders (logs, data)."""

import pytest


@pytest.fixture(autouse=True)
def _isolate_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
