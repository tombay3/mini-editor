import streamlit as st
import argparse
import logging
import re
import uuid
import time
from datetime import datetime
from pathlib import Path
from mutagen.mp4 import MP4
from storage import db, get_all_episodes
from gemini import (
    transcribe_audio,
    generate_summary,
    upload_to_gemini,
    gemini_uploaded,
    extract_music_segments,
)
from prompt_utils import load_prompts
from compare import render_comparison, generate_diff_html, render_timeline_html

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%m-%d %H:%M",
)
logger = logging.getLogger(__name__)

# Parse command line arguments
parser = argparse.ArgumentParser()
parser.add_argument("--debug", action="store_true", help="Enable debug logging")
parser.add_argument("--base", action="store_true", help="Enable base comparison")
args, _ = parser.parse_known_args()

if args.debug:
    logging.getLogger().setLevel(logging.DEBUG)
    logger.debug("Debug logging enabled")

# Uploads directory for audio files
UPLOADS_DIR = Path("uploads")
UPLOADS_DIR.mkdir(exist_ok=True)

# Page config
st.set_page_config(
    page_title="Editorial Review",
    page_icon="📻",
    layout="wide",
    initial_sidebar_state="expanded",
)

with open("styles.css") as f:
    st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)


def extract_metadata(file_path):
    """Extract metadata from m4a file."""
    path_obj = Path(file_path)
    if not path_obj.exists():
        return {
            "title": path_obj.name,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "duration": "00:00",
            "duration_seconds": 0,
            "file_size_mb": 0,
        }

    try:
        audio = MP4(str(file_path))
    except Exception as e:
        logger.error(f"Failed to extract metadata from {file_path}: {e}")
        return {
            "title": path_obj.name,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "duration": "00:00",
            "duration_seconds": 0,
            "file_size_mb": path_obj.stat().st_size / (1024 * 1024),
        }

    tags = audio.tags or {}
    filename = path_obj.name

    # Extract title
    title = filename.replace(".m4a", "").replace(".M4A", "")
    if "\xa9nam" in tags:
        title = tags["\xa9nam"][0]
    elif "title" in tags:
        title = tags["title"][0]

    # Extract date from title
    date = None
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", title)
    if date_match:
        date = date_match.group(1)

    # Fall back to file metadata if no date in title
    if not date and "\xa9day" in tags:
        date_str = tags["\xa9day"][0]
        try:
            if len(date_str) >= 10:
                date = date_str[:10]
            elif len(date_str) == 4:
                date = f"{date_str}-01-01"
        except Exception:
            pass

    # Default to today if no date found
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    # Extract duration
    duration_seconds = int(audio.info.length)
    duration_formatted = f"{duration_seconds // 60}:{duration_seconds % 60:02d}"

    # File size
    file_size_mb = path_obj.stat().st_size / (1024 * 1024)
    return {
        "title": title,
        "date": date,
        "duration": duration_formatted,
        "duration_seconds": duration_seconds,
        "file_size_mb": file_size_mb,
    }


def save_episode(episode_id, episode_data):
    """Save episode to database."""
    db[f"episode:{episode_id}"] = episode_data
    logger.info(
        f"Episode saved: {episode_id} ({episode_data.get('title', 'Untitled')}) - Status: {episode_data.get('status')}"
    )


def get_episode(episode_id):
    """Get a single episode from database."""
    key = f"episode:{episode_id}"
    if key in db:
        return db[key]
    return None


def delete_episode(episode_id):
    """Delete an episode from database and its audio file."""
    key = f"episode:{episode_id}"
    if key in db:
        del db[key]
        logger.info(f"Episode deleted: {episode_id}")


def format_status_badge(episode):
    status = episode.get("status", "pending")
    status_class = f"status-{status.lower()}"
    status_text = {
        "pending": "待審核 Pending",
        "approved": "已批准 Approved",
        "rejected": "已拒絕 Rejected",
    }.get(status.lower(), status)
    return f'<span class="status-badge {status_class}">{status_text}</span>'


