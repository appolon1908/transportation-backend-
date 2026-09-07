from __future__ import annotations

import os
import re
import sys
from pathlib import Path

TOKEN = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
SECRET_NAMES = {"GATEWAY_SHARED_SECRET", "KONG_REDIS_PASSWORD"}


def _read_value(name: str) -> str:
    secret_file = os.getenv(f"{name}_FILE")
    value = Path(secret_file).read_text().strip() if secret_file else os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required gateway value {name} is unavailable.")
    if "\n" in value or "\r" in value:
        raise RuntimeError(f"Gateway value {name} contains a newline.")
    return value


def render(template_path: Path, output_path: Path) -> None:
    template = template_path.read_text(encoding="utf-8")
    names = set(TOKEN.findall(template))
    values = {name: _read_value(name) for name in names}
    rendered = TOKEN.sub(lambda match: values[match.group(1)], template)
    if TOKEN.search(rendered):
        raise RuntimeError("Rendered gateway configuration still contains placeholders.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.chmod(0o444)
    temporary.replace(output_path)

    # Do not print the rendered document; it contains runtime credentials.
    print(f"rendered {output_path.name} with {len(names)} resolved values")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: render_config_v2.py TEMPLATE OUTPUT")
    render(Path(sys.argv[1]), Path(sys.argv[2]))
