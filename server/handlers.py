#
# Copyright (c) 2024-2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Food-ordering flow in Python so tool inputs can be restricted at runtime.

Each order tool is advertised as a FlowsFunctionSchema whose JSON-schema
``enum`` is built from ``flow_manager.state`` (see ``menu.current_menu``).
The same handler also rejects values that are not on that list — the schema
guides the LLM; the handler is the actual gate.
"""

from datetime import datetime, timedelta
from typing import TypedDict

from loguru import logger
from pipecat.flows import FlowArgs, FlowManager, FlowsFunctionSchema, NodeConfig


class PizzaOrderResult(TypedDict):
    size: str
    type: str
    price: float


class SushiOrderResult(TypedDict):
    count: int
    type: str
    price: float


class DeliveryEstimateResult(TypedDict):
    time: str


async def check_kitchen_status(action: dict, flow_manager: FlowManager) -> None:
    """Check if kitchen is open and log status."""
    logger.info(
        "Kitchen status pizza={} sushi={}",
        flow_manager.state.get("pizza_available"),
        flow_manager.state.get("sushi_available"),
    )


async def choose_pizza(flow_manager: FlowManager) -> tuple[None, NodeConfig]:
    """The caller wants to order pizza."""
    return None, create_pizza_node(flow_manager)


async def choose_sushi(flow_manager: FlowManager) -> tuple[None, NodeConfig]:
    """The caller wants to order sushi."""
    return None, create_sushi_node(flow_manager)


async def select_pizza_order(args: FlowArgs, flow_manager: FlowManager):
    """Record the pizza order if size and type are currently available."""
    allowed_sizes = flow_manager.state.get("available_pizza_sizes", [])
    allowed_types = flow_manager.state.get("available_pizza_types", [])
    size = args.get("size")
    pizza_type = args.get("pizza_type")

    if size not in allowed_sizes or pizza_type not in allowed_types:
        return {
            "status": "invalid",
            "allowed_sizes": allowed_sizes,
            "allowed_types": allowed_types,
        }, None

    price = {"small": 10.00, "medium": 15.00, "large": 20.00}[size]
    flow_manager.state["order"] = {
        "type": "pizza",
        "size": size,
        "pizza_type": pizza_type,
        "price": price,
    }
    return PizzaOrderResult(size=size, type=pizza_type, price=price), create_confirmation_node()


async def select_sushi_order(args: FlowArgs, flow_manager: FlowManager):
    """Record the sushi order if count and roll type are currently available."""
    allowed_rolls = flow_manager.state.get("available_roll_types", [])
    count = args.get("count")
    roll_type = args.get("roll_type")

    if not isinstance(count, int) or count < 1 or count > 10 or roll_type not in allowed_rolls:
        return {
            "status": "invalid",
            "allowed_roll_types": allowed_rolls,
            "count_range": [1, 10],
        }, None

    price = count * 8.00
    flow_manager.state["order"] = {
        "type": "sushi",
        "count": count,
        "roll_type": roll_type,
        "price": price,
    }
    return SushiOrderResult(count=count, type=roll_type, price=price), create_confirmation_node()


async def complete_order(flow_manager: FlowManager) -> tuple[None, NodeConfig]:
    """The caller confirms the order is correct."""
    return None, create_end_node()


async def revise_order(flow_manager: FlowManager) -> tuple[None, NodeConfig]:
    """The caller wants to make changes to their order."""
    return None, create_initial_node(flow_manager)


async def get_delivery_estimate(flow_manager: FlowManager):
    """Provide delivery estimate information."""
    delivery_time = datetime.now() + timedelta(minutes=30)
    return DeliveryEstimateResult(time=f"{delivery_time}"), None


def _select_pizza_schema(flow_manager: FlowManager) -> FlowsFunctionSchema:
    allowed_sizes = flow_manager.state.get("available_pizza_sizes", [])
    allowed_types = flow_manager.state.get("available_pizza_types", [])
    return FlowsFunctionSchema(
        name="select_pizza_order",
        description="Record the pizza size and type. Only use currently available values.",
        properties={
            "size": {
                "type": "string",
                "enum": allowed_sizes,
                "description": "Pizza size.",
            },
            "pizza_type": {
                "type": "string",
                "enum": allowed_types,
                "description": "Pizza type currently in stock.",
            },
        },
        required=["size", "pizza_type"],
        handler=select_pizza_order,
    )


def _select_sushi_schema(flow_manager: FlowManager) -> FlowsFunctionSchema:
    allowed_rolls = flow_manager.state.get("available_roll_types", [])
    return FlowsFunctionSchema(
        name="select_sushi_order",
        description="Record the sushi roll count and type. Only use currently available rolls.",
        properties={
            "count": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": "Number of rolls, from 1 to 10.",
            },
            "roll_type": {
                "type": "string",
                "enum": allowed_rolls,
                "description": "Sushi roll type currently in stock.",
            },
        },
        required=["count", "roll_type"],
        handler=select_sushi_order,
    )


def create_initial_node(flow_manager: FlowManager) -> NodeConfig:
    """Create the initial node; only advertise food types the kitchen can make."""
    functions = []
    if flow_manager.state.get("pizza_available"):
        functions.append(choose_pizza)
    if flow_manager.state.get("sushi_available"):
        functions.append(choose_sushi)

    options = []
    if flow_manager.state.get("pizza_available"):
        options.append("pizza")
    if flow_manager.state.get("sushi_available"):
        options.append("sushi")
    choice = " or ".join(options) if options else "nothing — the kitchen is closed"

    return NodeConfig(
        name="initial",
        role_message=(
            "You are an order-taking assistant for {{ restaurant_name }}. You must "
            "ALWAYS use the available functions to progress the conversation. This is "
            "a phone conversation and your responses will be converted to audio. Keep "
            "the conversation friendly, casual, and polite. Keep every reply to one "
            "or two short sentences. Only give a delivery time that came from "
            "get_delivery_estimate. Avoid outputting special characters and emojis."
        ),
        task_messages=[
            {
                "role": "developer",
                "content": (
                    f"Greet the caller briefly and ask whether they'd like {choice}, "
                    "then wait for them to use a function to choose. Do not offer an "
                    "item that has no matching function."
                ),
            }
        ],
        pre_actions=[{"type": "function", "handler": check_kitchen_status}],
        functions=functions,
    )


def create_pizza_node(flow_manager: FlowManager) -> NodeConfig:
    allowed = ", ".join(flow_manager.state.get("available_pizza_types", []))
    return NodeConfig(
        name="choose_pizza",
        task_messages=[
            {
                "role": "developer",
                "content": (
                    "Your only job on this step is to get the pizza's size and type and "
                    "call select_pizza_order with them. If the caller has already given "
                    "both, call it immediately; if one is missing, ask for it in one "
                    "short sentence. Available types: "
                    f"{allowed}. If they ask for something not on that list, say it is "
                    "not available and offer those types. Don't quote a price or say "
                    "the order is in.\n\n"
                    "Pricing, for questions only:\n- Small: $10\n- Medium: $15\n- Large: $20"
                ),
            }
        ],
        functions=[_select_pizza_schema(flow_manager)],
    )


def create_sushi_node(flow_manager: FlowManager) -> NodeConfig:
    allowed = ", ".join(flow_manager.state.get("available_roll_types", []))
    return NodeConfig(
        name="choose_sushi",
        task_messages=[
            {
                "role": "developer",
                "content": (
                    "Your only job on this step is to get the roll count and type and "
                    "call select_sushi_order with them. If the caller has already given "
                    "both, call it immediately; if one is missing, ask for it in one "
                    "short sentence. Available rolls: "
                    f"{allowed}. If they ask for something not on that list, say it is "
                    "not available and offer those rolls. Don't quote a price or say "
                    "the order is in.\n\n"
                    "Pricing, for questions only:\n- $8 per roll"
                ),
            }
        ],
        functions=[_select_sushi_schema(flow_manager)],
    )


def create_confirmation_node() -> NodeConfig:
    return NodeConfig(
        name="confirm",
        task_messages=[
            {
                "role": "developer",
                "content": (
                    'Say the order back once with the total and ask if that\'s right, for '
                    'example "So that\'s one large pepperoni, twenty dollars. Sound good?" '
                    "Nothing is ordered until the caller confirms. Use complete_order when "
                    "they confirm, or revise_order if they want to change something."
                ),
            }
        ],
        functions=[complete_order, revise_order],
    )


def create_end_node() -> NodeConfig:
    return NodeConfig(
        name="end",
        task_messages=[
            {
                "role": "developer",
                "content": "Thank the caller for the order and say goodbye, in one or two sentences.",
            }
        ],
        post_actions=[{"type": "end_conversation"}],
    )
