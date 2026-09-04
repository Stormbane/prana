"""``python -m prana.brain`` — run the brain server under the host.

Startup is fail-closed (spec §1a): missing wake context, unloadable
tokens, or a broken git-crypt storage contract stop the server before
it binds — a brain that can't authenticate or can't be Narada must not
answer.
"""

from __future__ import annotations

import logging
import sys

import uvicorn

from prana.brain.api import create_app
from prana.brain.backend import SdkBackend
from prana.brain.config import BrainConfig
from prana.brain.tokens import load_brain_tokens

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("prana.brain")


def main() -> None:
    config = BrainConfig()

    if not config.wake_context.exists():
        sys.stderr.write(f"FATAL: wake context missing: {config.wake_context}\n")
        sys.exit(2)
    try:
        # load_brain_tokens verifies git-crypt coverage before it will
        # create or read the file.
        load_brain_tokens()
    except (RuntimeError, OSError) as exc:
        sys.stderr.write(f"FATAL: token storage contract: {exc}\n")
        sys.exit(2)

    config.sessions_root.mkdir(parents=True, exist_ok=True)
    logger.info("brain server starting on %s:%s (model=%s)",
                config.host, config.port, config.model)
    app = create_app(config, SdkBackend)
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")


if __name__ == "__main__":
    main()
