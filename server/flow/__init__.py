"""PR-01 receptionist graph.

Layers:
- ``prompts``: greeting + system identity
- ``tools``: directory, availability, confirm, submit (the model never invents ids)
- ``nodes``: which prompt and tools are active at each stage

``docs/agent/flow.yaml`` is the *intended* full graph; this package is what
``bot.py`` actually runs today (identify → slot → confirm → goodbye).
"""

from flow.nodes import create_identify_node
from flow.prompts import GREETING

__all__ = ["GREETING", "create_identify_node"]
