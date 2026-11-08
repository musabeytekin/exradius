"""A library for bridges between work trackers and Exradius (docs/design.md).

A bridge implements ``Tracker`` and feeds ``Kit`` what its tracker's webhooks say; the kit
owns the flow from hand-over to hand-back.
"""

from exradius_bridge_kit.exradius import Broker, ExradiusError, IntegrationApi
from exradius_bridge_kit.kit import Kit
from exradius_bridge_kit.projects import Problem
from exradius_bridge_kit.settings import KitSettings, SettingsError
from exradius_bridge_kit.tracker import (
    AttachmentInfo,
    Comment,
    Item,
    ItemGone,
    ItemRef,
    Markdown,
    Mention,
    Message,
    Paragraph,
    Person,
    Reply,
    Span,
    Tracker,
    TrackerProblem,
)

__all__ = [
    "AttachmentInfo",
    "Broker",
    "Comment",
    "ExradiusError",
    "IntegrationApi",
    "Item",
    "ItemGone",
    "ItemRef",
    "Kit",
    "KitSettings",
    "Markdown",
    "Mention",
    "Message",
    "Paragraph",
    "Person",
    "Problem",
    "Reply",
    "SettingsError",
    "Span",
    "Tracker",
    "TrackerProblem",
]
