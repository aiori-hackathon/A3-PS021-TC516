"""Simulated agent adapters, each speaking a different wire protocol.

The point of PS-021 is that one dialog crosses adapters that do not share a
message format. Each adapter here has a native payload shape; the gateway
decodes it into the canonical envelope (``threadline.events``). Correlation
metadata survives the switch because every protocol carries the same five
fields: conversation_id, message_id, sequence, causation_id, idempotency_key.
"""


class Adapter:
    name = ""
    protocol = ""

    def encode(self, env):
        raise NotImplementedError

    def decode(self, native):
        raise NotImplementedError


class WebChatAdapter(Adapter):
    """Browser chat widget over a WebSocket-style JSON frame."""

    name = "web-chat"
    protocol = "websocket-json"

    def encode(self, env):
        return {
            "type": "chat.message",
            "session": env["conversation_id"],
            "idem": env["idempotency_key"],
            "msg": {
                "id": env["message_id"],
                "seq": env["sequence"],
                "reply_to": env["causation_id"],
                "from": env["role"],
                "text": env["content"],
                "ts": env.get("sent_at"),
            },
        }

    def decode(self, native):
        msg = native["msg"]
        return {
            "conversation_id": native["session"],
            "message_id": msg["id"],
            "sequence": msg["seq"],
            "causation_id": msg.get("reply_to"),
            "idempotency_key": native.get("idem"),
            "adapter": self.name,
            "role": msg["from"],
            "kind": "message",
            "content": msg["text"],
            "sent_at": msg.get("ts"),
        }


class PlannerAdapter(Adapter):
    """LLM planner agent over HTTP; correlation travels in headers."""

    name = "planner-agent"
    protocol = "http-agent"

    def encode(self, env):
        body = {"role": env["role"], "output": env["content"]}
        if env["kind"] == "handoff":
            body["handoff_to"] = env["target_adapter"]
        return {
            "headers": {
                "X-Conversation-Id": env["conversation_id"],
                "X-Message-Id": env["message_id"],
                "X-Sequence": str(env["sequence"]),
                "X-Causation-Id": env["causation_id"] or "",
                "Idempotency-Key": env["idempotency_key"],
                "Date": env.get("sent_at"),
            },
            "body": body,
        }

    def decode(self, native):
        h, body = native["headers"], native["body"]
        handoff = body.get("handoff_to")
        return {
            "conversation_id": h["X-Conversation-Id"],
            "message_id": h["X-Message-Id"],
            "sequence": int(h["X-Sequence"]),
            "causation_id": h.get("X-Causation-Id") or None,
            "idempotency_key": h.get("Idempotency-Key"),
            "adapter": self.name,
            "role": body["role"],
            "kind": "handoff" if handoff else "message",
            "target_adapter": handoff,
            "content": body["output"],
            "sent_at": h.get("Date"),
        }


class ToolAdapter(Adapter):
    """Tool-execution agent speaking JSON-RPC 2.0."""

    name = "tool-agent"
    protocol = "json-rpc-2.0"

    def encode(self, env):
        return {
            "jsonrpc": "2.0",
            "method": "tool.result",
            "id": env["message_id"],
            "params": {
                "conversation": env["conversation_id"],
                "seq": env["sequence"],
                "cause": env["causation_id"],
                "idempotency_key": env["idempotency_key"],
                "role": env["role"],
                "result": env["content"],
                "sent_at": env.get("sent_at"),
            },
        }

    def decode(self, native):
        p = native["params"]
        return {
            "conversation_id": p["conversation"],
            "message_id": native["id"],
            "sequence": p["seq"],
            "causation_id": p.get("cause"),
            "idempotency_key": p.get("idempotency_key"),
            "adapter": self.name,
            "role": p.get("role", "tool"),
            "kind": "message",
            "content": p["result"],
            "sent_at": p.get("sent_at"),
        }


ADAPTERS = {a.name: a for a in (WebChatAdapter(), PlannerAdapter(), ToolAdapter())}


def to_wire(env):
    """Encode a canonical envelope into its adapter's native payload."""
    adapter = ADAPTERS[env["adapter"]]
    return {"protocol": adapter.protocol, "payload": adapter.encode(env)}


def from_wire(adapter_name, native):
    """Decode a native payload back into a canonical envelope."""
    env = ADAPTERS[adapter_name].decode(native["payload"])
    env["native"] = native
    return env
