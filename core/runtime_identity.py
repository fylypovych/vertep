"""Stable identity for the lifetime of the current CORE process."""

import uuid


CORE_RUNTIME_INSTANCE_ID = uuid.uuid4().hex
