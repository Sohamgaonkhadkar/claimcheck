"""Safe application failures with an explicit, transport-neutral public projection."""
from __future__ import annotations


class ApplicationError(Exception):
    """An expected failure; only these allow-listed fields cross the HTTP boundary."""

    def __init__(self, status: int, code: str, title: str, detail: str,
                 *, retryable: bool = False) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.retryable = retryable