def format_tag_badge(episode):
    html = ""
    pct = 0
    # Which genre based on music percentage
    music_duration = episode.get("music_duration")
    if music_duration:
        total_duration = episode.get("duration_seconds", 1)
        if total_duration == 0:
            total_duration = 1
        pct = (music_duration / total_duration) * 100

    for cat in episode.get("categories", []):
        cat_badge = "tag-blue" if pct >= 75 else "tag-red"
        html += f' <span class="tag-badge {cat_badge}">{cat}</span>'
    for tone in episode.get("tone", []):
        html += f' <span class="tag-badge tag-green">{tone}</span>'
    for vibe in episode.get("vibe", []):
        html += f' <span class="tag-badge tag-green">{vibe}</span>'
    for tag in episode.get("tags", []):
        html += f' <span class="tag-badge">#{tag}</span>'

    return html


def show_toast(msg, icon=None):
    """Add a toast message to the queue to be displayed on next run."""
    if "toast_queue" not in st.session_state:
        st.session_state.toast_queue = []
    st.session_state.toast_queue.append((msg, icon, time.time()))


@st.dialog("Confirm Deletion")
def confirm_delete(episode_id, title):
    """Show confirmation dialog for deleting an episode."""
    st.write(f"Are you sure you want to delete **{title}**?")
    st.warning("This action cannot be undone.")
    col1, col2 = st.columns(2)
    with col1:
        if st.button(
            "Cancel", key=f"cancel_del_{episode_id}", use_container_width=True
        ):
            st.rerun()
    with col2:
        if st.button(
            "Delete",
            key=f"confirm_del_{episode_id}",
            type="primary",
            use_container_width=True,
        ):
            delete_episode(episode_id)
            show_toast(f"Episode '{title}' deleted successfully.", icon="🗑️")
            st.rerun()


# Main app
def main():
    # Initialize session state
    if "toast_queue" not in st.session_state:
        st.session_state.toast_queue = []

    # Display pending toasts
    if st.session_state.toast_queue:
        now = time.time()
        for item in st.session_state.toast_queue:
            if len(item) == 3:
                msg, icon, timestamp = item
                if now - timestamp > 30:
                    continue
            else:
                msg, icon = item
            st.toast(msg, icon=icon)
        st.session_state.toast_queue = []

    if "current_page" not in st.session_state:
        # Handle deep linking via query parameters
        target_page = st.query_params.get("page")
        if target_page == "review":
            episode_id = st.query_params.get("id")
            if episode_id and get_episode(episode_id):
                st.session_state.current_page = "review"
                st.session_state.selected_episode = episode_id
            else:
                if episode_id:
                    show_toast(f"Episode not found: {episode_id}", icon="⚠️")
                    if "id" in st.query_params:
                        del st.query_params["id"]
                    st.query_params["page"] = "dashboard"
                st.session_state.current_page = "dashboard"
        elif target_page in ["dashboard", "upload", "comparison"]:
            st.session_state.current_page = target_page
        else:
            st.session_state.current_page = "dashboard"
    if "selected_episode" not in st.session_state:
        st.session_state.selected_episode = None
    if "processing" not in st.session_state:
        st.session_state.processing = False

    # Clear upload success state if not on upload page
    if st.session_state.current_page != "upload" and st.session_state.get(
        "last_processed_episode"
    ):
        st.session_state.last_processed_episode = None

    # Sidebar navigation
    with st.sidebar:
        st.header(
            "📻 Radio Show Editorial Review", width="content", text_alignment="center"
        )
        if st.button("📋 Episode Dashboard", use_container_width=True):
            st.session_state.current_page = "dashboard"
            st.session_state.selected_episode = None
            st.query_params["page"] = "dashboard"
            if "id" in st.query_params:
                del st.query_params["id"]
            st.rerun()

        if st.button("📤 Upload New Episode", use_container_width=True):
            st.session_state.current_page = "upload"
            st.query_params["page"] = "upload"
            if "id" in st.query_params:
                del st.query_params["id"]
            st.rerun()

        if args.base and st.button("📑 Base Comparison", use_container_width=True):
            st.session_state.current_page = "comparison"
            st.query_params["page"] = "comparison"
            if "id" in st.query_params:
                del st.query_params["id"]
            st.rerun()

        st.divider()

        # Stats
        episodes = get_all_episodes()
        pending = sum(1 for e in episodes if e.get("status") == "pending")
        approved = sum(1 for e in episodes if e.get("status") == "approved")
        rejected = sum(1 for e in episodes if e.get("status") == "rejected")

        st.subheader("📊 Statistics")
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Pending", pending)
        with col2:
            st.metric("Approved", approved)
        with col3:
            st.metric("Rejected", rejected)

    # Main content area
    if st.session_state.current_page == "dashboard":
        render_dashboard()
    elif st.session_state.current_page == "upload":
        render_upload()
    elif st.session_state.current_page == "comparison":
        render_comparison()
    elif st.session_state.current_page == "review":
        render_review()


