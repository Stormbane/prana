"""Local model loader for the viveka core.

Loads a base model + optional LoRA adapter via Unsloth. Extracted from
svapna.identity.generate so prana stays decoupled from svapna's training
code — the LoRA is consumed as a filesystem artifact, not a Python import.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class GenerateConfig:
    model_path: str = ""
    lora_path: Path | None = None
    max_new_tokens: int = 512
    temperature: float = 0.7
    top_p: float = 0.9
    repetition_penalty: float = 1.15
    max_seq_length: int = 2048


def load_model(config: GenerateConfig) -> tuple[Any, Any]:
    if not config.model_path:
        raise ValueError("No model path configured.")

    try:
        from unsloth import FastLanguageModel
    except ImportError:
        raise ImportError(
            "Unsloth not installed. Run: pip install 'prana[viveka]'"
        )

    logger.info("Loading base model: %s", config.model_path)
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=config.model_path,
        max_seq_length=config.max_seq_length,
        dtype=None,
        load_in_4bit=True,
    )

    if config.lora_path and config.lora_path.exists():
        try:
            from peft import PeftModel

            logger.info("Loading LoRA adapter: %s", config.lora_path)
            model = PeftModel.from_pretrained(model, str(config.lora_path))
        except Exception as e:
            logger.warning(
                "Failed to load LoRA adapter from %s: %s. Using base model.",
                config.lora_path, e,
            )
    elif config.lora_path:
        logger.warning(
            "LoRA adapter path does not exist: %s. Using base model.",
            config.lora_path,
        )

    FastLanguageModel.for_inference(model)
    return model, tokenizer
