"""Vertep CORE composition root: wire owners, middleware, routers and mounts."""
from fastapi import FastAPI
from .version import application_version
from .lifecycle import lifespan
from .api.auth import AdminAuthMiddleware
from .web_ui import router as web_router, mount_web_ui
from .telegram import handlers
from . import telegram_pump
from .api.workflows import router as workflows_router
from .api.resources import router as resources_router
from .api.settings import router as settings_router
from .api.models import router as models_router
from .api.setup import router as setup_router
from .api.jobs import router as jobs_router
from .api.tasks import router as tasks_router
from .api.workers import router as workers_router
from .api.nodes import router as nodes_router
from .api.observability import router as observability_router
from .api.storyboards import router as storyboards_router
from .api.real_tests import router as real_tests_router
from .api.system import router as system_router
from .api.backups import router as backups_router
from .api.operations import router as operations_router
from .api.certificates import router as certificates_router

from .api.auth import router as auth_router
from .api.events import router as events_router
from .api.telegram import router as telegram_router
from .api.status import router as status_router

app = FastAPI(title="Vertep CORE", version=application_version(), lifespan=lifespan)
app.add_middleware(AdminAuthMiddleware)
app.include_router(workflows_router)
app.include_router(resources_router)
app.include_router(settings_router)
app.include_router(models_router)
app.include_router(setup_router)
app.include_router(jobs_router)
app.include_router(tasks_router)
app.include_router(workers_router)
app.include_router(nodes_router)
app.include_router(observability_router)
app.include_router(storyboards_router)
app.include_router(real_tests_router)
app.include_router(system_router)
app.include_router(backups_router)
app.include_router(operations_router)
app.include_router(certificates_router)
app.include_router(web_router)
app.include_router(auth_router)
app.include_router(events_router)
app.include_router(telegram_router)
app.include_router(status_router)
telegram_pump.register_restart(handlers._restart_telegram_polling)
telegram_pump.register_service(lambda: handlers.telegram_polling_service)
mount_web_ui(app)