def render_dashboard():
    """Render the episode dashboard."""
    st.markdown(
        """
    <div class="main-header">
        <h4>📋 Episode Dashboard</h4>
        <p>Review and approve AI-generated content for the audio archive</p>
    </div>
    """,
        unsafe_allow_html=True,
    )

    episodes = get_all_episodes()
    if not episodes:
        st.write("No episodes uploaded yet. Click 'Upload New Episode' to get started.")
        return

    # Episode list
    for episode in episodes:
        with st.container():
            col1, col2 = st.columns([5, 1])
            with col1:
                # Get truncated original filename
                filename = episode.get("original_filename", "")
                if not filename:
                    # Fallback to audio_path name if original_filename not saved
                    audio_path = episode.get("audio_path", "")
                    filename = Path(audio_path).name if audio_path else ""

                if filename:
                    truncated_name = (
                        filename[:8] + ".." + filename[-4:]
                        if len(filename) > 12
                        else filename
                    )
                else:
                    truncated_name = "N/A"

                created_at = episode.get("created_at", "")
                created_str = ""
                if created_at:
                    try:
                        dt = datetime.fromisoformat(created_at)
                        created_str = f" &nbsp;|&nbsp; 🕒 {dt.strftime('%m-%d %H:%M')}"
                    except ValueError:
                        pass

                prompt_path = episode.get("prompt_path", "")
                prompt_str = (
                    f"<span> &nbsp;{episode['id']} &nbsp;📝 {prompt_path}</span>"
                    if args.base
                    else ""
                )

                tag_badge = format_tag_badge(episode)
                st.markdown(
                    f"""
                <div class="episode-card">
                    <div class="episode-title">{episode.get("title", "Untitled")}{tag_badge}</div>
                    <div class="episode-meta">
                        🎵 {truncated_name} &nbsp;|&nbsp;
                        📅 {episode.get("date", "N/A")} &nbsp;|&nbsp; 
                        ⏱️ {episode.get("duration", "N/A")}{created_str} &nbsp;|&nbsp;
                        {format_status_badge(episode)}{prompt_str}
                    </div>
                </div>
                """,
                    unsafe_allow_html=True,
                )

            with col2:
                if st.button(
                    "📝 Review", key=f"review_{episode['id']}", use_container_width=True
                ):
                    st.session_state.selected_episode = episode["id"]
                    st.session_state.current_page = "review"
                    st.query_params["page"] = "review"
                    st.query_params["id"] = episode["id"]
                    st.rerun()


