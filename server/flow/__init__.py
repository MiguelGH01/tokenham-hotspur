"""Receptionist graph: identify → act → close. Tools own clinic branches."""

from flow.nodes import create_identify_node
from flow.prompts import GREETING
from flow.tools import RAILS

__all__ = ["GREETING", "RAILS", "create_identify_node"]
