"""
drill_tui.py — Bento Grid desktop TUI for the Oxe Protocol.

Night-mode optimized Textual interface with:
  - DALL-E Visual Bridge at center (no text labels for target chunks)
  - Pulsing blue waveform during voice/shadowing mode
  - Minimalist top dashboard with FSRS progress + mastery stats
  - Zero-reading: text only after 3 consecutive failures
  - AAA contrast, Linear Indigo (#5E6AD2) accent, Nordic Gray (#222326) bg

Usage:
    python3 drill_tui.py                Run interactive drill
    python3 drill_tui.py --session 20   Run 20-word session
"""

import math
import os
import subprocess
import sys
import time
import random
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static, Footer, ProgressBar
from textual import on
from textual.binding import Binding

from rich.text import Text
from rich.align import Align
from rich.panel import Panel
from rich.table import Table

from fsrs import Rating

from srs_engine import (
    get_next_word, get_due_words, record_review,
    get_unlocked_tier, tier_progress, TIER_LABELS, DB_PATH,
    LATENCY_THRESHOLD_MS,
)

AUDIO_DIR = Path(__file__).parent / "voca_vault" / "audios"
IMAGE_DIR = Path(__file__).parent / "voca_vault" / "images"

NATIVENESS_THRESHOLD = 85

# Carrier sentence building
INTERJECTIONS = [
    "Oxe,", "Vixe,", "Rapaz,", "Eita,", "Ô meu,", "Ave Maria,",
    "Misericórdia,", "Ô xente,", "Meu irmão,",
]
TAGS = [
    "viu!", "visse.", "tá ligado?", "meu irmão.", "rapaz.",
    "né?", "é mermo.", "sabe como é.", "acredita?",
]
LOCATIONS = [
    "no Pelourinho", "lá no Rio Vermelho", "na Barra", "em Itapuã",
    "no Candeal", "no Comércio", "na Ribeira", "na Pituba",
    "no Campo Grande", "na Liberdade", "no Bonfim",
]
CARRIER_TEMPLATES = [
    "{intj} tu sabe o que é {word}? {tag}",
    "{intj} ontem eu vi um negócio de {word} {loc}, {tag}",
    "Eu tava pensando em {word} agora mesmo, {tag}",
    "{intj} {word} é uma coisa que todo baiano conhece, {tag}",
    "Tu já ouviu falar de {word}? {tag}",
    "A gente sempre fala de {word} {loc}, {tag}",
    "{intj} sem {word} não dá pra viver, {tag}",
    "{intj} {word} é barril demais, {tag}",
]


def build_chunk(word):
    intj = random.choice(INTERJECTIONS)
    tag = random.choice(TAGS)
    loc = random.choice(LOCATIONS)
    template = random.choice(CARRIER_TEMPLATES)
    return word, template.format(intj=intj, word=word, tag=tag, loc=loc)


# ── Custom Widgets ────────────────────────────────────────────────


class MasteryDashboard(Static):
    """Minimalist top-aligned stats dashboard."""

    tier = reactive(1)
    mastery = reactive(0)
    due_count = reactive(0)
    session_count = reactive(0)
    session_correct = reactive(0)

    def render(self):
        accuracy = (
            f"{self.session_correct / self.session_count * 100:.0f}%"
            if self.session_count > 0
            else "—"
        )
        t = Table.grid(padding=(0, 3))
        t.add_row(
            Text(f"T{self.tier}", style="bold #5E6AD2"),
            Text(f"{TIER_LABELS.get(self.tier, '?')}", style="#9B9BA7"),
            Text(f"M{self.mastery}/5", style="#E8E8ED"),
            Text(f"Due {self.due_count}", style="#9B9BA7"),
            Text(f"{self.session_count} drills", style="#9B9BA7"),
            Text(f"{accuracy}", style="#4ADE80" if accuracy != "—" else "#9B9BA7"),
        )
        return t


class TierProgress(Static):
    """Vertical tier progress bars."""

    def render(self):
        progress = tier_progress()
        max_tier = get_unlocked_tier()
        lines = []
        for tier, label, mastered, total, pct in progress:
            bar_len = 10
            filled = int(pct / 100 * bar_len)
            bar = "█" * filled + "░" * (bar_len - filled)
            if tier < max_tier:
                color = "#4ADE80"
                marker = "✓"
            elif tier == max_tier:
                color = "#5E6AD2"
                marker = "→"
            else:
                color = "#484f58"
                marker = "·"
            lines.append(
                Text.assemble(
                    (f" {marker} T{tier} ", color),
                    (bar, color),
                    (f" {pct:>3.0f}%", "#9B9BA7"),
                )
            )
        result = Text("\n").join(lines)
        return result


