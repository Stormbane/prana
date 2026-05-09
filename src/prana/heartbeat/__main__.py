"""Run the heartbeat daemon.

Usage:
    python -m prana.heartbeat                    # Run with defaults (30 min interval)
    python -m prana.heartbeat --interval 300     # 5 minute interval (testing)
    python -m prana.heartbeat --once             # Single heartbeat then exit
    python -m prana.heartbeat --lora-path PATH   # Specific LoRA adapter

All runtime config (prompts, state sources, cycle-log paths) is read from
``~/.narada/heartbeat/wake.md``. See ``examples/narada-install/`` for the
template to install.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from prana.heartbeat.daemon import HeartbeatDaemon, DEFAULT_INTERVAL
from prana.heartbeat.delegate import ClaudeDelegate
from deha.display import DisplayClient
from prana.heartbeat.viveka import VivekaCore
from prana.heartbeat.wake import WAKE_PATH, WakeManifestError, load_manifest


def _assert_no_api_key() -> None:
    """Abort startup if an Anthropic API key is in the environment.

    The heartbeat MUST use Max subscription credentials (~/.claude/.credentials.json).
    Inheriting an API key from an interactive shell silently routes every
    cycle to API billing — the Apr 12-15 daemon did 180 cycles at ~$0.95 each
    before we caught it. See .ai/logs/errors.md for prior incidents.
    """
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if os.environ.get(var):
            sys.stderr.write(
                f"\nFATAL: {var} is set in the heartbeat environment.\n"
                f"The heartbeat must run on the Max subscription, not API billing.\n"
                f"Clear it in your launcher ('set {var}=') and try again.\n\n"
            )
            sys.exit(2)


def _load_manifest_or_exit():
    try:
        return load_manifest()
    except WakeManifestError as exc:
        sys.stderr.write(f"\nFATAL: {exc}\n\n")
        sys.stderr.write(
            "Install the example manifest:\n"
            "  mkdir -p ~/.narada/heartbeat\n"
            "  cp -r examples/narada-install/heartbeat/* ~/.narada/heartbeat/\n\n"
        )
        sys.exit(3)


def main() -> None:
    _assert_no_api_key()

    parser = argparse.ArgumentParser(description="Svapna heartbeat daemon")
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL,
        help=f"Seconds between heartbeats (default: {DEFAULT_INTERVAL})",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single heartbeat then exit",
    )
    parser.add_argument(
        "--lora-path", type=Path, default=None,
        help="Path to LoRA adapter (default: models/lora/latest or most recent)",
    )
    parser.add_argument(
        "--model", type=str, default=None,
        help="Base model override (default: from training.yml)",
    )
    parser.add_argument(
        "--claude-model", type=str, default="claude-sonnet-4-6",
        help="Claude model for delegation (default: claude-sonnet-4-6)",
    )
    parser.add_argument(
        "--display-ip", type=str, default="192.168.86.35",
        help="ESP32 display device IP address",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Verbose logging",
    )
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Wake manifest MUST exist — no fallback
    manifest = _load_manifest_or_exit()
    print(f"Wake manifest: {manifest.source_file}")
    print(f"  prompts: {', '.join(manifest.prompts.keys())}")
    print(f"  state keys: {', '.join(manifest.state.keys())}")

    # Resolve LoRA path
    lora_path = args.lora_path
    if lora_path is None:
        latest = Path("models/lora/latest")
        if latest.exists():
            lora_path = latest
        else:
            lora_dirs = sorted(Path("models/lora").glob("2026-*"), reverse=True)
            if lora_dirs:
                lora_path = lora_dirs[0]

    # Resolve base model
    model_path = args.model
    if model_path is None:
        try:
            import yaml
            with open("config/training.yml") as f:
                cfg = yaml.safe_load(f)
            model_path = cfg.get("base_model", {}).get("name", "")
        except Exception:
            model_path = "unsloth/Qwen3-8B-unsloth-bnb-4bit"

    print("=== Svapna Heartbeat Daemon ===")
    print(f"Model:    {model_path}")
    print(f"LoRA:     {lora_path}")
    print(f"Claude:   {args.claude_model}")
    print(f"Interval: {args.interval}s")
    print()

    viveka = VivekaCore(model_path=model_path, lora_path=lora_path)
    delegate = ClaudeDelegate(model=args.claude_model)
    display = DisplayClient(device_ip=args.display_ip)

    if display.is_available():
        print(f"Display: connected ({args.display_ip})")
    else:
        print(f"Display: not reachable ({args.display_ip}) — running without display")

    daemon = HeartbeatDaemon(
        viveka=viveka,
        delegate=delegate,
        manifest=manifest,
        display=display,
        interval=args.interval,
    )

    if args.once:
        print("Running single heartbeat...")
        viveka.load()
        result = daemon.beat_once()
        print(f"\nResult: {result}")
        viveka.unload()
    else:
        print("Starting heartbeat loop (Ctrl+C to stop)...")
        daemon.run()

    print("\nDone.")


if __name__ == "__main__":
    main()
