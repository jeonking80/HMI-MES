"""Serve an explicitly selected local production web build on the API origin."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


def mount_web(app: FastAPI, directory: Path) -> None:
    directory = directory.resolve(strict=True)
    index = directory / "index.html"
    assets = directory / "assets"
    if not index.is_file() or not assets.is_dir():
        raise ValueError("Build the selected web directory before serving")
    if index.is_symlink() or assets.is_symlink():
        raise ValueError("Web build entries must remain in the selected directory")

    @app.get("/", include_in_schema=False)
    async def home():
        return FileResponse(index)

    app.mount("/assets", StaticFiles(directory=assets, follow_symlink=False), name="web-assets")
