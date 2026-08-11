from __future__ import annotations

import logging
from pathlib import Path

from agent.main_agent.config import PROJECT_ROOT


DEFAULT_LOG_PATH = PROJECT_ROOT / "logs" / "agent.log"


def configure_agent_logging(log_path: Path | str | None = None) -> Path:
    path = Path(log_path or DEFAULT_LOG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    for handler in list(root.handlers):
        if getattr(handler, "_real_agent_handler", False):
            root.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    )
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler._real_agent_handler = True
    root.addHandler(file_handler)
    logging.getLogger(__name__).info("logging configured path=%s", path)
    return path
