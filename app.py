import base64
import hashlib
import json
import pathlib

import requests
import streamlit as st
import streamlit.components.v1 as components


# =========================================================
# PAGE CONFIG
# =========================================================

st.set_page_config(
    page_title="PDF Infographic Generator",
    page_icon="📄",
    layout="wide",
)


# =========================================================
# N8N WEBHOOKS
#
# Read from .streamlit/secrets.toml when present, so the URLs
# stay out of the public repo. The literals below are only a
# local fallback.
#
# For n8n TEST mode, change "webhook" to "webhook-test" and
# click Execute workflow in n8n before every request.
# =========================================================

N8N_BASE = "https://abeersalman7979.app.n8n.cloud/webhook"


def secret(name, fallback):
    """Read a secret, falling back when no secrets file exists.

    Streamlit raises rather than returning a default when there is
    no secrets.toml at all, which is the normal case when running
    the app locally.
    """
    try:
        return st.secrets.get(name, fallback)
    except Exception:
        return fallback


ANALYZE_URL = secret("ANALYZE_URL", f"{N8N_BASE}/pdf-analyze")
BUILD_URL = secret("BUILD_URL", f"{N8N_BASE}/pdf-infographic")
RESTYLE_URL = secret("RESTYLE_URL", f"{N8N_BASE}/restyle-infographic")
ASK_URL = secret("ASK_URL", f"{N8N_BASE}/ask-pdf")

ANALYZE_TIMEOUT = 180
BUILD_TIMEOUT = 240
RESTYLE_TIMEOUT = 60
ASK_TIMEOUT = 120

MAX_UPLOAD_MB = 20


# =========================================================
# PDF EXPORT ENGINE
#
# WeasyPrint needs system libraries. On Streamlit Community
# Cloud those come from packages.txt. If it is unavailable the
# app falls back to a browser print button, so the PDF option
# never simply disappears.
# =========================================================

from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors as rl_colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether,
)
from xml.sax.saxutils import escape as xml_escape


# =========================================================
# TEMPLATES
#
# These labels and descriptions are what the user picks from.
# The keys must match the TEMPLATES table in the n8n
# Infographic Builder node exactly.
# =========================================================

TEMPLATES = {
    "editorial": {
        "label": "Report",
        "blurb": "A clean article layout. The safe choice for most documents.",
        "best": "Reports, articles, anything general",
        # palette and block order, kept in step with the n8n builder
        "ink": "#10131c", "paper": "#fbfaf7", "accent": "#d8f35c",
        "rule": "#c9c6bc", "muted": "#57554e", "dark": False,
        "order": ["stats", "sections", "timeline", "takeaways"],
    },
    "poster": {
        "label": "Big Poster",
        "blurb": "Large bold type. The key points go at the top.",
        "best": "Announcements, pitches, one-page summaries",
        "ink": "#141414", "paper": "#ffffff", "accent": "#ff5a3c",
        "rule": "#d4d4d4", "muted": "#5a5a5a", "dark": False,
        "order": ["takeaways", "stats", "sections", "timeline"],
    },
    "brief": {
        "label": "Plain Text",
        "blurb": "Quiet and simple. No colour, no highlights, easy to read.",
        "best": "Academic papers, legal text, dense writing",
        "ink": "#22201d", "paper": "#ffffff", "accent": "#a8a49b",
        "rule": "#e6e4de", "muted": "#6b675f", "dark": False,
        "order": ["sections", "stats", "timeline", "takeaways"],
    },
    "dashboard": {
        "label": "Numbers First",
        "blurb": "Big figures at the top, on a cool blue-grey background.",
        "best": "Financial reports, surveys, anything with figures",
        "ink": "#0a1628", "paper": "#f4f7fb", "accent": "#2dd4bf",
        "rule": "#ccd6e2", "muted": "#4a5a70", "dark": False,
        "order": ["stats", "sections", "timeline", "takeaways"],
    },
    "timeline": {
        "label": "Timeline",
        "blurb": "Dated events on a vertical line, shown before everything else.",
        "best": "Histories, project reviews, case studies",
        "ink": "#16161d", "paper": "#f6f5f2", "accent": "#5b6ef5",
        "rule": "#dbd9d2", "muted": "#5c5a63", "dark": False,
        "order": ["timeline", "sections", "stats", "takeaways"],
    },
    "presentation": {
        "label": "Moving Presentation",
        "blurb": "Not a page. It zooms from point to point as you press the arrow keys.",
        "best": "Presenting to an audience, demos",
        "ink": "#f2eee7", "paper": "#171426", "accent": "#ffb454",
        "rule": "#332d4d", "muted": "#a49bbd", "dark": True,
        "order": ["stats", "sections", "timeline", "takeaways"],
    },
    "blueprint": {
        "label": "Dark Technical",
        "blurb": "Dark navy background with bright labelling.",
        "best": "Specifications, engineering documents, manuals",
        "ink": "#dfe7ef", "paper": "#0d1b2a", "accent": "#4cc9f0",
        "rule": "#20384f", "muted": "#8ba3bb", "dark": True,
        "order": ["sections", "stats", "timeline", "takeaways"],
    },
}


DEPTHS = {
    "Brief — 2 to 3 sections": "brief",
    "Standard — 3 to 4 sections": "standard",
    "Detailed — 5 to 6 sections": "detailed",
}

AUDIENCES = {
    "General reader": "general",
    "Student": "student",
    "Executive": "executive",
    "Technical specialist": "technical",
}

LANGUAGES = {
    "English": "english",
    "العربية  (Arabic)": "arabic",
}

# Changing any of these needs a new OpenAI call.
CONTENT_KEYS = ("depth", "audience", "language")



