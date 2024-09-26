"""Jaclang Runtimelib Implementation."""

from __future__ import annotations

from dataclasses import dataclass, field
from logging import getLogger
from re import IGNORECASE, compile
from typing import Type, TypeAlias, TypeVar
from uuid import UUID, uuid4

from .interface import (
    EdgeAnchor as _EdgeAnchor,
    EdgeArchitype as _EdgeArchitype,
    JID as _JID,
    NodeAnchor as _NodeAnchor,
    NodeArchitype as _NodeArchitype,
    Permission,
    WalkerAnchor as _WalkerAnchor,
    WalkerArchitype as _WalkerArchitype,
)


JID_REGEX = compile(
    r"^(n|e|w):([^:]*):([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    IGNORECASE,
)


_ANCHOR = TypeVar("_ANCHOR", "NodeAnchor", "EdgeAnchor", "WalkerAnchor", covariant=True)
Anchor: TypeAlias = "NodeAnchor" | "EdgeAnchor" | "WalkerAnchor"
Architype: TypeAlias = "NodeArchitype" | "EdgeArchitype" | "WalkerArchitype"
logger = getLogger(__name__)


@dataclass(kw_only=True)
class JID(_JID[UUID, Anchor]):
    """Jaclang Default JID."""

    id: UUID
    type: Type[Anchor]
    name: str

    def __init__(
        self,
        id: str | UUID | None = None,
        type: Type[_ANCHOR] | None = None,
        name: str = "",
    ) -> None:
        """Override JID initializer."""
        match id:
            case str():
                if matched := JID_REGEX.search(id):
                    self.id = UUID(matched.group(3))
                    self.name = matched.group(2)
                    match matched.group(1).lower():
                        case "n":
                            self.type = NodeAnchor
                        case "e":
                            self.type = EdgeAnchor
                        case _:
                            self.type = WalkerAnchor
                    return
                raise ValueError("Not a valid JID format!")
            case UUID():
                self.id = id
            case None:
                self.id = uuid4()
            case _:
                raise ValueError("Not a valid id for JID!")

        if type is None:
            raise ValueError("Type is required from non string JID!")
        self.type = type
        self.name = name

    def __repr__(self) -> str:
        """Override string representation."""
        return f"{self.type.__class__.__name__[:1].lower()}:{self.name}:{self.id}"

    def __str__(self) -> str:
        """Override string parsing."""
        return f"{self.type.__class__.__name__[:1].lower()}:{self.name}:{self.id}"


@dataclass(kw_only=True)
class NodeAnchor(_NodeAnchor["NodeAnchor"]):
    """NodeAnchor Interface."""

    jid: JID["NodeAnchor"] = field(default_factory=lambda: JID(type=NodeAnchor))
    architype: "NodeArchitype"
    root: JID["NodeAnchor"] | None = None
    access: Permission = field(default_factory=Permission)
    persistent: bool = False
    hash: int = 0

    edge_ids: set[JID[EdgeAnchor]] = field(default_factory=set)

    def __serialize__(self) -> NodeAnchor:
        """Override serialization."""
        return self

    @classmethod
    def __deserialize__(cls, data: NodeAnchor) -> NodeAnchor:
        """Override deserialization."""
        return data


aa = JID(id=UUID(), type=NodeAnchor)


@dataclass(kw_only=True)
class EdgeAnchor(_EdgeAnchor["EdgeAnchor"]):
    """NodeAnchor Interface."""

    jid: JID[EdgeAnchor] = field(default_factory=lambda: JID(type=EdgeAnchor))
    architype: "EdgeArchitype"
    root: JID["NodeAnchor"] | None = None
    access: Permission = field(default_factory=Permission)
    persistent: bool = False
    hash: int = 0

    source_id: JID[NodeAnchor]
    target_id: JID[NodeAnchor]

    def __serialize__(self) -> EdgeAnchor:
        """Override serialization."""
        return self

    @classmethod
    def __deserialize__(cls, data: EdgeAnchor) -> EdgeAnchor:
        """Override deserialization."""
        return data


@dataclass(kw_only=True)
class WalkerAnchor(_WalkerAnchor["WalkerAnchor"]):
    """NodeAnchor Interface."""

    jid: JID[WalkerAnchor] = field(default_factory=lambda: JID(type=WalkerAnchor))
    architype: "WalkerArchitype"
    root: JID["NodeAnchor"] | None = None
    access: Permission = field(default_factory=Permission)
    persistent: bool = False
    hash: int = 0

    def __serialize__(self) -> WalkerAnchor:
        """Override serialization."""
        return self

    @classmethod
    def __deserialize__(cls, data: WalkerAnchor) -> WalkerAnchor:
        """Override deserialization."""
        return data


class NodeArchitype(_NodeArchitype["NodeArchitype"]):
    """NodeArchitype Interface."""

    __jac__: NodeAnchor

    def __serialize__(self) -> NodeArchitype:
        """Override serialization."""
        return self

    @classmethod
    def __deserialize__(cls, data: NodeArchitype) -> NodeArchitype:
        """Override deserialization."""
        return data


class EdgeArchitype(_EdgeArchitype["EdgeArchitype"]):
    """EdgeArchitype Interface."""

    __jac__: EdgeAnchor

    def __serialize__(self) -> EdgeArchitype:
        """Override serialization."""
        return self

    @classmethod
    def __deserialize__(cls, data: EdgeArchitype) -> EdgeArchitype:
        """Override deserialization."""
        return data


class WalkerArchitype(_WalkerArchitype["WalkerArchitype"]):
    """Walker Architype Interface."""

    __jac__: WalkerAnchor

    def __serialize__(self) -> WalkerArchitype:
        """Override serialization."""
        return self

    @classmethod
    def __deserialize__(cls, data: WalkerArchitype) -> WalkerArchitype:
        """Override deserialization."""
        return data


@dataclass(kw_only=True)
class Root(NodeArchitype):
    """Default Root Architype."""


@dataclass(kw_only=True)
class GenericEdge(EdgeArchitype):
    """Default Edge Architype."""