def render_upload():
    """Render the upload page."""
    st.markdown(
        """
    <div class="main-header">
        <h4>📤 Upload New Episode</h4>
        <p>Upload an m4a audio file for AI processing</p>
    </div>
    """,
        unsafe_allow_html=True,
    )

    # Initialize cache in db if not exists
    if "gemini_cache" not in db:
        db["gemini_cache"] = {"files": []}

    tab2, tab1 = st.tabs(["Select from uploaded files", "📤 Upload a new file"])
    # --- Tab 1: New Upload ---
    with tab1:
        uploaded_file = st.file_uploader("Choose an m4a file", type=["m4a"])
        if uploaded_file is not None:
            if st.button("🚀 Upload to Gemini", type="primary"):
                if st.session_state.get("last_processed_episode"):
                    st.session_state.last_processed_episode = None
                with st.spinner("Uploading to Gemini..."):
                    try:
                        # Save to uploads directory for persistence
                        save_path = UPLOADS_DIR / uploaded_file.name
                        with open(save_path, "wb") as f:
                            f.write(uploaded_file.getvalue())

                        # Extract metadata from saved file
                        metadata = extract_metadata(save_path)

                        # Upload to Gemini
                        file_ref = upload_to_gemini(save_path)

                        # Cache the reference
                        cache_entry = {
                            "name": file_ref.name,
                            "display_name": uploaded_file.name,
                            "uri": file_ref.uri,
                            "metadata": metadata,
                            "uploaded_at": datetime.now().isoformat(),
                            "audio_path": str(save_path),
                        }

                        # Update DB
                        current_cache = db["gemini_cache"]
                        current_cache["files"].insert(0, cache_entry)  # Add to top
                        db["gemini_cache"] = current_cache

                        show_toast(
                            f"Uploaded successfully! File reference: {file_ref.name}",
                            icon="✅",
                        )
                        st.session_state.selected_gemini_file = cache_entry
                        st.rerun()

                    except Exception as e:
                        logger.error(f"Upload failed: {e}", exc_info=True)
                        st.error(f"Upload failed: {str(e)}")

    # --- Tab 2: Reuse Existing ---
    with tab2:
        cached_files = db["gemini_cache"]["files"]
        if not cached_files:
            st.write("No previously uploaded files found.  Please upload a new file.")
        else:
            # Create options list
            sorted_files = sorted(
                cached_files, key=lambda x: x.get("uploaded_at", ""), reverse=True
            )
            options = {}
            for f in sorted_files:
                dt = datetime.fromisoformat(f["uploaded_at"])
                upload_str = f"{dt.strftime('%m-%d %H:%M:%S')}"
                label = f"{f['display_name']} . {upload_str}"
                options[label] = f

            selected_option = st.selectbox(
                "Select previously uploaded file:", list(options.keys())
            )

            if selected_option:
                selected_entry = options[selected_option]

                if st.button("📂 Load Selected File", type="primary"):
                    if st.session_state.get("last_processed_episode"):
                        st.session_state.last_processed_episode = None
                    # Verify file still exists on Gemini
                    with st.spinner("Verifying file on server..."):
                        file_obj = gemini_uploaded(selected_entry["name"])
                        if file_obj:
                            st.session_state.selected_gemini_file = selected_entry
                            show_toast("File verified and loaded!", icon="✅")
                            st.rerun()
                        else:
                            show_toast(
                                "File expired or not found on server. Removing from cache.",
                                icon="⚠️",
                            )
                            # Remove from cache
                            current_cache = db["gemini_cache"]
                            current_cache["files"] = [
                                f
                                for f in current_cache["files"]
                                if f["name"] != selected_entry["name"]
                            ]
                            db["gemini_cache"] = current_cache
                            st.rerun()

    # Show success message and Review Now button if episode was just processed
    if st.session_state.get("last_processed_episode"):
        episode_id = st.session_state.last_processed_episode
        episode = get_episode(episode_id)
        if episode:
            st.success(
                f"Episode **{episode.get('title', 'Untitled')}** processed successfully!"
            )
            if st.button("📝 Review Now", type="primary"):
                st.session_state.selected_episode = episode_id
                st.session_state.current_page = "review"
                st.session_state.last_processed_episode = None
                st.query_params["page"] = "review"
                st.query_params["id"] = episode_id
                st.rerun()

    # --- Processing Section ---
    if "selected_gemini_file" in st.session_state:
        entry = st.session_state.selected_gemini_file
        metadata = entry["metadata"]
        st.markdown("---")

        # Metadata cards
        st.markdown("### 📋 Episode file info")
        col1, col2, col3, col4 = st.columns([2, 1, 1, 1])
        with col1:
            st.info(f"**Title:** {metadata['title']}")
        with col2:
            st.info(f"**Date:** {metadata['date']}")
        with col3:
            st.info(f"**Duration:** {metadata['duration']}")
        with col4:
            st.info(f"**Size:** {metadata['file_size_mb']:.1f} MB")

        # Prompt selection and Summary length
        col_p1, col_p2 = st.columns([1, 2])
        with col_p1:
            prompts = load_prompts()
            prompt_keys = [k for k in prompts.keys() if k not in ["settings", "p1"]]
            # Default to e1 if exists, otherwise first available
            default_idx = prompt_keys.index("e1") if "e1" in prompt_keys else 0
            selected_prompt_suite = st.selectbox(
                "Processing prompt", options=prompt_keys, index=default_idx
            )

        with col_p2:
            summary_length = st.select_slider(
                "Summary Length",
                options=["short", "medium", "long"],
                value="short",
                format_func=lambda x: {
                    "short": "Short (30-50 words)",
                    "medium": "Medium (60-90 words)",
                    "long": "Long (100-150 words)",
                }[x],
            )

        col_process, col_clear = st.columns([3, 1])
        with col_process:
            if st.button(
                "🚀 Process with AI",
                type="primary",
                disabled=st.session_state.processing,
            ):
                st.session_state.processing = True
                logger.info(f"Starting AI processing for file: {entry['display_name']}")
                episode_id = str(uuid.uuid4())[:8]
                progress_bar = st.progress(0)
                status_text = st.empty()

                try:
                    status_text.text("🎙️ Transcribing audio with Gemini AI...")
                    progress_bar.progress(10)

                    # Get the file object again to pass to transcribe
                    # We know it exists because we verified it or just uploaded it
                    file_ref = gemini_uploaded(entry["name"])
                    if not file_ref:
                        st.error("File reference lost. Please upload again.")
                        st.stop()
                    progress_bar.progress(20)

                    transcript_data = transcribe_audio(
                        file_ref, f"{selected_prompt_suite}.transcript"
                    )
                    transcript_text = transcript_data["transcript"]
                    progress_bar.progress(60)

                    status_text.text("📝 Generating summary...")
                    summary_data = generate_summary(
                        file_ref,
                        transcript_text,
                        metadata["title"],
                        f"{selected_prompt_suite}.summary",
                        summary_length,
                    )
                    progress_bar.progress(90)

                    # Extract music timeline data
                    music_info = extract_music_segments(transcript_text)
                    status_text.text("💾 Saving episode...")

                    episode_data = {
                        "id": episode_id,
                        "title": metadata["title"],
                        "date": metadata["date"],
                        "duration": metadata["duration"],
                        "duration_seconds": metadata["duration_seconds"],
                        "transcript": transcript_text,
                        "summary": summary_data.get("summary", ""),
                        "original_summary": summary_data.get("summary", ""),
                        "music_segments": music_info["segments"],
                        "music_duration": music_info["total_duration"],
                        "tone": transcript_data.get("tone", []),
                        "vibe": transcript_data.get("vibe", []),
                        "categories": summary_data.get("categories", []),
                        "tags": summary_data.get("tags", []),
                        "prompt_path": selected_prompt_suite,
                        "original_filename": entry["display_name"],
                        "audio_path": entry.get("audio_path", ""),
                        "status": "pending",
                        "created_at": datetime.now().isoformat(),
                    }

                    save_episode(episode_id, episode_data)
                    progress_bar.progress(100)

                    status_text.text("✅ Episode processed successfully!")
                    logger.info(f"Episode processing complete: {episode_id}")
                    show_toast(
                        "Episode has been processed and is ready for review!", icon="✅"
                    )

                    # Store episode ID for Review Now button
                    st.session_state.last_processed_episode = episode_id

                    # Clear upload state
                    del st.session_state.selected_gemini_file
                    st.session_state.processing = False
                    st.rerun()

                except Exception as e:
                    logger.error(f"Error processing audio: {str(e)}", exc_info=True)
                    st.error(f"Error processing audio: {str(e)}")
                    st.session_state.processing = False
                finally:
                    st.session_state.processing = False

        with col_clear:
            if st.button("🗑️ Clear"):
                if "selected_gemini_file" in st.session_state:
                    del st.session_state.selected_gemini_file
                st.rerun()