# =========================================================
# PDF EXPORT
#
# Built directly from the structured content with ReportLab,
# not by printing the web page. That means the download is a
# real, instant PDF file with no browser dialog, and it uses
# the same palette as the template on screen.
#
# ReportLab is pure Python, so nothing has to be installed at
# the operating system level.
# =========================================================

# ------------------------------------------------------------
# Arabic support for the PDF
#
# Three things are needed and all three degrade quietly:
#   a font with Arabic glyphs   (fonts/DejaVuSans.ttf in the repo)
#   arabic_reshaper             (joins the letters)
#   python-bidi                 (lays the line out right to left)
# Without them the PDF still builds, just without Arabic.
# ------------------------------------------------------------

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    SHAPING = True
except Exception:
    SHAPING = False

APP_DIR = pathlib.Path(__file__).parent

# The fonts work from a fonts/ folder or from the repo root, so it
# does not matter which way they were uploaded.
FONT_DIRS = [APP_DIR / "fonts", APP_DIR, APP_DIR / "assets"]


def find_font(filename):
    for folder in FONT_DIRS:
        candidate = folder / filename
        if candidate.exists():
            return candidate
    return None


ARABIC_FONT = None
ARABIC_FONT_BOLD = None

try:
    regular = find_font("DejaVuSans.ttf")
    bold = find_font("DejaVuSans-Bold.ttf")

    if regular:
        pdfmetrics.registerFont(TTFont("ArabicBody", str(regular)))
        ARABIC_FONT = "ArabicBody"

        if bold:
            pdfmetrics.registerFont(TTFont("ArabicBold", str(bold)))
            ARABIC_FONT_BOLD = "ArabicBold"
        else:
            ARABIC_FONT_BOLD = "ArabicBody"
except Exception:
    ARABIC_FONT = None
    ARABIC_FONT_BOLD = None


ARABIC_PDF_READY = bool(ARABIC_FONT and SHAPING)


def has_arabic(value):
    return any("\u0600" <= ch <= "\u06ff" for ch in str(value or ""))


def shape_arabic(text, font, size, max_width):
    """Join the letters, wrap, then flip each line.

    Wrapping has to happen before the flip. Flipping the whole
    paragraph first and letting the PDF wrap it afterwards puts
    the lines in reverse order, which reads as nonsense.
    """
    shaped = arabic_reshaper.reshape(str(text))

    # Break a little earlier than the true width. If ReportLab ever
    # has to wrap a line that has already been flipped, it splits it
    # left to right and the words come out in the wrong order.
    limit = max(40, max_width - 10)

    lines, current = [], ""

    for word in shaped.split(" "):
        trial = (current + " " + word).strip()
        fits = pdfmetrics.stringWidth(trial, font, size) <= limit
        if not current or fits:
            current = trial
        else:
            lines.append(current)
            current = word

    if current:
        lines.append(current)

    return "<br/>".join(get_display(line) for line in lines)


MOJIBAKE = [
    ("\u00e2\u20ac\u2122", "'"), ("\u00e2\u20ac\u02dc", "'"),
    ("\u00e2\u20ac\u009c", '"'), ("\u00e2\u20ac\u009d", '"'),
    ("\u00e2\u20ac\u0153", '"'), ("\u00e2\u20ac\u201d", "\u2014"),
    ("\u00e2\u20ac\u201c", "\u2013"), ("\u00e2\u20ac\u00a2", "\u2022"),
    ("\u00e2\u20ac\u00a6", "\u2026"), ("\u00e2\u20ac", '"'),
    ("\u00c2\u00a0", " "), ("\u00c3\u00a9", "\u00e9"),
    ("\u00c3\u00a8", "\u00e8"), ("\u00c3\u00a1", "\u00e1"),
    ("\u00c3\u00b3", "\u00f3"), ("\u00c3\u00bc", "\u00fc"),
]


def clean_text(value):
    """Repair the mojibake that PDF text extraction sometimes produces."""
    s = "" if value is None else str(value)
    if any(m in s for m in ("\u00e2", "\u00c2", "\u00c3")):
        for bad, good in MOJIBAKE:
            s = s.replace(bad, good)
    return s.strip()


def _p(text, style, width=None):
    """Build a paragraph, shaping and flipping it when it is Arabic."""

    cleaned = clean_text(text)

    if ARABIC_PDF_READY and has_arabic(cleaned) and width:
        try:
            body = shape_arabic(
                xml_escape(cleaned), style.fontName, style.fontSize, width
            )
            return Paragraph(body, style)
        except Exception:
            pass

    return Paragraph(xml_escape(cleaned), style)


