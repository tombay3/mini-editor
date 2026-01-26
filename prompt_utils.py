import os
import tomllib
import streamlit as st
from pathlib import Path
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# Path to the prompts.toml file
DEFAULT_PROMPTS = "prompts.toml"


def load_prompts(prompt_file: Optional[str] = None) -> Dict[str, Any]:
    """
    Load prompts from the prompts.toml file

    Returns:
        Dictionary containing all prompts and their metadata
    """
    if not prompt_file:
        prompt_file = DEFAULT_PROMPTS
        try:
            if "prompt_path" in st.secrets:
                user_prompt_path = Path(st.secrets["prompt_path"]).expanduser()
                if user_prompt_path.exists():
                    prompt_file = str(user_prompt_path)
        except Exception:
            pass

    # Check if prompts file exists
    if not os.path.exists(prompt_file):
        logger.error(f"Prompts file not found: {prompt_file}")
        return {}

    try:
        # Load prompts from TOML file
        logger.info(f"Opening prompts file: {prompt_file}")
        with open(prompt_file, "rb") as f:
            prompts = tomllib.load(f)
        logger.debug(
            f"Loaded {len(prompts)} prompt categories {list(prompts.keys())} from {prompt_file}"
        )
        return prompts
    except Exception as e:
        logger.error(f"Error loading prompts: {str(e)}")
        return {}


def get_prompt(prompt_path: str, **variables) -> Optional[str]:
    """
    Get a prompt by its path and format it with the provided variables

    Args:
        prompt_path: Path to the prompt in dot notation (e.g., "category.subcategory.prompt_name")
        **variables: Variables to format the prompt with

    Returns:
        Formatted prompt or None if not found
    """
    prompts = load_prompts()

    # Navigate through the prompt structure using the path
    path_parts = prompt_path.split(".")
    current = prompts

    for part in path_parts:
        if part not in current:
            logger.error(f"Prompt path not found: {prompt_path}")
            return None
        current = current[part]

    # Check if we have a template
    if not isinstance(current, dict) or "template" not in current:
        logger.error(f"No template found at prompt path: {prompt_path}")
        return None

    # Get the template and format it with variables
    template = current["template"]

    try:
        return template.format(**variables)
    except KeyError as e:
        logger.error(f"Missing variable in prompt template: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Error formatting prompt: {str(e)}")
        return None


def get_system_message(message_type: str) -> Optional[str]:
    """
    Get a system message for AI models

    Args:
        message_type: Type of system message in dot notation

    Returns:
        System message or None if not found
    """
    return get_prompt(f"system_messages.{message_type}")
