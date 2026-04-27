from __future__ import annotations

from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium
from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "output" / "pdf"
TMP_DIR = ROOT / "tmp" / "pdfs"
PDF_PATH = OUTPUT_DIR / "loopscribe_app_summary.pdf"
PNG_PATH = TMP_DIR / "loopscribe_app_summary_page.png"

PAGE_WIDTH, PAGE_HEIGHT = LETTER
MARGIN = 36
GAP = 18
LEFT_X = MARGIN
LEFT_W = 332
RIGHT_X = LEFT_X + LEFT_W + GAP
RIGHT_W = PAGE_WIDTH - MARGIN - RIGHT_X

TITLE = "LoopScribe"
SUBTITLE = "One-page summary based on repo evidence"

WHAT_IS = (
    "LoopScribe is a local Windows desktop app that records system audio with "
    "WASAPI loopback or imports existing audio files and transcribes them with "
    "faster-whisper. It stores recordings and transcripts on disk and exposes "
    "the workflow through a PySide6 GUI with hotkeys."
)

WHO_FOR = (
    "Primary persona: a Windows user who needs quick local transcription for "
    "played audio, meetings, calls, or saved audio files."
)

FEATURES = [
    "Records Windows system audio through WASAPI loopback.",
    "Supports microphone-only and combined system-audio-plus-microphone capture.",
    "Offers global start and stop hotkeys plus push-to-transcribe.",
    "Imports .wav, .mp3, .m4a, .flac, .ogg, and .aac audio files.",
    "Runs local faster-whisper transcription with selectable language, model, and performance profile.",
    "Shows transcription progress and an editable transcript, with clipboard copy support.",
    "Auto-saves transcript files and exports to .txt, .srt, or .vtt.",
]

ARCHITECTURE_BOXES = [
    (
        "UI + settings",
        "PySide6 MainWindow loads SettingsStore, builds the desktop UI, and wires buttons and hotkeys.",
    ),
    (
        "Capture",
        "AudioCaptureService records to WAV via soundcard and soundfile using WASAPI loopback, microphone, or both.",
    ),
    (
        "Transcribe",
        "TranscriptionWorker runs FastTranscriptionService, which calls faster-whisper locally and caches models.",
    ),
    (
        "Outputs",
        "Transcript text is saved under transcripts/, shown in the editor, then copied to clipboard or exported.",
    ),
]

RUN_STEPS = [
    "Windows 10/11 and Python 3.10+.",
    "python -m venv .venv",
    ".venv\\Scripts\\Activate.ps1",
    "python -m pip install -e .",
    "python -m recorder_app.main  or  start.bat",
]

FOOTNOTES = [
    "Remote backend/API: Not found in repo.",
    "Database/auth service: Not found in repo.",
    "First transcription downloads the selected Whisper model.",
]


def draw_card(pdf: canvas.Canvas, x: float, top_y: float, width: float, height: float, fill: colors.Color) -> None:
    pdf.setFillColor(fill)
    pdf.setStrokeColor(colors.HexColor("#D7DEE8"))
    pdf.roundRect(x, top_y - height, width, height, 12, fill=1, stroke=1)


def draw_section_label(pdf: canvas.Canvas, x: float, y: float, text: str) -> float:
    pdf.setFillColor(colors.HexColor("#0B5A6A"))
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(x, y, text.upper())
    return y - 16


def draw_paragraph(
    pdf: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    width: float,
    *,
    font_name: str = "Helvetica",
    font_size: float = 9.5,
    leading: float = 12,
    color: colors.Color = colors.HexColor("#182430"),
) -> float:
    lines = simpleSplit(text, font_name, font_size, width)
    pdf.setFillColor(color)
    pdf.setFont(font_name, font_size)
    for line in lines:
        pdf.drawString(x, y, line)
        y -= leading
    return y


def draw_bullets(
    pdf: canvas.Canvas,
    items: list[str],
    x: float,
    y: float,
    width: float,
    *,
    font_size: float = 9.4,
    leading: float = 11.5,
) -> float:
    pdf.setFillColor(colors.HexColor("#182430"))
    pdf.setFont("Helvetica", font_size)
    text_width = width - 14
    for item in items:
        lines = simpleSplit(item, "Helvetica", font_size, text_width)
        if not lines:
            continue
        pdf.drawString(x, y, "-")
        pdf.drawString(x + 10, y, lines[0])
        y -= leading
        for line in lines[1:]:
            pdf.drawString(x + 10, y, line)
            y -= leading
        y -= 3
    return y


def draw_flow_box(
    pdf: canvas.Canvas,
    x: float,
    top_y: float,
    width: float,
    height: float,
    title: str,
    body: str,
) -> None:
    pdf.setFillColor(colors.white)
    pdf.setStrokeColor(colors.HexColor("#BDD2DA"))
    pdf.roundRect(x, top_y - height, width, height, 10, fill=1, stroke=1)

    pdf.setFillColor(colors.HexColor("#0B5A6A"))
    pdf.setFont("Helvetica-Bold", 9.4)
    pdf.drawString(x + 10, top_y - 16, title)

    draw_paragraph(
        pdf,
        body,
        x + 10,
        top_y - 30,
        width - 20,
        font_name="Helvetica",
        font_size=8.3,
        leading=10,
    )


