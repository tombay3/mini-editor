"""
Gemini AI module for audio transcription and summary generation.
Handles all AI-related operations with proper rate limiting and retries.
"""

import os
import re
import logging
import json
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception,
    before_sleep_log,
)
from google import genai
from prompt_utils import get_prompt, load_prompts

logger = logging.getLogger(__name__)

# Gemini client setup - always uses GEMINI_API_KEY
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY)

# Model to use for all AI operations
MODEL_NAME = "gemini-3-pro-preview"
# MODEL_NAME = "gemini-2.5-pro"


def is_rate_limit_error(exception):
    """Check if the exception is a rate limit error."""
    error_msg = str(exception)
    return (
        "429" in error_msg
        or "RATELIMIT_EXCEEDED" in error_msg
        or "quota" in error_msg.lower()
        or "rate limit" in error_msg.lower()
        or (hasattr(exception, "status") and exception.status == 429)
    )


def upload_to_gemini(file_path):
    """Uploads a local file to Gemini and returns the file reference."""
    logger.info(f"Uploading file to Gemini: {file_path}")
    return client.files.upload(file=str(file_path))


def gemini_uploaded(name):
    """Gets a file object from Gemini by name (URI), or None if not found."""
    try:
        return client.files.get(name=name)
    except Exception as e:
        logger.debug(f"File {name} not found or error checking status: {e}")
        return None


def transcribe_audio(file_ref, prompt_path):
    """Transcribe audio using an existing Gemini file reference.

    Args:
        file_ref: The Gemini File object (already uploaded)
        prompt_path: Path to the prompt in prompts.toml

    Returns:
        Dictionary containing transcript text, tone, and vibe
    """
    prompt = get_prompt(prompt_path)
    if not prompt:
        raise ValueError(f"Prompt '{prompt_path}' not found in prompts.toml")

    logger.info(
        f"Starting transcription for file_ref={file_ref.name} using {MODEL_NAME}"
    )

    @retry(
        stop=stop_after_attempt(7),
        wait=wait_exponential(multiplier=1, min=2, max=128),
        retry=retry_if_exception(is_rate_limit_error),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def generate_transcription():
        response = client.models.generate_content(
            model=MODEL_NAME, contents=[file_ref, prompt]
        )
        text = response.text or ""

        tone = []
        vibe = []

        # Extract TONE
        tone_match = re.search(r"\[TONE\]\s*(.*)", text, re.IGNORECASE)
        if tone_match:
            tone = [
                t.strip().lstrip("#")
                for t in re.split(r"[,， /]", tone_match.group(1))
                if t.strip().lstrip("#")
            ]
            text = text.replace(tone_match.group(0), "")

        # Extract VIBE
        vibe_match = re.search(r"\[VIBE\]\s*(.*)", text, re.IGNORECASE)
        if vibe_match:
            vibe = [
                t.strip().lstrip("#")
                for t in re.split(r"[,， /]", vibe_match.group(1))
                if t.strip().lstrip("#")
            ]
            text = text.replace(vibe_match.group(0), "")

        return {"transcript": text.strip(), "tone": tone, "vibe": vibe}

    return generate_transcription()


def generate_summary(
    file_ref, transcript, episode_title, prompt_path, summary_length="short"
):
    """Generate a summary from the transcript using Gemini.

    Args:
        file_ref: The Gemini File object (already uploaded)
        transcript: The transcribed text to summarize
        episode_title: Title of the episode for context
        prompt_path: Path to the prompt in prompts.toml
        summary_length: One of "short", "medium", or "long"

    Returns:
        Dictionary containing summary, categories, and tags
    """
    # Adjust word count based on length setting
    length_settings = {
        "short": ("1 paragraph", "30-50"),
        "medium": ("2 paragraphs", "60-90"),
        "long": ("3 paragraphs", "100-150"),
    }
    paragraphs, word_range = length_settings.get(
        summary_length, length_settings["short"]
    )

    prompt = get_prompt(
        prompt_path,
        episode_title=episode_title,
        paragraphs=paragraphs,
        word_range=word_range,
        transcript=transcript,
    )
    if not prompt:
        raise ValueError(f"Prompt '{prompt_path}' not found in prompts.toml")

    logger.info(f"Generating summary for '{episode_title}' using {MODEL_NAME}")

    @retry(
        stop=stop_after_attempt(7),
        wait=wait_exponential(multiplier=1, min=2, max=128),
        retry=retry_if_exception(is_rate_limit_error),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def generate():
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        text = response.text or ""
        try:
            # Attempt to extract JSON object if wrapped in markdown or text
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
            else:
                data = json.loads(text)

            # Post-processing: Filter categories
            prompts_data = load_prompts()
            blacklist = prompts_data.get("settings", {}).get("category_blacklist", [])

            if "categories" in data and isinstance(data["categories"], list):
                data["categories"] = [
                    c.lstrip("#")
                    for c in data["categories"]
                    if not any(b in c.lower() for b in blacklist)
                ]

            if "tags" in data and isinstance(data["tags"], list):
                data["tags"] = [t.lstrip("#") for t in data["tags"]]

            return data
        except (json.JSONDecodeError, AttributeError) as e:
            # Fallback for non-JSON response
            logger.error(
                f"Failed to parse summary JSON for '{episode_title}'. Error: {e}"
            )
            logger.debug(f"Raw text causing error: {text}")
            return {"summary": text, "categories": [], "tags": []}

    return generate()


def to_sec(t):
    """Convert timestamp string to seconds."""
    parts = list(map(int, t.split(":")))
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return 0


def extract_music_segments(transcript):
    """Extract music segments and calculate total duration from transcript."""
    content_str = ""
    if isinstance(transcript, list):
        for item in transcript:
            content_str += item.get("content", "") + "\n"
    else:
        content_str = str(transcript)

    matches = re.findall(
        r"\[MUSIC PLAYING:?\s*(\d+:\d+(?::\d+)?)\s*-\s*(\d+:\d+(?::\d+)?)\]",
        content_str,
        flags=re.IGNORECASE,
    )

    segments = []
    total_duration = 0

    for start_str, end_str in matches:
        start = to_sec(start_str)
        end = to_sec(end_str)
        duration = end - start
        # Only consider segments of at least 60 seconds
        if duration >= 60:
            segments.append({"start": start, "end": end})
            total_duration += duration

    return {"segments": segments, "total_duration": total_duration}
