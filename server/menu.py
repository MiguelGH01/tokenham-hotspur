#
# Copyright (c) 2024-2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Runtime menu availability. Tool schemas are built from this, not from a static list."""

import os

ALL_PIZZA_SIZES = ["small", "medium", "large"]
ALL_PIZZA_TYPES = ["pepperoni", "cheese", "supreme", "vegetarian"]
ALL_ROLL_TYPES = ["california", "spicy tuna", "rainbow", "dragon"]


def _csv_set(env_name: str) -> set[str]:
    raw = os.getenv(env_name, "")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _truthy(env_name: str, default: str = "true") -> bool:
    return os.getenv(env_name, default).strip().lower() not in {"0", "false", "no"}


def current_menu() -> dict:
    """Compute which items the LLM may pass into tools for this session."""
    sold_out_pizzas = _csv_set("PIZZA_SOLD_OUT")
    sold_out_rolls = _csv_set("SUSHI_SOLD_OUT")
    pizza_available = _truthy("PIZZA_AVAILABLE")
    sushi_available = _truthy("SUSHI_AVAILABLE")

    pizza_types = [item for item in ALL_PIZZA_TYPES if item not in sold_out_pizzas]
    roll_types = [item for item in ALL_ROLL_TYPES if item not in sold_out_rolls]

    if not pizza_types:
        pizza_available = False
    if not roll_types:
        sushi_available = False

    return {
        "pizza_available": pizza_available,
        "sushi_available": sushi_available,
        "available_pizza_sizes": list(ALL_PIZZA_SIZES) if pizza_available else [],
        "available_pizza_types": pizza_types if pizza_available else [],
        "available_roll_types": roll_types if sushi_available else [],
    }