def build_pdf(data, template_key):
    """Render the infographic content as a real PDF. Returns bytes."""

    t = TEMPLATES.get(template_key, TEMPLATES["editorial"])

    ink = rl_colors.HexColor(t["ink"])
    paper = rl_colors.HexColor(t["paper"])
    accent = rl_colors.HexColor(t["accent"])
    rule = rl_colors.HexColor(t["rule"])
    muted = rl_colors.HexColor(t["muted"])

    margin = 18 * mm
    page_w, page_h = A4
    content_w = page_w - 2 * margin

    # Is this document Arabic? If so the whole page flips.
    arabic = has_arabic(
        " ".join([
            str(data.get("title") or ""),
            str(data.get("subtitle") or ""),
            str(data.get("summary") or ""),
        ])
    )

    use_arabic = arabic and ARABIC_PDF_READY

    body_font = ARABIC_FONT if use_arabic else "Helvetica"
    bold_font = ARABIC_FONT_BOLD if use_arabic else "Helvetica-Bold"

    # 2 is right aligned, which is where Arabic text starts
    align = 2 if use_arabic else 0

    def style(name, **kw):
        base = dict(name=name, fontName=body_font, fontSize=10,
                    leading=15, textColor=ink, alignment=align)
        if kw.get("fontName") == "Helvetica-Bold":
            kw["fontName"] = bold_font
        base.update(kw)
        return ParagraphStyle(**base)

    s_meta = style("meta", fontSize=8.5, leading=12, textColor=muted)
    s_title = style("title", fontName="Helvetica-Bold", fontSize=26,
                    leading=30)
    s_sub = style("sub", fontSize=13, leading=18, textColor=muted)
    s_body = style("body", fontSize=10.5, leading=16)
    s_head = style("head", fontName="Helvetica-Bold", fontSize=15,
                   leading=19, spaceBefore=4, spaceAfter=8)
    s_note_h = style("noteh", fontName="Helvetica-Bold", fontSize=11,
                     leading=14)
    s_stat_v = style("statv", fontName="Helvetica-Bold", fontSize=21,
                     leading=24)
    s_stat_l = style("statl", fontSize=9, leading=12)
    s_cite = style("cite", fontSize=8, leading=11, textColor=muted)
    s_date = style("date", fontName="Helvetica-Bold", fontSize=9, leading=13)

    HEAD_TIMELINE = "\u0627\u0644\u062a\u0633\u0644\u0633\u0644 \u0627\u0644\u0632\u0645\u0646\u064a" if arabic else "How it unfolded"
    HEAD_TAKEAWAYS = "\u0623\u0628\u0631\u0632 \u0627\u0644\u0646\u0642\u0627\u0637" if arabic else "What to remember"

    flow = []

    # ---------- masthead ----------

    meta = data.get("meta") or {}
    bits = []
    if meta.get("page_count"):
        bits.append(f"{meta['page_count']} pages")
    if meta.get("word_count"):
        bits.append(f"{meta['word_count']:,} words")

    if meta.get("filename"):
        line = clean_text(meta["filename"])
        if bits:
            line += " - " + ", ".join(bits)
        flow.append(_p(line, s_meta, content_w))
        flow.append(Spacer(1, 5 * mm))

    flow.append(_p(data.get("title") or "Untitled", s_title, content_w))
    flow.append(Spacer(1, 2.5 * mm))
    flow.append(HRFlowable(width="38%", thickness=3, color=accent,
                           spaceAfter=6,
                           hAlign="RIGHT" if use_arabic else "LEFT"))

    if data.get("subtitle"):
        flow.append(Spacer(1, 2 * mm))
        flow.append(_p(data["subtitle"], s_sub, content_w))

    flow.append(Spacer(1, 6 * mm))
    flow.append(HRFlowable(width="100%", thickness=0.8, color=ink,
                           spaceAfter=8))

    if data.get("summary"):
        flow.append(_p(data["summary"], s_body, content_w))
        flow.append(Spacer(1, 7 * mm))

    # ---------- blocks ----------

    def block_stats():
        stats = data.get("key_stats") or []
        if not stats:
            return []

        out = []
        cols = min(len(stats), 3)
        cell_w = content_w / cols

        for start in range(0, len(stats), cols):
            row = stats[start:start + cols]
            cells = []
            for st in row:
                value = clean_text(st.get("value"))
                unit = clean_text(st.get("unit"))
                inner = [
                    _p(f"{value} {unit}".strip(), s_stat_v, cell_w - 16),
                    Spacer(1, 1.5 * mm),
                    _p(st.get("label"), s_stat_l, cell_w - 16),
                    Spacer(1, 1.5 * mm),
                    _p(st.get("source_quote"), s_cite, cell_w - 16),
                ]
                cells.append(inner)
            while len(cells) < cols:
                cells.append([Spacer(1, 1)])

            tbl = Table([cells], colWidths=[cell_w] * cols)
            tbl.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEABOVE", (0, 0), (-1, 0), 0.8, ink),
                ("LINEBELOW", (0, 0), (-1, -1), 0.8, ink),
                ("LINEBEFORE", (1, 0), (-1, -1), 0.4, rule),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
                ("LEFTPADDING", (0, 0), (0, -1), 0),
                ("LEFTPADDING", (1, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ]))
            out.append(tbl)
            out.append(Spacer(1, 5 * mm))

        return out

    def block_sections():
        sections = data.get("sections") or []
        if not sections:
            return []

        rows = []
        for sec in sections:
            cells = [
                _p(sec.get("heading"), s_note_h, 44 * mm),
                _p(sec.get("text"), s_body, content_w - 50 * mm),
            ]
            # Arabic reads right to left, so the heading column moves
            rows.append(cells[::-1] if use_arabic else cells)

        widths = [46 * mm, content_w - 46 * mm]
        tbl = Table(rows, colWidths=widths[::-1] if use_arabic else widths)
        tbl.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEABOVE", (0, 0), (-1, -1), 0.4, rule),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (0, -1), 0),
            ("RIGHTPADDING", (0, 0), (0, -1), 8),
            ("LEFTPADDING", (1, 0), (-1, -1), 0),
        ]))
        return [tbl, Spacer(1, 7 * mm)]

    def block_timeline():
        events = data.get("timeline") or []
        if not events:
            return []

        rows = []
        for e in events:
            cells = [_p(e.get("date"), s_date, 32 * mm),
                     _p(e.get("event"), s_body, content_w - 40 * mm)]
            rows.append(cells[::-1] if use_arabic else cells)

        tw = [34 * mm, content_w - 34 * mm]
        tbl = Table(rows, colWidths=tw[::-1] if use_arabic else tw)
        tbl.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEABOVE", (0, 0), (-1, -1), 0.4, rule),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (0, -1), 8),
        ]))
        return [KeepTogether([_p(HEAD_TIMELINE, s_head, content_w), tbl]),
                Spacer(1, 7 * mm)]

    def block_takeaways():
        items = data.get("takeaways") or []
        if not items:
            return []

        rows = []
        for item in items:
            marker = Table([[""]], colWidths=[3.2 * mm], rowHeights=[3.2 * mm])
            marker.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), accent),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]))
            cells = [marker, _p(item, s_body, content_w - 12 * mm)]
            rows.append(cells[::-1] if use_arabic else cells)

        bw = [9 * mm, content_w - 9 * mm]
        tbl = Table(rows, colWidths=bw[::-1] if use_arabic else bw)
        tbl.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ]))
        return [KeepTogether([_p(HEAD_TAKEAWAYS, s_head, content_w), tbl]),
                Spacer(1, 5 * mm)]

    builders = {
        "stats": block_stats,
        "sections": block_sections,
        "timeline": block_timeline,
        "takeaways": block_takeaways,
    }

    # each template lays its blocks out in its own order
    for name in t.get("order", ["stats", "sections", "timeline", "takeaways"]):
        flow.extend(builders[name]())

    # ---------- page furniture ----------

    def decorate(canvas, doc):
        canvas.saveState()
        if t["dark"] or t["paper"].lower() != "#ffffff":
            canvas.setFillColor(paper)
            canvas.rect(0, 0, page_w, page_h, fill=1, stroke=0)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(muted)
        canvas.drawRightString(page_w - margin, 11 * mm, str(doc.page))
        canvas.restoreState()

    buffer = BytesIO()

    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=margin, rightMargin=margin,
        topMargin=margin, bottomMargin=20 * mm,
        title=clean_text(data.get("title") or "Infographic"),
    )

    doc.build(flow, onFirstPage=decorate, onLaterPages=decorate)

    return buffer.getvalue()


