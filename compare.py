import streamlit as st
import diff_match_patch
from storage import get_all_episodes


def generate_diff_html(original, edited):
    """Generate HTML highlighting differences between original and edited text using diff_match_patch."""
    if not original or not edited:
        return '<p class="no-changes">No comparison available</p>'

    if original == edited:
        return '<p class="no-changes">No changes made - summary is identical to original</p>'

    dmp = diff_match_patch.diff_match_patch()
    diffs = dmp.diff_main(original, edited)
    dmp.diff_cleanupSemantic(diffs)

    html = []
    for op, data in diffs:
        import html as html_esc

        safe_data = html_esc.escape(data).replace("\n", "<br>")

        if op == dmp.DIFF_INSERT:
            html.append(f'<span class="diff-added">{safe_data}</span>')
        elif op == dmp.DIFF_DELETE:
            html.append(f'<span class="diff-removed">{safe_data}</span>')
        elif op == dmp.DIFF_EQUAL:
            html.append(safe_data)

    return "".join(html)


def render_timeline_html(
    episode, timeline_width=80, show_stats=False, current_time=None
):
    """Generate HTML visualization of music segments using episode data."""
    segments = episode.get("music_segments")
    music_duration = episode.get("music_duration", 0)
    total_duration = episode.get("duration_seconds", 1)
    if total_duration == 0:
        total_duration = 1

    if segments is None:
        return ""

    def fmt_time(s):
        return f"{int(s // 60):02}:{int(s % 60):02}"

    # CSS-based timeline
    html_parts = []

    # Track background
    html_parts.append(
        '<div style="position: relative; width: 100%; height: 12px; background-color: #e0e0e0; border-radius: 6px; margin: 10px 0; overflow: hidden;">'
    )

    for seg in segments:
        start = seg["start"]
        end = seg["end"]
        if end > total_duration:
            end = total_duration

        left_pct = (start / total_duration) * 100
        width_pct = ((end - start) / total_duration) * 100

        html_parts.append(
            f'<div style="position: absolute; left: {left_pct:.2f}%; width: {width_pct:.2f}%; height: 100%; background-color: #3C9FD8; opacity: 0.8;" title="Music: {fmt_time(start)} - {fmt_time(end)}"></div>'
        )

    if current_time is not None:
        pos_pct = (current_time / total_duration) * 100
        html_parts.append(
            f'<div style="position: absolute; left: {pos_pct:.2f}%; top: 0; bottom: 0; width: 4px; background-color: #FF4B4B; z-index: 10;" title="Current: {fmt_time(current_time)}"></div>'
        )

    html_parts.append("</div>")

    # Calculate stats
    pct = (music_duration / total_duration) * 100
    stats_html = f"<div style='font-size: 0.8em; color: #555; white-space: nowrap;'>{fmt_time(music_duration)} / {fmt_time(total_duration)} ({pct:.1f}%)</div>"

    html = "".join(html_parts)
    if show_stats:
        html += stats_html
    return html


def render_comparison():
    """Render the comparison page."""
    episodes = get_all_episodes()

    # Find base versions (earliest created_at) for comparison and map titles
    base_version_map = {}
    for e in episodes:
        fname = e.get("original_filename")
        if not fname:
            continue

        # Track base version (earliest)
        if fname not in base_version_map or e.get("created_at", "") < base_version_map[
            fname
        ].get("created_at", ""):
            base_version_map[fname] = e

    # Filter by original_filename
    filenames = sorted(list(base_version_map.keys()))

    # Find latest filename for default selection
    default_index = 0
    for e in episodes:
        fname = e.get("original_filename")
        if fname and fname in filenames:
            default_index = filenames.index(fname) + 1
            break

    def format_option(option):
        if option is None:
            return "Select a file..."
        else:
            return f"{option} . {base_version_map[option].get('title', '')} . {base_version_map[option].get('id', '')}"

    selected_filename = st.selectbox(
        "Filter by Base filename",
        [None] + filenames,
        format_func=format_option,
        index=default_index,
    )
    if not selected_filename:
        return

    episodes = [e for e in episodes if e.get("original_filename") == selected_filename]
    for episode in episodes:
        with st.container():
            fname = episode.get("original_filename")
            base_ver = base_version_map.get(fname)
            is_base = base_ver and base_ver["id"] == episode["id"]

            if is_base or not base_ver:
                content_html = f'<div class="chinese-text" style="white-space: pre-wrap;">{episode.get("summary", "")}</div>'
            else:
                diff_html = generate_diff_html(
                    base_ver.get("summary", ""), episode.get("summary", "")
                )
                content_html = f'<div class="diff-container">{diff_html}</div>'

            # Review button HTML
            review_url = f"?page=review&id={episode['id']}"
            # Style <a> tag directly to avoid nesting issues and layout breaks
            review_btn_html = f'''<a href="{review_url}" target="_self" 
            style="background-color: #00a0e3; color: white; text-decoration: none; padding: 4px 12px; border-radius: 4px; font-size: 0.8em; display: inline-block; margin-left: auto; white-space: nowrap;">Review</a>'''

            st.markdown(
                f"""
            <div class="episode-card">
                <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 8px; font-weight: bold; color: #555;">
                    <div>{episode["id"]}</div>
                    {render_timeline_html(episode, 60, show_stats=True)}
                    {review_btn_html}
                </div>
                {content_html}
            </div>
            """,
                unsafe_allow_html=True,
            )
