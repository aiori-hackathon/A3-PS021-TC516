"""Versioned experimental wrapper shared by the planner and tool adapters."""

from .adapters import ADAPTERS, from_wire, to_wire
from .effect_service import KEY
from .events import validate


SCHEMA_VERSION = "threadline-order-workflow-v1"


def encode_task(task_id, operation_key, envelope):
    return {"schema": SCHEMA_VERSION, "task_id": task_id, "operation_key": operation_key,
            "wire": to_wire(envelope)}


def decode_task(frame, adapter_name):
    """Decode a task frame without losing the IDs outside its native message."""
    if not isinstance(frame, dict) or frame.get("schema") != SCHEMA_VERSION:
        raise ValueError("unsupported task schema")
    task_id, key, wire = frame.get("task_id"), frame.get("operation_key"), frame.get("wire")
    if (not isinstance(task_id, str) or not task_id
            or not isinstance(key, str) or not KEY.fullmatch(key)
            or not isinstance(wire, dict) or wire.get("protocol") != ADAPTERS[adapter_name].protocol):
        raise ValueError("invalid task frame")
    try:
        envelope = from_wire(adapter_name, wire)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid adapter payload") from exc
    if validate(envelope) or not isinstance(envelope["conversation_id"], str):
        raise ValueError("invalid adapter envelope")
    return task_id, key, envelope