@st.cache_data(show_spinner=False)
def cached_pdf(payload_key, data, template_key):
    """Cache by a small key so the PDF is built once per result."""
    return build_pdf(data, template_key)


# =========================================================
# SESSION STATE
#
# Streamlit re-runs this whole file on every click. Anything
# not stored here disappears the moment a widget is touched.
# =========================================================

DEFAULTS = {
    "phase": "upload",       # upload -> choose -> done
    "profile": None,         # the agent's read of the document
    "document_text": None,   # cached PDF text
    "doc_meta": None,        # filename, pages, words
    "source_name": None,
    "result": None,          # full generate response
    "html": None,            # current infographic markup
    "ai_output": None,       # cached extraction, reused for restyles
    "built_options": None,   # options that produced the current result
    "history": [],           # (question, answer, category, label, tone)
}

for key, value in DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = value


def reset_all():
    for key, value in DEFAULTS.items():
        st.session_state[key] = [] if isinstance(value, list) else value


# =========================================================
# NETWORK
# =========================================================

def friendly_error(raw):
    """Pull the readable sentence out of an n8n error payload.

    A rejected PDF comes back as JSON with the explanation buried
    inside it. Showing the whole blob helps nobody, so the message
    itself is extracted when it is there.
    """
    try:
        data = json.loads(raw)
    except Exception:
        return None

    for key in ("message", "error", "description"):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, str) and len(value) > 20:
            # n8n prefixes the node name, which is noise for the user
            return value.split("[line", 1)[0].strip()

    return None


def read_response(response):
    if response.status_code != 200:
        nice = friendly_error(response.text)
        if nice:
            return None, nice
        return None, (
            f"n8n returned status {response.status_code}.\n\n"
            f"{response.text[:1000]}"
        )
    try:
        return response.json(), None
    except ValueError:
        return None, (
            "n8n returned a 200 response that was not valid JSON.\n\n"
            f"{response.text[:1000]}"
        )


def call_n8n(url, timeout, files=None, payload=None):
    """One entry point for every network call. Returns (data, error)."""
    try:
        response = requests.post(url, files=files, json=payload, timeout=timeout)
    except requests.exceptions.Timeout:
        return None, (
            f"The request took longer than {timeout} seconds. "
            "The n8n workflow may still be running."
        )
    except requests.exceptions.RequestException as exc:
        return None, f"Could not reach n8n: {exc}"

    return read_response(response)


# =========================================================
# PROGRESS WIDGET
#
# Streamlit blocks while the HTTP request runs, so a normal
# st.progress cannot advance. This is an iframe with its own
# JavaScript timer, so it keeps animating while the main
# thread waits. The stage timings are indicative, not measured.
# =========================================================

