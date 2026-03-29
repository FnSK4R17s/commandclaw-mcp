"""Credential zeroing utilities — ctypes.memset on bytearrays."""

from __future__ import annotations

import ctypes


def zero_bytes(data: bytearray) -> None:
    """Overwrite a bytearray with zeros using ctypes.memset.

    This provides best-effort credential zeroing. Python's GC may still
    hold copies, but explicit wipe is better than relying on collection.
    """
    if not data:
        return
    buf_addr = (ctypes.c_char * len(data)).from_buffer(data)
    ctypes.memset(buf_addr, 0, len(data))


class SecureBytes:
    """A bytearray wrapper that zeros its contents on deletion or context exit."""

    __slots__ = ("_data",)

    def __init__(self, data: bytes | bytearray) -> None:
        self._data = bytearray(data)

    @property
    def data(self) -> bytearray:
        return self._data

    def __bytes__(self) -> bytes:
        return bytes(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __enter__(self) -> SecureBytes:
        return self

    def __exit__(self, *_: object) -> None:
        self.clear()

    def clear(self) -> None:
        zero_bytes(self._data)

    def __del__(self) -> None:
        self.clear()
