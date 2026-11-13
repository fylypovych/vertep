"""Video engine backends behind the ``VideoEngine`` interface.

Phase 5 of the open-source audit formalizes the FFmpeg assembly pipeline as a
high-level ``VideoEngine`` driver and makes the external MoneyPrinterTurbo engine
(MIT reference) pluggable without Vertep giving up the Job lifecycle or character
metadata.

* ``NativeVertepEngine`` — the default, wraps the current FFmpeg assembly
  pipeline (via ``AssemblyProvider``). No external service required.
* ``MoneyPrinterEngine`` — the opt-in HTTP-backed engine that
  submit the asset spec to a remote render endpoint, poll for status and
  download the rendered video. Vertep never owns the external render logic
  (REUSE, not copy). They talk to ``httpx`` only through an injectable transport
  so tests drive the exact flow with ``FakeTransport``.

Modules: ``contract`` (pinned bridge profile), ``native``, ``remote`` (HTTP
flow), ``moneyprinter``, ``unsupported`` and ``_media`` (shared helpers). This
package re-exports the public names so ``adapters.providers.video_engines``
imports keep working.
"""

from __future__ import annotations

from ..base import (
    REASON_SUBMIT_UNKNOWN,
    REASON_TASK_ABSENT,
    REASON_UPSTREAM_UNREACHABLE,
    REASON_VOICE_STAGING_UNSUPPORTED,
    RELEASE_UNCONFIRMED,
    BridgeContractError,
)
from .contract import MONEY_PRINTER_CONTRACT, BridgeContractProfile
from .moneyprinter import MoneyPrinterEngine
from .native import NativeVertepEngine
from .remote import RemoteVideoEngine
from .unsupported import UnsupportedVideoEngine

__all__ = [
    "MONEY_PRINTER_CONTRACT",
    "REASON_SUBMIT_UNKNOWN",
    "REASON_TASK_ABSENT",
    "REASON_UPSTREAM_UNREACHABLE",
    "REASON_VOICE_STAGING_UNSUPPORTED",
    "RELEASE_UNCONFIRMED",
    "BridgeContractError",
    "BridgeContractProfile",
    "MoneyPrinterEngine",
    "NativeVertepEngine",
    "RemoteVideoEngine",
    "UnsupportedVideoEngine",
]