def progress_widget(stages, accent="#5b6ef5", est_seconds=45):
    steps_html = "".join(
        f'<li class="stage" data-i="{i}">'
        f'<span class="dot"></span><span class="txt">{s}</span></li>'
        for i, s in enumerate(stages)
    )

    per_stage = max(int(est_seconds * 1000 / max(len(stages), 1)), 1200)

    return f"""
<style>
  .prog {{
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    max-width: 520px;
    padding: 4px 2px 8px;
  }}
  .bar {{
    height: 5px;
    border-radius: 3px;
    background: rgba(128,128,128,.18);
    overflow: hidden;
    margin-bottom: 18px;
  }}
  .bar span {{
    display: block;
    height: 100%;
    width: 38%;
    border-radius: 3px;
    background: linear-gradient(90deg,
      transparent, {accent}, transparent);
    animation: slide 1.5s ease-in-out infinite;
  }}
  @keyframes slide {{
    0%   {{ transform: translateX(-100%); }}
    100% {{ transform: translateX(360%); }}
  }}
  .stages {{ list-style: none; margin: 0; padding: 0; }}
  .stage {{
    display: flex; align-items: center; gap: 11px;
    padding: 5px 0;
    font-size: 14px;
    color: rgba(128,128,128,.75);
    opacity: .45;
    transition: opacity .35s ease, color .35s ease;
  }}
  .stage .dot {{
    width: 9px; height: 9px; border-radius: 50%;
    background: rgba(128,128,128,.4);
    flex: none;
    transition: background .35s ease, transform .35s ease;
  }}
  .stage.active {{ opacity: 1; color: {accent}; font-weight: 600; }}
  .stage.active .dot {{
    background: {accent};
    transform: scale(1.35);
    animation: pulse 1.1s ease-in-out infinite;
  }}
  .stage.done {{ opacity: .85; }}
  .stage.done .dot {{ background: {accent}; }}
  @keyframes pulse {{
    0%, 100% {{ box-shadow: 0 0 0 0 {accent}66; }}
    50%      {{ box-shadow: 0 0 0 6px {accent}00; }}
  }}
  @media (prefers-reduced-motion: reduce) {{
    .bar span {{ animation: none; width: 100%; opacity: .5; }}
    .stage.active .dot {{ animation: none; }}
  }}
</style>

<div class="prog">
  <div class="bar"><span></span></div>
  <ul class="stages">{steps_html}</ul>
</div>

<script>
  (function () {{
    var items = document.querySelectorAll('.stage');
    var i = 0;
    function step() {{
      items.forEach(function (el, n) {{
        el.classList.toggle('active', n === i);
        el.classList.toggle('done', n < i);
      }});
      // hold on the last stage rather than looping, so a slow
      // run never looks like it restarted
      if (i < items.length - 1) {{
        i += 1;
        setTimeout(step, {per_stage});
      }}
    }}
    step();
  }})();
</script>
"""


def show_progress(placeholder, stages, accent="#5b6ef5", est=45):
    with placeholder.container():
        components.html(
            progress_widget(stages, accent, est),
            height=60 + 30 * len(stages),
        )


# =========================================================
# HELPERS
# =========================================================

def estimate_height(data):
    height = 640
    height += 150 * max(1, len(data.get("key_stats") or []) // 3 + 1)
    height += 170 * len(data.get("sections") or [])
    height += 70 * len(data.get("timeline") or [])
    height += 48 * len(data.get("takeaways") or [])
    return min(height, 5200)


def safe_stem(name, fallback="infographic"):
    base = (name or fallback).rsplit(".", 1)[0]
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in base)


def what_changed(current, built):
    """Return 'none', 'style' or 'content'."""
    if built is None:
        return "content"
    if any(current[k] != built[k] for k in CONTENT_KEYS):
        return "content"
    if current["template"] != built["template"] or current["accent"] != built["accent"]:
        return "style"
    return "none"


def print_button(html, label="Open print view", present=False):
    """Open the infographic in a new tab.

    present=False also triggers the print dialog, which is the
    fallback route to a PDF. present=True just opens it, which is
    what the zooming presentation needs.
    """
    encoded = base64.b64encode(html.encode("utf-8")).decode("ascii")
    uid = hashlib.md5((encoded + str(present)).encode()).hexdigest()[:8]

    after = "" if present else (
        "setTimeout(function () { try { w.focus(); w.print(); } "
        "catch (e) {} }, 1200);"
    )

    components.html(
        f"""
<button id="p{uid}" style="
  font-family: system-ui, sans-serif; font-size: 14px;
  padding: 8px 16px; border-radius: 8px; cursor: pointer;
  border: 1px solid rgba(128,128,128,.4); background: transparent;
  color: inherit;">{label}</button>
<script>
  document.getElementById('p{uid}').onclick = function () {{
    // atob gives bytes, not text. Decoding them as UTF-8 is what
    // keeps Arabic readable instead of turning it into mojibake.
    var raw = atob("{encoded}");
    var bytes = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);

    var blob = new Blob([bytes], {{ type: 'text/html;charset=utf-8' }});
    var url = URL.createObjectURL(blob);

    var w = window.open(url, '_blank');
    if (w) {{ w.focus(); }}
    {after}
  }};
</script>
""",
        height=48,
    )


# =========================================================
# HEADER
# =========================================================

st.title("📄 PDF Infographic Generator")

st.write(
    "Upload a PDF. The agent reads it, suggests a layout, and turns it into "
    "a one-page infographic you can download."
)


# =========================================================
# PHASE 1 — UPLOAD
# =========================================================

if st.session_state["phase"] == "upload":

    st.subheader("Step 1 — Upload your document")

    uploaded_file = st.file_uploader("PDF file", type=["pdf"], key="pdf_upload")

    if uploaded_file is not None:

        file_bytes = uploaded_file.getvalue()
        size_mb = len(file_bytes) / (1024 * 1024)

        st.caption(f"{uploaded_file.name} — {size_mb:.1f} MB")

        if size_mb > MAX_UPLOAD_MB:
            st.error(f"This file is over the {MAX_UPLOAD_MB} MB limit.")

        elif st.button("Read this document", type="primary"):

            slot = st.empty()

            show_progress(
                slot,
                [
                    "Checking the file",
                    "Reading the text",
                    "Judging whether it suits an infographic",
                    "Choosing a layout",
                ],
                est=25,
            )

            data, error = call_n8n(
                ANALYZE_URL,
                ANALYZE_TIMEOUT,
                files={
                    "file": (uploaded_file.name, file_bytes, "application/pdf")
                },
            )

            slot.empty()

            if error:
                st.error(error)

            elif (data or {}).get("status") == "rejected":
                # Not an error. The file simply cannot become an
                # infographic, and the agent explains why. Arabic
                # replies are laid out right to left.
                rtl = data.get("dir") == "rtl"
                side = "right" if rtl else "left"
                flow = "rtl" if rtl else "ltr"

                def _safe(value):
                    return (str(value or "")
                            .replace("&", "&amp;")
                            .replace("<", "&lt;")
                            .replace(">", "&gt;"))

                st.markdown(
                    f"<div style='direction:{flow};text-align:{side};"
                    "background:#fef3c7;color:#92400e;padding:14px 18px;"
                    "border-radius:10px;font-weight:600;font-size:1rem'>"
                    f"{_safe(data.get('title', 'This file cannot be used'))}"
                    "</div>",
                    unsafe_allow_html=True,
                )

                st.write("")

                st.markdown(
                    f"<div style='direction:{flow};text-align:{side};"
                    "font-size:1rem;line-height:1.8'>"
                    f"{_safe(data.get('message', ''))}</div>",
                    unsafe_allow_html=True,
                )

                st.write("")

                with st.expander("What the checker found"):
                    q = data.get("quality") or {}
                    m = data.get("meta") or {}
                    st.write(
                        f"Pages: {m.get('page_count', '?')}  \n"
                        f"Readable words: {q.get('real_words', '?')}  \n"
                        f"Readable words per page: {q.get('words_per_page', '?')}  \n"
                        f"Unreadable characters: {q.get('junk_percent', '?')}%"
                    )
                    st.caption(
                        "A document works here when its text can be selected "
                        "and copied in a PDF reader."
                    )

            else:
                st.session_state["profile"] = data.get("profile") or {}
                st.session_state["document_text"] = data.get("document_text")
                st.session_state["doc_meta"] = data.get("meta") or {}
                st.session_state["source_name"] = uploaded_file.name
                st.session_state["phase"] = "choose"
                st.rerun()

    else:
        st.info("Choose a PDF to begin.")


