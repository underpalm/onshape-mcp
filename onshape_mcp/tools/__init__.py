"""Extension tools added on top of the upstream server.

Each module exposes ``TOOLS`` (list of ``mcp.types.Tool``) and an async
``handle(name, arguments, ctx)`` returning a list of content items, or ``None``
when it does not own the tool. Keeping them here (instead of in server.py)
keeps upstream merges easy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

from mcp.types import TextContent, Tool


@dataclass
class ToolContext:
    """Shared managers handed to extension tool handlers."""

    client: Any
    document_manager: Any
    partstudio_manager: Any
    assembly_manager: Any
    transfer_manager: Any


def _modules():
    from . import (
        assembly_tools,
        copy_tools,
        drawing_tools,
        feature_tools,
        parts_tools,
        physical_tools,
        print_tools,
        sim_tools,
    )

    return [
        copy_tools,
        parts_tools,
        feature_tools,
        physical_tools,
        drawing_tools,
        print_tools,
        sim_tools,
        assembly_tools,
    ]


def extension_tools() -> List[Tool]:
    tools: List[Tool] = []
    for module in _modules():
        tools.extend(module.TOOLS)
    return tools


async def handle_extension_tool(name: str, arguments: dict, ctx: ToolContext) -> Optional[list]:
    for module in _modules():
        result = await module.handle(name, arguments, ctx)
        if result is not None:
            return result
    return None


def text(message: str) -> list:
    return [TextContent(type="text", text=message)]
