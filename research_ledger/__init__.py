"""research-ledger: an append-only log of results and decisions, and preregistrations that freeze code and data and
let the outcome be read once."""
from .log import correct, find, record, rows
from .prereg import GateRefused, freeze, gate, note, replace, verify, veto

__version__ = "1.0.0"
__all__ = ["record", "correct", "find", "rows", "freeze", "verify", "gate", "veto", "replace", "note", "GateRefused"]