# =========================================================
# PHASE 2 — THE AGENT ASKS WHICH TEMPLATE
# =========================================================

elif st.session_state["phase"] == "choose":

    profile = st.session_state["profile"] or {}
    meta = st.session_state["doc_meta"] or {}

    recommended = profile.get("recommended_template", "editorial")
    if recommended not in TEMPLATES:
        recommended = "editorial"

    st.subheader("Step 2 — Choose a template")

    # --- the agent's message ---

    bits = []
    if meta.get("page_count"):
        bits.append(f"{meta['page_count']} pages")
    if meta.get("word_count"):
        bits.append(f"{meta['word_count']:,} words")

    with st.chat_message("assistant"):
        st.markdown(
            f"I've read **{profile.get('title_guess') or meta.get('filename')}**"
            + (f" ({', '.join(bits)})." if bits else ".")
        )
        st.markdown(
            f"It looks like a **{profile.get('doc_type', 'document')}**. "
            f"{profile.get('reason', '')}"
        )
        st.markdown(
            f"I'd suggest **{TEMPLATES[recommended]['label']}**, but any of "
            "these six will work. Which would you like?"
        )

    if meta.get("truncated"):
        st.warning(
            "The document was longer than the analysis limit, so only its "
            "first part was read."
        )

    # --- the six options ---

    keys = list(TEMPLATES.keys())

    template = st.radio(
        "Template",
        keys,
        index=keys.index(recommended),
        format_func=lambda k: (
            f"{TEMPLATES[k]['label']}"
            + ("  ·  suggested for your document" if k == recommended else "")
        ),
        label_visibility="collapsed",
    )

    st.caption(TEMPLATES[template]["blurb"])
    st.caption(f"Best for: {TEMPLATES[template]['best']}")

    # --- optional refinements ---

    with st.expander("Adjust the writing (optional)"):

        col_a, col_b, col_c = st.columns(3)

        with col_a:
            depth = DEPTHS[st.selectbox("Detail level", list(DEPTHS.keys()), index=1)]
        with col_b:
            audience = AUDIENCES[st.selectbox("Written for", list(AUDIENCES.keys()), index=0)]
        with col_c:
            # default to the language the agent found in the document
            lang_values = list(LANGUAGES.values())
            detected = (profile.get("language") or "").strip().lower()
            default_lang = 1 if detected.startswith(("ar", "\u0639")) else 0

            language = LANGUAGES[st.selectbox(
                "Language", list(LANGUAGES.keys()), index=default_lang
            )]

        use_accent = st.checkbox("Custom highlight colour")
        accent = st.color_picker("Highlight", "#5b6ef5") if use_accent else None

    col_go, col_back = st.columns([3, 1])

    with col_go:
        go = st.button(
            f"Build my {TEMPLATES[template]['label'].lower()}",
            type="primary",
            use_container_width=True,
        )

    with col_back:
        if st.button("Start over", use_container_width=True):
            reset_all()
            st.rerun()

    if go:

        slot = st.empty()

        show_progress(
            slot,
            [
                "Sending the document",
                "Pulling out the key points",
                f"Laying out the {TEMPLATES[template]['label']}",
                "Almost there",
            ],
            accent=accent or "#5b6ef5",
            est=55,
        )

        options = {
            "template": template,
            "accent": accent,
            "depth": depth,
            "audience": audience,
            "language": language,
        }

        data, error = call_n8n(
            BUILD_URL,
            BUILD_TIMEOUT,
            payload={
                "document_text": st.session_state["document_text"],
                "meta": st.session_state["doc_meta"],
                **options,
            },
        )

        slot.empty()

        if error:
            st.error(error)
        else:
            st.session_state["result"] = data
            st.session_state["html"] = data.get("html")
            st.session_state["ai_output"] = data.get("ai_output")
            st.session_state["built_options"] = dict(options)
            st.session_state["phase"] = "done"
            st.session_state["history"] = []
            st.rerun()


# =========================================================
# PHASE 3 — RESULT
# =========================================================

