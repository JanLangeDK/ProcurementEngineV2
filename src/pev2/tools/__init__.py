"""The tool catalog of section 3, all mock adapters. Import this package once to register everything."""

from pev2.tools import brain, dms, email, jira, sap
from pev2.tools.base import Registry, ToolSpec, registry

for module in (sap, jira, email, brain, dms):
    module.register()

__all__ = ["Registry", "ToolSpec", "registry"]