class ImagePanel(Static):
    """Center panel — DALL-E Visual Bridge. No text labels."""

    image_path = reactive("")
    reveal_text = reactive("")

    def render(self):
        if self.reveal_text:
            # 3-failure text reveal
            return Align.center(
                Text(self.reveal_text, style="bold #f7931e"),
                vertical="middle",
            )
        if self.image_path and Path(self.image_path).exists():
            return Align.center(
                Text("🖼  Visual Bridge  🖼", style="bold #5E6AD2"),
                vertical="middle",
            )
        return Align.center(
            Text("◆", style="bold #5E6AD2 on #2A2B30"),
            vertical="middle",
        )


class LatencyMeter(Static):
    """Large latency display with color coding."""

    latency = reactive(0)
    rating_text = reactive("")

    def render(self):
        if self.latency == 0:
            return Align.center(Text("—", style="#484f58"), vertical="middle")

        if self.latency <= 600:
            color = "#4ADE80"
        elif self.latency <= 1000:
            color = "#FBBF24"
        else:
            color = "#F87171"

        t = Text.assemble(
            (f"{self.latency}", f"bold {color}"),
            ("ms\n", "#9B9BA7"),
            (self.rating_text, color),
        )
        return Align.center(t, vertical="middle")


class WaveformPanel(Widget):
    """Pulsing blue gradient waveform — rhythmic guide for syllable-timed Baiano."""

    active = reactive(False)
    flash_color = reactive("")  # "" = normal, "green" = success, "red" = fail

    DEFAULT_CSS = """
    WaveformPanel {
        height: 5;
        width: 100%;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._phase = 0.0
        self._wave_data = [0.0] * 80
        self._timer = None

    def on_mount(self):
        self._timer = self.set_interval(1 / 20, self._tick)

    def _tick(self):
        self._phase += 0.15
        if self.active:
            # Syllable-timed pulse: constant period ~200ms (5 syl/s)
            for i in range(len(self._wave_data)):
                x = i / len(self._wave_data) * math.pi * 8 + self._phase
                self._wave_data[i] = (
                    math.sin(x) * 0.6
                    + math.sin(x * 2.3) * 0.3
                    + math.sin(x * 0.7 + self._phase * 0.5) * 0.1
                )
        else:
            # Idle: gentle flat pulse
            for i in range(len(self._wave_data)):
                self._wave_data[i] = math.sin(self._phase * 0.5) * 0.15
        self.refresh()

    def render(self):
        width = self.size.width or 80
        height = 4
        blocks = " ▁▂▃▄▅▆▇█"

        # Determine colors
        if self.flash_color == "green":
            base_r, base_g, base_b = 74, 222, 128
        elif self.flash_color == "red":
            base_r, base_g, base_b = 248, 113, 105
        else:
            base_r, base_g, base_b = 94, 106, 210  # #5E6AD2

        lines = []
        for row in range(height):
            row_threshold = 1.0 - (row / height)
            chars = Text()
            for col in range(width):
                idx = int(col / width * len(self._wave_data))
                val = (self._wave_data[idx] + 1) / 2  # normalize 0-1
                if val >= row_threshold:
                    block_idx = min(int((val - row_threshold) * height * len(blocks)), len(blocks) - 1)
                    intensity = 0.4 + val * 0.6
                    r = int(base_r * intensity)
                    g = int(base_g * intensity)
                    b = int(base_b * intensity)
                    chars.append(blocks[block_idx], style=f"rgb({r},{g},{b})")
                else:
                    chars.append(" ")
            lines.append(chars)

        return Text("\n").join(lines)


class StatusLine(Static):
    """Bottom status: current state prompt."""

    status = reactive("Carregando...")

    def render(self):
        return Text(f"  {self.status}", style="#9B9BA7")


# ── Main App ──────────────────────────────────────────────────────


class OxeTUI(App):
    """Oxe Protocol — Bento Grid Desktop TUI."""

    CSS = """
    Screen {
        background: #222326;
    }

    #top-row {
        height: 3;
        layout: horizontal;
        background: #1a1b1e;
        border-bottom: solid #30363d;
    }

    #dashboard {
        width: 1fr;
        content-align: center middle;
        padding: 0 2;
    }

    #mid-row {
        height: 1fr;
        layout: horizontal;
    }

    #tier-panel {
        width: 28;
        padding: 1 1;
        background: #1a1b1e;
        border-right: solid #30363d;
    }

    #center-panel {
        width: 1fr;
        content-align: center middle;
        background: #222326;
    }

    #latency-panel {
        width: 20;
        padding: 1 1;
        background: #1a1b1e;
        border-left: solid #30363d;
        content-align: center middle;
    }

    #bottom-row {
        height: 7;
        background: #1a1b1e;
        border-top: solid #30363d;
    }

    #waveform {
        height: 5;
    }

    #status-line {
        height: 1;
        dock: bottom;
        background: #1a1b1e;
    }
    """

    BINDINGS = [
        Binding("space", "respond", "Shadow Response", show=True),
        Binding("c", "chorus", "Chorusing", show=True),
        Binding("s", "toggle_shadowing", "Shadow Mode", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    # State
    current_word = reactive(None)
    state = reactive("idle")  # idle, playing, waiting, result
    audio_end_time = 0.0
    failure_counts: dict = {}
    session_count = 0
    session_correct = 0
    session_target = 0
    shadowing_mode = False

    def compose(self) -> ComposeResult:
        with Container(id="top-row"):
            yield MasteryDashboard(id="dashboard")
        with Container(id="mid-row"):
            yield TierProgress(id="tier-panel")
            yield ImagePanel(id="center-panel")
            yield LatencyMeter(id="latency-panel")
        with Container(id="bottom-row"):
            yield WaveformPanel(id="waveform")
        yield StatusLine(id="status-line")
        yield Footer()

    def on_mount(self):
        self._update_dashboard()
        self.set_timer(0.5, self._load_next)

    def _update_dashboard(self):
        dash = self.query_one("#dashboard", MasteryDashboard)
        dash.tier = get_unlocked_tier()
        due = get_due_words()
        dash.due_count = len(list(due))
        dash.session_count = self.session_count
        dash.session_correct = self.session_correct
        if self.current_word:
            dash.mastery = self.current_word["mastery_level"]

    def _load_next(self):
        """Load the next SRS word."""
        row = get_next_word()
        if not row:
            self.query_one("#status-line", StatusLine).status = (
                "Nenhuma palavra pra revisar. Descansa, parceiro!"
            )
            return

        self.current_word = dict(row)
        word = row["word"]
        _, carrier = build_chunk(word)
        self.current_word["carrier"] = carrier

        # Update dashboard
        self._update_dashboard()

        # Reset image panel (no text — zero-reading)
        img = self.query_one("#center-panel", ImagePanel)
        img.reveal_text = ""
        img.image_path = str(IMAGE_DIR / f"word_{row['id']}.png")

        # Reset latency
        lat = self.query_one("#latency-panel", LatencyMeter)
        lat.latency = 0
        lat.rating_text = ""

        # Refresh tier progress
        self.query_one("#tier-panel", TierProgress).refresh()

        # Activate waveform
        wf = self.query_one("#waveform", WaveformPanel)
        wf.active = True
        wf.flash_color = ""

        self.state = "playing"
        self.query_one("#status-line", StatusLine).status = (
            "Ouvindo... (SPACE para responder)"
        )

        # Play audio if available
        audio_path = AUDIO_DIR / f"word_{row['id']}.mp3"
        if audio_path.exists():
            self.run_worker(self._play_audio(str(audio_path)))
        else:
            # No audio file — go straight to waiting
            self._on_audio_done()

    async def _play_audio(self, path):
        """Play audio in background worker."""
        proc = subprocess.Popen(
            ["afplay", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait()
        self.call_from_thread(self._on_audio_done)

    def _on_audio_done(self):
        """Audio finished playing — start latency timer."""
        self.audio_end_time = time.monotonic()
        self.state = "waiting"
        self.query_one("#status-line", StatusLine).status = (
            "SHADOW → press SPACE"
        )

    def action_respond(self):
        """Handle shadow response (spacebar)."""
        if self.state != "waiting":
            return

        latency_ms = int((time.monotonic() - self.audio_end_time) * 1000)
        self.state = "result"

        word_id = self.current_word["id"]
        word = self.current_word["word"]

        # Determine rating
        if latency_ms <= 600:
            rating = Rating.Easy
        elif latency_ms <= LATENCY_THRESHOLD_MS:
            rating = Rating.Good
        elif latency_ms <= 2000:
            rating = Rating.Hard
        else:
            rating = Rating.Again

        # Record review
        card, new_mastery, downgraded = record_review(word_id, rating, latency_ms)
        rating_name = {1: "De novo", 2: "Difícil", 3: "Bom", 4: "Fácil"}[rating.value]

        # Update latency display
        lat = self.query_one("#latency-panel", LatencyMeter)
        lat.latency = latency_ms
        lat.rating_text = rating_name

        # Waveform flash
        wf = self.query_one("#waveform", WaveformPanel)
        wf.active = False
        if rating.value >= Rating.Good.value:
            wf.flash_color = "green"
            self.session_correct += 1
        else:
            wf.flash_color = "red"

        self.session_count += 1

        # 3-failure text reveal (zero-reading enforcement)
        if rating == Rating.Again:
            self.failure_counts[word_id] = self.failure_counts.get(word_id, 0) + 1
            if self.failure_counts[word_id] >= 3:
                img = self.query_one("#center-panel", ImagePanel)
                img.reveal_text = self.current_word["carrier"]
                self.failure_counts[word_id] = 0
        else:
            self.failure_counts[word_id] = 0

        # Status
        status = f"{latency_ms}ms — {rating_name}"
        if downgraded:
            status += " (>1s → auto-Hard + recursive explanation needed)"
        status += f" | Mastery: {new_mastery}/5"
        self.query_one("#status-line", StatusLine).status = status

        self._update_dashboard()

        # Auto-advance after delay
        if self.session_target > 0 and self.session_count >= self.session_target:
            self.set_timer(2.0, self._session_complete)
        else:
            self.set_timer(2.0, self._load_next)

    def _session_complete(self):
        accuracy = (
            f"{self.session_correct / self.session_count * 100:.0f}%"
            if self.session_count > 0
            else "—"
        )
        self.query_one("#status-line", StatusLine).status = (
            f"Sessão completa: {self.session_count} drills, {accuracy} accuracy. "
            f"Press Q to quit."
        )
        wf = self.query_one("#waveform", WaveformPanel)
        wf.active = False
        wf.flash_color = ""

    def action_chorus(self):
        """Trigger chorusing drill on current word."""
        if not self.current_word:
            return
        word = self.current_word["word"]
        word_id = self.current_word["id"]
        audio_path = AUDIO_DIR / f"word_{word_id}.mp3"

        self.query_one("#status-line", StatusLine).status = (
            f"Chorusing: '{word}' — listen and speak AT THE SAME TIME"
        )
        wf = self.query_one("#waveform", WaveformPanel)
        wf.active = True
        wf.flash_color = ""

        if audio_path.exists():
            self.run_worker(self._chorus_sequence(str(audio_path)))

    async def _chorus_sequence(self, path):
        """Play chorus at 0.75x then 1.0x."""
        proc = subprocess.Popen(
            ["afplay", "-r", "0.75", path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        proc.wait()
        import asyncio
        await asyncio.sleep(0.5)
        proc = subprocess.Popen(
            ["afplay", path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        proc.wait()
        self.call_from_thread(self._on_audio_done)

    def action_toggle_shadowing(self):
        """Toggle shadowing mode."""
        self.shadowing_mode = not self.shadowing_mode
        mode = "ON" if self.shadowing_mode else "OFF"
        self.query_one("#status-line", StatusLine).status = (
            f"Shadowing mode: {mode}"
        )
        wf = self.query_one("#waveform", WaveformPanel)
        wf.active = self.shadowing_mode

    def action_quit(self):
        self.exit()


def main():
    session_target = 0
    if "--session" in sys.argv:
        idx = sys.argv.index("--session")
        if idx + 1 < len(sys.argv):
            session_target = int(sys.argv[idx + 1])

    app = OxeTUI()
    app.session_target = session_target
    app.run()


if __name__ == "__main__":
    main()