def render_review():
    """Render the review page with two-column layout."""
    episode_id = st.session_state.selected_episode

    if not episode_id:
        st.warning("No episode selected. Please select an episode from the dashboard.")
        return

    episode = get_episode(episode_id)

    if not episode:
        show_toast("Episode not found. Redirecting to dashboard...", icon="❌")
        st.session_state.current_page = "dashboard"
        st.session_state.selected_episode = None
        st.query_params["page"] = "dashboard"
        if "id" in st.query_params:
            del st.query_params["id"]
        st.rerun()
        return

    # Header with back button
    col_back, col_title = st.columns([1, 5])
    with col_back:
        if st.button("← Back"):
            st.session_state.current_page = "dashboard"
            st.session_state.selected_episode = None
            st.query_params["page"] = "dashboard"
            if "id" in st.query_params:
                del st.query_params["id"]
            st.rerun()

    st.markdown(
        f"""
    <div class="main-header">
        <h4>📝 Editorial Review</h4>
        <h5>{episode.get("title", "Untitled")} &nbsp;|&nbsp; {episode.get("date", "")} &nbsp;|&nbsp; {episode.get("duration", "")}</h5>
    </div>
    """,
        unsafe_allow_html=True,
    )

    # Status display
    st.markdown(
        f"**Current Status:** {format_status_badge(episode)}",
        unsafe_allow_html=True,
    )

    # Audio player
    st.markdown("#### 🎧 Audio Playback")

    # Initialize audio start time if not set
    if "audio_start_time" not in st.session_state:
        st.session_state.audio_start_time = 0

    if "audio_path" in episode:
        audio_path = Path(episode["audio_path"])
        if audio_path.exists():
            audio_bytes = audio_path.read_bytes()
            st.audio(
                audio_bytes,
                format="audio/mp4",
                start_time=st.session_state.audio_start_time,
            )

            # Music Segments Navigation
            segments = episode.get("music_segments")
            if segments is None:
                info = extract_music_segments(episode.get("transcript", ""))
                segments = info["segments"]
                # Update local episode dict so render_timeline_html uses it
                episode["music_segments"] = segments
                episode["music_duration"] = info["total_duration"]

            if segments:
                # Collect all jump points (start and end of each segment)
                jump_points = []
                for s in segments:
                    jump_points.append(s["start"])
                    jump_points.append(s["end"])
                jump_points = sorted(list(set(jump_points)))

                def skip_to_prev():
                    current = st.session_state.audio_start_time
                    # Find point < current - 2
                    prev_points = [p for p in jump_points if p < current - 2]
                    if prev_points:
                        st.session_state.audio_start_time = int(prev_points[-1])
                    elif jump_points:
                        # Wrap to last
                        st.session_state.audio_start_time = int(jump_points[-1])

                def skip_to_next():
                    current = st.session_state.audio_start_time
                    # Find point > current + 2
                    next_point = next((p for p in jump_points if p > current + 2), None)
                    if next_point is not None:
                        st.session_state.audio_start_time = int(next_point)
                    elif jump_points:
                        # Wrap to first
                        st.session_state.audio_start_time = int(jump_points[0])

                col_tl, col_prev, col_next = st.columns([4, 1, 1])
                with col_prev:
                    st.button(
                        "⏮ Skip to Prev",
                        on_click=skip_to_prev,
                        use_container_width=True,
                        key="skip_prev_btn",
                    )
                with col_tl:
                    st.markdown(
                        render_timeline_html(
                            episode, 70, current_time=st.session_state.audio_start_time
                        ),
                        unsafe_allow_html=True,
                    )
                with col_next:
                    st.button(
                        "Skip to Next ⏭",
                        on_click=skip_to_next,
                        use_container_width=True,
                        key="skip_next_btn",
                    )
        else:
            st.warning("Audio file not found.")

    # Two-column layout for transcript and summary
    col1, col2 = st.columns(2)
    with col1:
        st.markdown(
            """
        <div class="panel-header">📄 &ensp;Original Transcript</div>
        """,
            unsafe_allow_html=True,
        )

        transcript = episode.get("transcript", "No transcript available")

        # Display transcript in a scrollable container
        st.markdown(
            f"""
        <div class="review-panel" style="max-height: 250px; overflow-y: auto;">
            <div class="transcript-content chinese-text">{transcript}</div>
        </div>
        """,
            unsafe_allow_html=True,
        )

    with col2:
        st.markdown(
            """
        <div class="panel-header">✏️ &ensp;AI Summary (Editable)</div>
        """,
            unsafe_allow_html=True,
        )

        # Editable summary
        edited_summary = st.text_area(
            "Edit the summary below:",
            value=episode.get("summary", ""),
            height=200,
            label_visibility="collapsed",
            key="summary_editor",
        )

        # Save changes button
        if edited_summary != episode.get("summary", ""):
            if st.button("💾 Save Changes", type="secondary"):
                episode["summary"] = edited_summary
                save_episode(episode_id, episode)
                logger.info(f"Summary updated for episode {episode_id}")
                show_toast("Changes saved!", icon="✅")
                st.rerun()

    # Tagging editor
    all_cats = set()
    all_tags = set()
    all_tones = set()
    all_vibes = set()
    for e in get_all_episodes():
        all_cats.update(e.get("categories", []) or [])
        all_tags.update(e.get("tags", []) or [])
        all_tones.update(e.get("tone", []) or [])
        all_vibes.update(e.get("vibe", []) or [])

    current_cats = episode.get("categories", []) or []
    current_tags = episode.get("tags", []) or []
    current_tones = episode.get("tone", []) or []
    current_vibes = episode.get("vibe", []) or []
    current_tags_display = [f"#{t}" for t in current_tags]
    current_combined = list(
        dict.fromkeys(
            current_cats + current_tones + current_vibes + current_tags_display
        )
    )
    options = sorted(list(all_cats | all_tones | all_vibes | set(current_combined)))

    updated_combined = st.multiselect(
        "Tagging",
        options=options,
        default=current_combined,
        key="tagging_editor",
    )

    if sorted(updated_combined) != sorted(current_combined):
        new_cats = []
        new_tags = []
        new_tones = []
        new_vibes = []
        for item in updated_combined:
            if item.startswith("#"):
                new_tags.append(item[1:])
            elif item in all_cats:
                new_cats.append(item)
            elif item in all_tones:
                new_tones.append(item)
            elif item in all_vibes:
                new_vibes.append(item)
            else:
                new_tags.append(item)

        episode["categories"] = new_cats
        episode["tags"] = new_tags
        episode["tone"] = new_tones
        episode["vibe"] = new_vibes
        save_episode(episode_id, episode)
        st.rerun()

    st.markdown("---")

    # Approval workflow
    st.markdown("#### 📋 Editorial Decision")
    col_approve, col_reject, col_reset, col_delete = st.columns(4)
    with col_approve:
        if st.button("✅ Approve", type="primary", use_container_width=True):
            episode["status"] = "approved"
            episode["approved_at"] = datetime.now().isoformat()
            save_episode(episode_id, episode)
            logger.info(f"Episode approved: {episode_id}")
            show_toast("Episode approved for publication!", icon="✅")
            st.rerun()

    with col_reject:
        if st.button("❌ Reject", type="secondary", use_container_width=True):
            episode["status"] = "rejected"
            episode["rejected_at"] = datetime.now().isoformat()
            save_episode(episode_id, episode)
            logger.info(f"Episode rejected: {episode_id}")
            show_toast("Episode rejected.", icon="❌")
            st.rerun()

    with col_reset:
        if st.button("🔄 Reset to Pending", use_container_width=True):
            episode["status"] = "pending"
            save_episode(episode_id, episode)
            show_toast("Status reset to pending.", icon="🔄")
            logger.info(f"Episode status reset to pending: {episode_id}")
            st.rerun()

    with col_delete:
        if st.button("🗑️ Delete", key=f"delete_{episode_id}", use_container_width=True):
            confirm_delete(episode_id, episode.get("title", "Untitled"))

    # Show diff between original and edited summary
    st.markdown(
        """
    <div class="diff-legend" style="display: flex; align-items: center">
        <p style="font-size: 1.2rem; font-weight: 600; margin: 0; margin-right: 30px;">Summary Changes</p>
        <span class="diff-removed">Removed</span>
        <span class="diff-added">Added</span>
    </div>
    """,
        unsafe_allow_html=True,
    )

    original_summary = episode.get("original_summary", "")
    current_summary = episode.get("summary", "")
    diff_html = generate_diff_html(original_summary, current_summary)
    st.markdown(
        f"""
    <div class="diff-container">{diff_html}</div>
    """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
