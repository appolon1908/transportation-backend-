"""Compatibility entrypoint for the canonical integration worker.

The production image uses :mod:`workers.integration_worker`. Keeping this thin
module prevents old process definitions from importing a second, incompatible
worker implementation.
"""

from workers.integration_worker import main


if __name__ == "__main__":
    main()
