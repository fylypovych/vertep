"""Health boundary of the Publisher Node runtime.

Publication runs only through the adapters in ``publishers/``. Channel readiness comes
from CORE (GET /api/tasks/publisher-readiness), which owns the credentials.
"""

from fastapi import FastAPI


app = FastAPI(title="Vertep Publisher", version="3")


@app.get("/health")
def health() -> dict:
    return {"status": "HEALTHY", "service": "publisher"}
