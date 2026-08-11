"""Errors raised for invalid lifecycle hook protocol behavior."""


class HookProtocolError(ValueError):
    """A hook handler returned a result that violates the protocol."""
