import os
import shutil
import subprocess
from typing import Any

from app.core.config import settings


def build_runtime_env() -> dict[str, str]:
    env = os.environ.copy()

    if settings.node_extra_path:
        current_path = env.get("PATH", "")
        extra = settings.node_extra_path.strip()
        if extra and extra not in current_path:
            env["PATH"] = current_path + os.pathsep + extra

    return env


def detect_node() -> dict[str, Any]:
    env = build_runtime_env()

    # 1) tenta o binário configurado
    node_bin = settings.node_bin.strip() if settings.node_bin else "node"

    try:
        output = subprocess.check_output(
            [node_bin, "-v"],
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        ).strip()
        return {
            "available": True,
            "node_bin": node_bin,
            "version": output,
        }
    except Exception as e:
        # 2) fallback com shutil.which
        resolved = shutil.which("node", path=env.get("PATH"))
        return {
            "available": False,
            "node_bin": node_bin,
            "resolved_path": resolved,
            "error": str(e),
        }