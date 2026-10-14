from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .agent_backend import get_agent_backend


def read_pi_model_catalog(models_path: Path, auth_path: Path) -> dict[str, Any]:
    binary = shutil.which(get_agent_backend("pi").cli_bin())
    node = shutil.which("node")
    if binary is None or node is None:
        return {}
    try:
        result = subprocess.run(
            [node, str(Path(__file__).with_suffix(".mjs")), binary,
             str(models_path), str(auth_path), str(models_path.parent / "models-store.json")],
            capture_output=True, text=True, timeout=15, check=True,
        )
        data = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    return data if isinstance(data, dict) and isinstance(data.get("models"), list) else {}
