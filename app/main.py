from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn

from backend.app.config import settings
from backend.app.api.v1.endpoints import router as api_v1_router
from backend.app.core.exceptions import AppBaseException


app = FastAPI(
    title=settings.APP_NAME,
    description="High-performance asynchronous Bilibili video, audio, and danmaku extraction engine.",
    version="1.0.0",
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "HEAD", "OPTIONS"],
    allow_headers=["*"],
)


# Global Exception Handlers
@app.exception_handler(AppBaseException)
async def custom_app_exception_handler(request: Request, exc: AppBaseException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "error": exc.detail},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": "Internal server error occurred."},
    )


# Register Routers
app.include_router(api_v1_router, prefix=settings.API_V1_STR)


@app.get("/", tags=["Root"])
async def root():
    return {
        "name": settings.APP_NAME,
        "status": "online",
        "docs": "/docs",
        "endpoints": {
            "resolve": f"{settings.API_V1_STR}/resolve",
            "healthcheck": f"{settings.API_V1_STR}/healthcheck",
            "download": f"{settings.API_V1_STR}/download",
        },
    }


if __name__ == "__main__":
    uvicorn.run("main:app", host=settings.HOST, port=settings.PORT, reload=settings.DEBUG)
