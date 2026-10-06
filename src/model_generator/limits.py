"""Technical resource budgets, independent of regulatory limits."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    input_bytes: int = 1024**3
    expanded_bytes: int = 512 * 1024**2
    member_bytes: int = 256 * 1024**2
    entries: int = 1000
    nested_depth: int = 1
    fbx_depth: int = 64
    fbx_nodes: int = 100000
    fbx_properties: int = 1000
    fbx_array_bytes: int = 256 * 1024**2


class ReadError(ValueError):
    def __init__(self, rule, message):
        super().__init__(message)
        self.rule = rule