def build_pdf() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    pdf = canvas.Canvas(str(PDF_PATH), pagesize=LETTER)
    pdf.setTitle("LoopScribe App Summary")
    pdf.setAuthor("OpenAI Codex")

    pdf.setFillColor(colors.HexColor("#F3F7FA"))
    pdf.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, fill=1, stroke=0)

    header_height = 88
    pdf.setFillColor(colors.HexColor("#0F596A"))
    pdf.rect(0, PAGE_HEIGHT - header_height, PAGE_WIDTH, header_height, fill=1, stroke=0)

    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 24)
    pdf.drawString(MARGIN, PAGE_HEIGHT - 40, TITLE)
    pdf.setFont("Helvetica", 10.5)
    pdf.drawString(MARGIN, PAGE_HEIGHT - 58, SUBTITLE)
    pdf.setFont("Helvetica", 9)
    pdf.drawRightString(
        PAGE_WIDTH - MARGIN,
        PAGE_HEIGHT - 56,
        "Repo basis: README.md, pyproject.toml, src/recorder_app/*.py",
    )

    left_top = PAGE_HEIGHT - 110
    left_height = 632
    right_top = left_top

    draw_card(pdf, LEFT_X, left_top, LEFT_W, left_height, colors.white)
    draw_card(pdf, RIGHT_X, right_top, RIGHT_W, 404, colors.HexColor("#EDF5F7"))
    draw_card(pdf, RIGHT_X, right_top - 420, RIGHT_W, 145, colors.white)

    x = LEFT_X + 18
    y = left_top - 22
    section_width = LEFT_W - 36

    y = draw_section_label(pdf, x, y, "What it is")
    y = draw_paragraph(pdf, WHAT_IS, x, y, section_width)
    y -= 8

    y = draw_section_label(pdf, x, y, "Who it is for")
    y = draw_paragraph(pdf, WHO_FOR, x, y, section_width)
    y -= 8

    y = draw_section_label(pdf, x, y, "What it does")
    y = draw_bullets(pdf, FEATURES, x, y, section_width)
    y -= 2

    y = draw_section_label(pdf, x, y, "Repo notes")
    y = draw_bullets(pdf, FOOTNOTES, x, y, section_width, font_size=9.0, leading=11)

    rx = RIGHT_X + 16
    ry = right_top - 22
    right_inner_w = RIGHT_W - 32

    ry = draw_section_label(pdf, rx, ry, "How it works")
    pdf.setFillColor(colors.HexColor("#39515A"))
    pdf.setFont("Helvetica", 8.4)
    pdf.drawString(rx, ry, "Compact architecture and data flow:")
    ry -= 18

    box_height = 68
    gap = 12
    for index, (title, body) in enumerate(ARCHITECTURE_BOXES):
        top_y = ry - index * (box_height + gap)
        draw_flow_box(pdf, rx, top_y, right_inner_w, box_height, title, body)
        if index < len(ARCHITECTURE_BOXES) - 1:
            center_x = rx + (right_inner_w / 2)
            arrow_start = top_y - box_height - 3
            arrow_end = arrow_start - 8
            pdf.setStrokeColor(colors.HexColor("#6A8791"))
            pdf.setLineWidth(1)
            pdf.line(center_x, arrow_start, center_x, arrow_end)
            pdf.line(center_x, arrow_end, center_x - 3, arrow_end + 4)
            pdf.line(center_x, arrow_end, center_x + 3, arrow_end + 4)

    note_y = ry - len(ARCHITECTURE_BOXES) * (box_height + gap) + 16
    draw_paragraph(
        pdf,
        "Observed flow: hotkey/button -> WAV/import -> local transcription -> transcript/export.",
        rx,
        note_y,
        right_inner_w,
        font_name="Helvetica-Oblique",
        font_size=7.6,
        leading=9,
        color=colors.HexColor("#536770"),
    )

    ry2 = right_top - 442
    ry2 = draw_section_label(pdf, rx, ry2, "How to run")
    pdf.setFillColor(colors.HexColor("#39515A"))
    pdf.setFont("Helvetica", 8.6)
    pdf.drawString(rx, ry2, "Minimal getting-started path:")
    ry2 -= 16
    for index, step in enumerate(RUN_STEPS, start=1):
        lines = simpleSplit(step, "Helvetica", 8.8, right_inner_w - 18)
        pdf.setFillColor(colors.HexColor("#182430"))
        pdf.setFont("Helvetica-Bold", 8.8)
        pdf.drawString(rx, ry2, f"{index}.")
        pdf.setFont("Helvetica", 8.8)
        pdf.drawString(rx + 14, ry2, lines[0])
        ry2 -= 11
        for line in lines[1:]:
            pdf.drawString(rx + 14, ry2, line)
            ry2 -= 11
        ry2 -= 2

    pdf.setFillColor(colors.HexColor("#70838B"))
    pdf.setFont("Helvetica", 7.8)
    pdf.drawRightString(
        PAGE_WIDTH - MARGIN,
        20,
        "Single-page project summary generated from repo evidence only",
    )

    pdf.showPage()
    pdf.save()


def validate_pdf() -> None:
    reader = PdfReader(str(PDF_PATH))
    if len(reader.pages) != 1:
        raise RuntimeError(f"Expected 1 page, found {len(reader.pages)} pages.")

    with pdfplumber.open(str(PDF_PATH)) as doc:
        if len(doc.pages) != 1:
            raise RuntimeError(f"pdfplumber saw {len(doc.pages)} pages instead of 1.")
        extracted = (doc.pages[0].extract_text() or "").strip()
        if "LoopScribe" not in extracted:
            raise RuntimeError("Validation failed: expected title text was not extracted.")


def render_preview() -> None:
    document = pdfium.PdfDocument(str(PDF_PATH))
    page = document[0]
    bitmap = page.render(scale=2.0)
    image = bitmap.to_pil()
    image.save(PNG_PATH)
    page.close()
    document.close()


def main() -> None:
    build_pdf()
    validate_pdf()
    render_preview()
    print(f"PDF: {PDF_PATH}")
    print(f"Preview: {PNG_PATH}")


if __name__ == "__main__":
    main()
