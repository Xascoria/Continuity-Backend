from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.staticfiles import StaticFiles

from .auth import require_admin_browser
from .config import FRONTEND_DIR
from .database import Base, engine
from .migrations import run_migrations
from .request_log import RequestLogMiddleware
from .routers import admin, user

Base.metadata.create_all(bind=engine)
run_migrations(engine)  # add columns introduced after a table was first created

# The built-in docs routes are public, so they are disabled here and re-added
# below behind admin auth.
app = FastAPI(
    title="20Seconds API", docs_url=None, redoc_url=None, openapi_url=None
)
app.add_middleware(RequestLogMiddleware)
# The itch.io web build calls this API from another origin. application/json
# makes the browser preflight with OPTIONS, which must succeed before the POST.
# Added last so it runs first and answers the preflight itself.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(require_admin_browser)])
def openapi_schema():
    return app.openapi()


@app.get("/docs", include_in_schema=False, dependencies=[Depends(require_admin_browser)])
def swagger_ui():
    return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} - docs")


@app.get("/redoc", include_in_schema=False, dependencies=[Depends(require_admin_browser)])
def redoc_ui():
    return get_redoc_html(openapi_url="/openapi.json", title=f"{app.title} - redoc")



# Every /api route requires auth, except POST /api/user/register.
app.include_router(user.router)
app.include_router(admin.router)

# Serve the static frontend (admin dashboard) at "/". Must be mounted last so
# /api routes win. The files are public; the data behind them is not.
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