else:

    result = st.session_state["result"] or {}
    used = result.get("template_used") or {}

    # -----------------------------------------------------
    # SIDEBAR — switch template without a new OpenAI call
    # -----------------------------------------------------

    with st.sidebar:

        st.header("Template")

        keys = list(TEMPLATES.keys())
        built = st.session_state["built_options"] or {}
        current_key = built.get("template", "editorial")
        if current_key not in keys:
            current_key = "editorial"

        template = st.radio(
            "Template",
            keys,
            index=keys.index(current_key),
            format_func=lambda k: TEMPLATES[k]["label"],
            label_visibility="collapsed",
        )

        st.caption(TEMPLATES[template]["blurb"])

        use_accent = st.checkbox(
            "Custom highlight colour",
            value=bool(built.get("accent")),
        )

        accent = (
            st.color_picker("Highlight", built.get("accent") or "#5b6ef5")
            if use_accent else None
        )

        st.divider()
        st.header("Writing")

        depth_keys = list(DEPTHS.keys())
        depth = DEPTHS[st.selectbox(
            "Detail level", depth_keys,
            index=[DEPTHS[k] for k in depth_keys].index(built.get("depth", "standard")),
        )]

        aud_keys = list(AUDIENCES.keys())
        audience = AUDIENCES[st.selectbox(
            "Written for", aud_keys,
            index=[AUDIENCES[k] for k in aud_keys].index(built.get("audience", "general")),
        )]

        lang_keys = list(LANGUAGES.keys())
        language = LANGUAGES[st.selectbox(
            "Language", lang_keys,
            index=max(0, [LANGUAGES[k] for k in lang_keys]
                      .index(built["language"])
                      if built.get("language") in LANGUAGES.values() else 0),
        )]

        current_options = {
            "template": template,
            "accent": accent,
            "depth": depth,
            "audience": audience,
            "language": language,
        }

        change = what_changed(current_options, built)

        st.divider()

        if change == "style":
            label, help_text = "Switch template", "Only the look changes. No new analysis."
        elif change == "content":
            label, help_text = "Rebuild", "The writing settings changed, so the document is read again."
        else:
            label, help_text = "Rebuild", "Nothing has changed yet."

        apply_clicked = st.button(label, type="primary", use_container_width=True)
        st.caption(help_text)

        if st.button("New document", use_container_width=True):
            reset_all()
            st.rerun()

    # -----------------------------------------------------
    # APPLY
    # -----------------------------------------------------

    if apply_clicked:

        slot = st.empty()

        if change == "style" and st.session_state["ai_output"]:

            show_progress(
                slot,
                [f"Re-laying out as {TEMPLATES[template]['label']}"],
                accent=accent or "#5b6ef5",
                est=6,
            )

            data, error = call_n8n(
                RESTYLE_URL,
                RESTYLE_TIMEOUT,
                payload={
                    "ai_output": st.session_state["ai_output"],
                    "meta": st.session_state["doc_meta"] or {},
                    "options": {"template": template, "accent": accent},
                },
            )

            slot.empty()

            if error:
                st.error(error)
            else:
                st.session_state["html"] = data.get("html")

                # keep the stored result in step with what is on screen,
                # otherwise the caption and the layout stay on the old
                # template even though the HTML has changed
                if st.session_state["result"] is not None:
                    st.session_state["result"]["template_used"] = (
                        data.get("template_used")
                        or {"template": template,
                            "label": TEMPLATES[template]["label"],
                            "accent": accent}
                    )

                st.session_state["built_options"] = dict(current_options)
                st.rerun()

        else:

            show_progress(
                slot,
                [
                    "Re-reading the document",
                    "Pulling out the key points",
                    f"Laying out the {TEMPLATES[template]['label']}",
                ],
                accent=accent or "#5b6ef5",
                est=55,
            )

            data, error = call_n8n(
                BUILD_URL,
                BUILD_TIMEOUT,
                payload={
                    "document_text": st.session_state["document_text"],
                    "meta": st.session_state["doc_meta"],
                    **current_options,
                },
            )

            slot.empty()

            if error:
                st.error(error)
            else:
                st.session_state["result"] = data
                st.session_state["html"] = data.get("html")
                st.session_state["ai_output"] = data.get("ai_output")
                st.session_state["built_options"] = dict(current_options)
                st.rerun()

    # -----------------------------------------------------
    # TABS
    # -----------------------------------------------------

    tab_visual, tab_content, tab_ask = st.tabs(
        ["Infographic", "Extracted content", "Ask the document"]
    )

    # --- TAB 1 -------------------------------------------

    with tab_visual:

        html = st.session_state["html"]

        if not html:
            st.error(
                "The workflow returned no HTML. Check the Infographic "
                "Builder node in n8n."
            )
        else:
            is_presentation = used.get("template") == "presentation"

            st.caption(f"Template: {used.get('label', '—')}")

            if is_presentation:
                st.info(
                    "Use the arrow keys, or click the stage, to move between "
                    "points. It looks best fullscreen — the button is below "
                    "the frame."
                )

            components.html(
                html,
                height=680 if is_presentation else estimate_height(result),
                scrolling=not is_presentation,
            )

            if is_presentation:
                print_button(html, "Open fullscreen", present=True)

            st.divider()

            stem = safe_stem(st.session_state["source_name"])

            col_html, col_pdf = st.columns(2)

            with col_html:
                st.download_button(
                    "Download HTML",
                    data=html,
                    file_name=f"{stem}_infographic.html",
                    mime="text/html",
                    use_container_width=True,
                )

            with col_pdf:
                # Built from the content, not printed from the page,
                # so it downloads straight away with no browser dialog.
                try:
                    # a moving presentation cannot print, so its PDF is
                    # rendered as the ordinary Report layout instead
                    pdf_template = used.get("template")
                    if pdf_template == "presentation":
                        pdf_template = "editorial"

                    key = f"{stem}|{pdf_template}|{len(html)}"
                    pdf_bytes = cached_pdf(key, result, pdf_template)

                    st.download_button(
                        "Download PDF",
                        data=pdf_bytes,
                        file_name=f"{stem}_infographic.pdf",
                        mime="application/pdf",
                        use_container_width=True,
                    )
                except Exception as exc:
                    st.caption(f"PDF export failed: {exc}")
                    print_button(html, "Save as PDF (via browser)")

            if is_presentation:
                st.caption(
                    "The HTML download keeps the movement. The PDF cannot, "
                    "so it is produced as an ordinary printable page with "
                    "the same content."
                )
            else:
                st.caption(
                    "The PDF is generated as a proper A4 document in the "
                    "same colours as the template on screen."
                )

    # --- TAB 2 -------------------------------------------

    with tab_content:

        st.subheader(result.get("title") or "Untitled")

        if result.get("subtitle"):
            st.caption(result["subtitle"])
        if result.get("summary"):
            st.write(result["summary"])

        st.divider()

        stats = result.get("key_stats") or []

        st.markdown("**Numbers found in the document**")

        if not stats:
            st.caption(
                "None. A number is only kept when the agent can quote the "
                "sentence it came from, so an empty list here means the "
                "document had no verifiable figures."
            )
        else:
            for stat in stats:
                unit = stat.get("unit") or ""
                st.markdown(f"**{stat.get('value')} {unit}** — {stat.get('label')}")
                st.caption(f"Source: {stat.get('source_quote')}")

        st.divider()

        for section in (result.get("sections") or []):
            with st.expander(section.get("heading", "Section")):
                st.write(section.get("text", ""))

        timeline = result.get("timeline") or []
        if timeline:
            st.markdown("**Timeline**")
            for event in timeline:
                st.write(f"{event.get('date')} — {event.get('event')}")

        takeaways = result.get("takeaways") or []
        if takeaways:
            st.markdown("**Takeaways**")
            for item in takeaways:
                st.write(f"- {item}")

        with st.expander("Raw response from n8n"):
            st.json({
                k: v for k, v in result.items()
                if k not in ("html", "ai_output")
            })

    # --- TAB 3 -------------------------------------------
    #
    # A normal chat: oldest at the top, newest at the bottom,
    # and the input box pinned underneath the conversation.
    #
    # Arabic replies are laid out right to left, and the badge
    # and captions come back from n8n already in the language
    # of the question, so nothing switches mid conversation.
    # -----------------------------------------------------

    with tab_ask:

        document_text = st.session_state["document_text"]

        st.caption(
            "Your question is checked first. Only if it is about this "
            "document does the agent go and read it. Anything else gets a "
            "polite refusal, labelled so you can see how it was classified."
        )

        # colours for the badge above a non-answer
        BADGE = {
            "ok":   ("#0f766e", "#ccfbf1"),
            "warn": ("#92400e", "#fef3c7"),
            "stop": ("#991b1b", "#fee2e2"),
        }

        def is_arabic(text):
            return any("\u0600" <= ch <= "\u06ff" for ch in str(text))

        def rtl_block(text, size="1rem", colour=None, italic=False):
            """Render a paragraph in the correct direction."""
            direction = "rtl" if is_arabic(text) else "ltr"
            align = "right" if direction == "rtl" else "left"
            style = (
                f"direction:{direction};text-align:{align};"
                f"font-size:{size};line-height:1.7;"
            )
            if colour:
                style += f"color:{colour};"
            if italic:
                style += "font-style:italic;"
            safe = (
                str(text)
                .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
            st.markdown(
                f"<div style='{style}'>{safe}</div>",
                unsafe_allow_html=True,
            )

        history = st.session_state["history"]

        if not history:
            with st.chat_message("assistant"):
                st.write(
                    f"Ask me anything about **{result.get('title') or 'this document'}**. "
                    "I only answer from the PDF you uploaded, in whichever "
                    "language you ask."
                )

        # oldest first, so the newest reply is always at the bottom
        for entry in history:

            asked, answer, category, label, tone, quote, source_label = entry

            with st.chat_message("user"):
                rtl_block(asked)

            with st.chat_message("assistant"):

                if category != "answered":
                    fg, bg = BADGE.get(tone, BADGE["warn"])
                    side = "right" if is_arabic(answer) else "left"
                    st.markdown(
                        f"<div style='text-align:{side}'>"
                        f"<span style='background:{bg};color:{fg};"
                        "padding:2px 10px;border-radius:999px;font-size:12px;"
                        f"font-weight:600'>{label}</span></div>",
                        unsafe_allow_html=True,
                    )
                    st.write("")

                rtl_block(answer)

                if quote:
                    rtl_block(
                        f"{source_label}: {quote}",
                        size="0.82rem",
                        colour="#6b7280",
                        italic=True,
                    )

        # --- the input sits below the conversation ---

        question = st.chat_input("Ask about this document")

        if question and question.strip():

            slot = st.empty()
            # The stage list runs on a timer in the browser, so it
            # cannot know whether n8n opened the document. Keep the
            # second stage neutral rather than claiming something
            # that may not have happened.
            show_progress(
                slot,
                [
                    "Checking your question",
                    "Preparing the answer",
                ],
                est=9,
            )

            data, error = call_n8n(
                ASK_URL,
                ASK_TIMEOUT,
                payload={
                    "question": question.strip(),
                    "document_text": document_text,
                    "doc_title": result.get("title") or "",
                },
            )

            slot.empty()

            if error:
                st.error(error)
            else:
                # append, so the newest lands at the bottom
                st.session_state["history"].append((
                    question.strip(),
                    data.get("answer", "No answer returned."),
                    data.get("category", "unknown"),
                    data.get("category_label", "Unknown"),
                    data.get("tone", "warn"),
                    data.get("source_quote", ""),
                    data.get("source_label", "From the document"),
                ))
                st.rerun()
