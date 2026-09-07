from __future__ import annotations

import os
import re
import sys
from pathlib import Path

TOKEN = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def _secret(name: str) -> str:
    file_name = os.getenv(f"{name}_FILE")
    if file_name:
        value = Path(file_name).read_text().strip()
    else:
        value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required gateway value {name} is unavailable.")
    if "\n" in value or "\r" in value:
        raise RuntimeError(f"Gateway value {name} contains a newline.")
    return value


def render(template_path: Path, output_path: Path) -> None:
    template = template_path.read_text()
    names = set(TOKEN.findall(template))
    values: dict[str, str] = {}
    secret_names = {"GATEWAY_SHARED_SECRET", "KONG_REDIS_PASSWORD"}
    for name in names:
        values[name] = _secret(name) if name in secret_names else os.getenv(name, "").strip()
        if not values[name]:
            raise RuntimeError(f"Required gateway value {name} is unavailable.")
        if "\n" in values[name] or "\r" in values[name]:
            raise RuntimeError(f"Gateway value {name} contains a newline.")

    rendered = TOKEN.sub(lambda match: values[match.group(1)], template)
    if TOKEN.search(rendered):
        raise RuntimeError("Rendered gateway configuration still contains placeholders.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(".tmp")
    temporary.write_text(rendered)
    temporary.chmod(0o600)
    temporary.replace(output_path)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: render_config.py TEMPLATE OUTPUT")
    render(Path(sys.argv[1]), Path(sys.argv[2]))
