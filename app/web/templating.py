from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.config import settings

_TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Shown in the upload hint so the stated limit always matches the enforced one.
templates.env.globals["upload_max_mb"] = round(settings.upload_max_bytes / (1024 * 1024))
