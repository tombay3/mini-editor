import os
import sys
import json
import time
import argparse
import datetime
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from google.genai import types
from mutagen.mp4 import MP4
from prompt_utils import get_prompt
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%m-%d %H:%M",
)
logger = logging.getLogger(__name__)

# Load environment variables (GEMINI_API_KEY)
load_dotenv()


def extract_metadata(file_path):
    """Extract metadata (title, date, duration) from m4a file tags."""
    try:
        audio = MP4(file_path)

        # Extract title (\xa9nam atom)
        title_tags = audio.tags.get("\xa9nam") if audio.tags else None
        title = title_tags[0] if title_tags else Path(file_path).stem

        # Extract duration
        duration = audio.info.length
        minutes = int(duration // 60)
        seconds = int(duration % 60)
        duration_str = f"{minutes:02}:{seconds:02}"

        # File size
        file_size = os.path.getsize(file_path)
        file_size_mb = int(file_size / (1024 * 1024))

        return {
            "title": title,
            "duration": duration_str,
            "file_size_mb": file_size_mb,
        }
    except Exception as e:
        print(f"Warning: Could not extract metadata ({e}). Using defaults.")
        stem = Path(file_path).stem
        file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        return {
            "title": stem,
            "duration": "00:00",
            "file_size_mb": int(file_size / (1024 * 1024)),
        }


def transcribe_audio(file_path, prompt_path):
    prompt = get_prompt(prompt_path)
    if not prompt:
        print("Failed to load prompt templates")
        return None

    """Upload audio to Gemini and generate transcript + summary."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY not found in environment variables.")
        return None

    client = genai.Client(api_key=api_key)

    print(f"Uploading file: {file_path}...")
    try:
        file_ref = client.files.upload(file=file_path)
        print(f"File uploaded: {file_ref.uri}")

        # Wait for processing (Audio is usually fast, but good practice)
        while file_ref.state and file_ref.state.name == "PROCESSING":
            print("Processing file on server...", end="\r")
            time.sleep(2)
            file_ref = client.files.get(name=str(file_ref.name))

        if file_ref.state and file_ref.state.name == "FAILED":
            print("\nError: File processing failed on server.")
            return None

    except Exception as e:
        print(f"\nError during file upload: {e}")
        return None

    print("\nGenerating transcript with Gemini...")
    try:
        response = client.models.generate_content(
            model="gemini-pro-latest",
            contents=[file_ref, prompt],
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        # Ensure response.text is not None before loading as JSON
        if response.text:
            return json.loads(response.text)
        return None
    except Exception as e:
        print(f"Error during generation: {e}")
        return None


def format_transcript(transcript_list):
    """Format transcript JSON list into a plain text string."""
    lines = []
    for segment in transcript_list:
        timestamp = segment.get("timestamp", "")
        content = segment.get("content", "")
        if content.strip().startswith("[MUSIC PLAYING:"):
            lines.append(content)
        else:
            lines.append(f"[{timestamp}] {content}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Transcribe m4a audio using Gemini.")
    parser.add_argument("prompt_path", help="Prompt key from prompts.toml")
    parser.add_argument("input_file", help="Path to the .m4a file")
    parser.add_argument(
        "output_dir", nargs="?", default="out", help="Directory to save output files"
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("Debug logging enabled")

    if get_prompt(args.prompt_path) is None:
        print(f"Error: Prompt '{args.prompt_path}' not found in prompts.toml")
        sys.exit(1)

    input_path = Path(args.input_file).expanduser().resolve()
    if not input_path.exists():
        logger.error(f"File not found: {input_path}")
        sys.exit(1)

    metadata = extract_metadata(input_path)
    print(
        f"Processing: {metadata['title']} ({metadata['duration']}, {metadata['file_size_mb']} MB)"
    )
    result = transcribe_audio(input_path, args.prompt_path)
    if result:
        # Merge metadata and result
        final_output = metadata.copy()
        final_output["transcript"] = result.get("transcript", [])
        final_output["summary"] = result.get("summary", "")

        # Create output directory if it doesn't exist
        os.makedirs(args.output_dir, exist_ok=True)

        # Generate base_name: 5 chars of filename + MMDD + HHMM
        stem = Path(input_path).stem
        short_stem = stem[:5]
        now = datetime.datetime.now()
        mmdd = now.strftime("%m%d")
        hhmm = now.strftime("%H%M")
        base_name = f"{short_stem}-{mmdd}-{hhmm}-{args.prompt_path}"

        # Save JSON
        json_file = os.path.join(args.output_dir, f"{base_name}.json")
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(final_output, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved JSON to: {json_file}")

        # Save Summary TXT
        summary_file = os.path.join(args.output_dir, f"{base_name}-sum.txt")
        with open(summary_file, "w", encoding="utf-8") as f:
            f.write(final_output["summary"])
        print(f"Saved summary to: {summary_file}")

        # Save Formatted Transcript TXT
        transcript_file = os.path.join(args.output_dir, f"{base_name}-srt.txt")
        formatted_transcript = format_transcript(final_output["transcript"])
        with open(transcript_file, "w", encoding="utf-8") as f:
            f.write(formatted_transcript)
        print(f"Saved transcript to: {transcript_file}")


if __name__ == "__main__":
    main()
