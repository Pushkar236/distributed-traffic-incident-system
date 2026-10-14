"""Shared exception type for FA-2 node commands."""


class CommandError(Exception):
    """Raised by a command when it cannot be carried out; reported back to the gateway."""
