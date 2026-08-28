"""Fail-closed Windows machine-scope secret protection."""

from __future__ import annotations

import base64
import ctypes
import os
from ctypes import wintypes
from typing import Protocol


class SecretProtector(Protocol):
    def protect(self, plaintext: bytes) -> bytes: ...

    def unprotect(self, ciphertext: bytes) -> bytes: ...


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data)
    return (
        _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))),
        buffer,
    )


class WindowsMachineDPAPI:
    """Protect bytes with DPAPI's current-machine scope."""

    _CRYPTPROTECT_LOCAL_MACHINE = 0x4
    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows machine DPAPI is available only on Windows")
        self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def protect(self, plaintext: bytes) -> bytes:
        source, source_buffer = _blob(plaintext)
        output = _DataBlob()
        success = self._crypt32.CryptProtectData(
            ctypes.byref(source),
            None,
            None,
            None,
            None,
            self._CRYPTPROTECT_LOCAL_MACHINE | self._CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output),
        )
        del source_buffer
        if not success:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            self._kernel32.LocalFree(output.pbData)

    def unprotect(self, ciphertext: bytes) -> bytes:
        source, source_buffer = _blob(ciphertext)
        output = _DataBlob()
        success = self._crypt32.CryptUnprotectData(
            ctypes.byref(source),
            None,
            None,
            None,
            None,
            self._CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output),
        )
        del source_buffer
        if not success:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            self._kernel32.LocalFree(output.pbData)


class MachineSecretStore:
    """Encode application secrets using DPAPI or an explicitly injected test protector."""

    def __init__(self, protector: SecretProtector | None = None) -> None:
        if protector is None:
            if os.name != "nt":
                raise RuntimeError(
                    "non-Windows use requires an explicit secret protector"
                )
            protector = WindowsMachineDPAPI()
        self._protector = protector

    def encrypt(self, plaintext: str) -> str:
        protected = self._protector.protect(plaintext.encode("utf-8"))
        return base64.urlsafe_b64encode(protected).decode("ascii")

    def decrypt(self, encoded_ciphertext: str) -> str:
        protected = base64.urlsafe_b64decode(encoded_ciphertext.encode("ascii"))
        return self._protector.unprotect(protected).decode("utf-8")
