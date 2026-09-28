"""Minimal example plugins — the SDK's own documentation that runs.

Three plugins, each chosen because the core does NOT already do exactly this and
each one demonstrating a different part of the contract:

    SystemPlugin     declares nothing and needs nothing (LOW risk, no scopes)
    DeveloperPlugin  declares a scope it uses (filesystem:read, read-only)
    BrowserPlugin    contributes a judgement, not a second browser controller

They are examples, not built-ins: nothing registers them automatically, and the
tests and the ``phase10_sdk`` demo are what exercise them.
"""

from novacontrol.plugins.examples.browser_plugin import BrowserPlugin
from novacontrol.plugins.examples.developer_plugin import DeveloperPlugin
from novacontrol.plugins.examples.system_plugin import SystemPlugin

__all__ = ["BrowserPlugin", "DeveloperPlugin", "SystemPlugin"]
