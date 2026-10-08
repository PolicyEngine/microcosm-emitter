"""Explicit failures; callers decide whether their work can continue."""


class LocalServiceError(RuntimeError):
    """A local service could not start, accept a message, or shut down."""
