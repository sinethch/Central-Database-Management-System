import logging
import re
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import db
from app.auth import initialize_users
from app.services.scheduler_service import scheduler_service
from app.api.v1 import (
    health,
    backups,
    metrics,
    diagnostics,
    alerts,
    databases,
    actions,
    notifications,
    restores,
    storage,
    servers,
    auth,
)

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("dbops.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure storage, attempt db connection, start scheduler
    settings.get_backup_path()
    initialize_users()
    logger.info(f"Starting {settings.APP_NAME} v{settings.APP_VERSION}")
    try:
        await db.init_pool()
    except Exception as e:
        logger.warning(f"Initial target DB connection attempt failed: {e}. Control plane will retry on demand.")

    scheduler_service.start()
    from app.services.notification_service import telegram_recipient_listener
    telegram_stop = asyncio.Event()
    telegram_task = asyncio.create_task(telegram_recipient_listener(telegram_stop))
    yield

    # Shutdown: clean up background scheduler and connection pools
    telegram_stop.set()
    telegram_task.cancel()
    try:
        await telegram_task
    except asyncio.CancelledError:
        pass
    scheduler_service.shutdown()
    await db.close_pool()
    logger.info(f"{settings.APP_NAME} stopped cleanly.")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "Multi-engine Database Operations & Health Management System (DBOps Control Plane). "
        "Provides engine-specific monitoring, PostgreSQL snapshots and restores, diagnostics, and alerting. "
        "Some explicit operations can change or interrupt database work."
    ),
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API v1 Routers
app.include_router(health.router, prefix="/api/v1")
app.include_router(backups.router, prefix="/api/v1")
app.include_router(metrics.router, prefix="/api/v1")
app.include_router(diagnostics.router, prefix="/api/v1")
app.include_router(alerts.router, prefix="/api/v1")
app.include_router(databases.router, prefix="/api/v1")
app.include_router(actions.router, prefix="/api/v1")
app.include_router(notifications.router, prefix="/api/v1")
app.include_router(restores.router, prefix="/api/v1")
app.include_router(storage.router, prefix="/api/v1")
app.include_router(servers.router, prefix="/api/v1")
app.include_router(auth.router, prefix="/api/v1")

# Mount Static Files and Web Control Plane UI
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


PAGE_CONFIG = {
    "console": ("Console", "Central operations, database targets, and registered servers."),
    "overview": ("Overview", "Health, performance, migration, and query activity for the active target selected in Console."),
    "backups": ("Backups", "Create, verify, download, and manage snapshots for the active target selected in Console."),
    "restore": ("Restore", "Restore a verified snapshot into a new or selected database."),
    "storage": ("Storage", "Review database size and monitored relations for the active target selected in Console."),
    "data-browser": ("Data Browser", "Read registered database tables and collections without modifying their contents."),
    "activity": ("Activity", "Review recent operator and system activity."),
    "restored-databases": ("Restored Databases", "Find restored database targets and copy their connection URLs."),
}
PAGE_PATHS = {"/": "console", **{f"/{name}": name for name in PAGE_CONFIG}}
SECTION_LINKS = {
    "#command-center": "/console",
    "#overview": "/overview",
    "#backups": "/backups",
    "#restores": "/restore",
    "#storage": "/storage",
    "#data-browser": "/data-browser",
    "#activity": "/activity",
    "#restored-databases": "/restored-databases",
}


def render_control_plane_page(page: str) -> str:
    """Render a full-navigation page with shared chrome and only its own content sections."""
    index_path = static_dir / "index.html"
    html = index_path.read_text(encoding="utf-8")
    title, description = PAGE_CONFIG[page]

    workspace_pattern = re.compile(
        r"<!--[ \t]*workspace:([a-z-]+):start[ \t]*-->(.*?)<!--[ \t]*workspace:\1:end[ \t]*-->",
        re.DOTALL,
    )
    html = workspace_pattern.sub(
        lambda match: match.group(2) if match.group(1) == page else "",
        html,
    )
    for old, new in SECTION_LINKS.items():
        html = html.replace(f'href="{old}"', f'href="{new}"')
    html = html.replace("<html lang=", f'<html data-page="{page}" lang=', 1)
    html = html.replace(
        "<title>DBOps Control Plane - Multi-Database Operations &amp; Health</title>",
        f"<title>{title} - DBOps Control Plane</title>",
        1,
    )
    if page not in ("console", "data-browser"):
        heading = (
            f'<section class="page-heading"><span>DBOPS WORKSPACE</span>'
            f'<h2>{title}</h2><p>{description}</p></section>\n    '
        )
        html = html.replace("  <main>\n", "  <main>\n    " + heading, 1)
    return html


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
@app.get("/console", response_class=HTMLResponse, include_in_schema=False)
@app.get("/overview", response_class=HTMLResponse, include_in_schema=False)
@app.get("/backups", response_class=HTMLResponse, include_in_schema=False)
@app.get("/restore", response_class=HTMLResponse, include_in_schema=False)
@app.get("/storage", response_class=HTMLResponse, include_in_schema=False)
@app.get("/data-browser", response_class=HTMLResponse, include_in_schema=False)
@app.get("/activity", response_class=HTMLResponse, include_in_schema=False)
@app.get("/restored-databases", response_class=HTMLResponse, include_in_schema=False)
async def serve_control_plane_ui(request: Request):
    """Serve dedicated full-navigation pages for the DBOps workspace."""
    page = PAGE_PATHS.get(request.url.path, "console")
    return HTMLResponse(render_control_plane_page(page))

