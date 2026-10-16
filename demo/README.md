# Demo video

`build/video.html` is a 1920×1080 animated stage. Every animation is a pure function of time
(`seek(t)`), so frames render exactly. The material is real: `capture.py` drives the live portal's
sign-in pages in Chromium, runs Claude Code (Haiku) sessions against both MCP servers, and
screenshots the portal (into `build/img/`). Re-run it against your deployed domain to refresh the
screens.

    python demo/capture.py                      # needs the stack running + claude CLI
    python demo/render.py 0 88 part1.mp4 & python demo/render.py 88 176 part2.mp4
    # then concatenate with ffmpeg; see NARRATION.md for the voice-over
