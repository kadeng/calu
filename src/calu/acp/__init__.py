"""`calu acp`: serve a running calu session to any editor that speaks the Agent Client Protocol.

`calu.acp.messages` holds the protocol's shapes, `calu.acp.peer` the JSON-RPC framing over
newline-delimited JSON, `calu.acp.adapter` the translation between ACP and calu's own protocol,
and `calu.acp.serve` the entry point the CLI calls.
"""

from calu.acp.adapter import Adapter
from calu.acp.peer import Peer, RpcError
from calu.acp.serve import serve_acp

__all__ = ["Adapter", "Peer", "RpcError", "serve_acp"]
