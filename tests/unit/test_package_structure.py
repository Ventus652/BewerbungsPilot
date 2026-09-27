from importlib import import_module

import bewerbungspilot


def test_package_version() -> None:
    assert bewerbungspilot.__version__ == "0.1.0"


def test_all_architecture_modules_import() -> None:
    modules = (
        "core",
        "llm",
        "domain",
        "memory",
        "jobs",
        "documents",
        "browser",
        "connectors",
        "persistence",
        "api",
    )
    for module in modules:
        assert import_module(f"bewerbungspilot.{module}") is not None
