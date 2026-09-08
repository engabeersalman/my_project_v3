import base64
import hashlib

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

ANALYZE_URL = st.secrets.get("ANALYZE_URL", f"{N8N_BASE}/pdf-analyze")
BUILD_URL = st.secrets.get("BUILD_URL", f"{N8N_BASE}/pdf-infographic")
RESTYLE_URL = st.secrets.get("RESTYLE_URL", f"{N8N_BASE}/restyle-infographic")
ASK_URL = st.secrets.get("ASK_URL", f"{N8N_BASE}/ask-pdf")

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

try:
    from weasyprint import HTML as WeasyHTML
    PDF_ENGINE = "weasyprint"
except Exception:
    WeasyHTML = None
    PDF_ENGINE = None


@st.cache_data(show_spinner=False)
def html_to_pdf(html: str) -> bytes:
    """Render the infographic HTML to PDF bytes. Cached by content."""
    return WeasyHTML(string=html).write_pdf()


# =========================================================
# TEMPLATES
#
# These labels and descriptions are what the user picks from.
# The keys must match the TEMPLATES table in the n8n
# Infographic Builder node exactly.
# =========================================================

TEMPLATES = {
    "editorial": {
        "label": "Editorial Report",
        "blurb": "Highlighter marks on warm paper, serif headlines. "
                 "The safe default for reports and articles.",
        "best": "Reports, articles, general documents",
    },
    "poster": {
        "label": "Bold Poster",
        "blurb": "Heavy type and solid accent blocks, with the takeaways "
                 "pulled to the top.",
        "best": "Announcements, pitches, one-page summaries",
    },
    "brief": {
        "label": "Minimal Brief",
        "blurb": "All serif, no highlighter, generous whitespace. Prose "
                 "sections lead.",
        "best": "Academic papers, legal text, dense writing",
    },
    "dashboard": {
        "label": "Data Dashboard",
        "blurb": "Cool blue-grey ground with oversized figures at the top.",
        "best": "Financial reports, surveys, anything number-heavy",
    },
    "timeline": {
        "label": "Timeline Story",
        "blurb": "A vertical rail of dated events, placed before everything "
                 "else.",
        "best": "Histories, project retrospectives, case studies",
    },
    "blueprint": {
        "label": "Technical Blueprint",
        "blurb": "Dark navy ground with cyan accents and precise labelling.",
        "best": "Specifications, engineering docs, technical manuals",
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
    "Same as the document": "auto",
    "English": "english",
    "Arabic": "arabic",
}

# Changing any of these needs a new OpenAI call.
CONTENT_KEYS = ("depth", "audience", "language")


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

def read_response(response):
    if response.status_code != 200:
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


def print_button(html, label="Open print view"):
    """Fallback PDF route: open the infographic and call print()."""
    encoded = base64.b64encode(html.encode("utf-8")).decode("ascii")
    uid = hashlib.md5(encoded.encode()).hexdigest()[:8]

    components.html(
        f"""
<button id="p{uid}" style="
  font-family: system-ui, sans-serif; font-size: 14px;
  padding: 8px 16px; border-radius: 8px; cursor: pointer;
  border: 1px solid rgba(128,128,128,.4); background: transparent;
  color: inherit;">{label}</button>
<script>
  document.getElementById('p{uid}').onclick = function () {{
    var html = atob("{encoded}");
    var w = window.open('', '_blank');
    w.document.write(html);
    w.document.close();
    setTimeout(function () {{ w.focus(); w.print(); }}, 900);
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
                    "Uploading your file",
                    "Extracting the text",
                    "Reading the structure",
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
            language = LANGUAGES[st.selectbox("Language", list(LANGUAGES.keys()), index=0)]

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
                "Checking every figure against the text",
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
            index=[LANGUAGES[k] for k in lang_keys].index(built.get("language", "auto")),
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
                st.session_state["built_options"] = dict(current_options)
                st.rerun()

        else:

            show_progress(
                slot,
                [
                    "Re-reading the document",
                    "Pulling out the key points",
                    "Checking every figure against the text",
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
            st.caption(f"Template: {used.get('label', '—')}")

            components.html(
                html,
                height=estimate_height(result),
                scrolling=True,
            )

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
                if PDF_ENGINE == "weasyprint":
                    try:
                        pdf_bytes = html_to_pdf(html)
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
                else:
                    print_button(html, "Save as PDF (via browser)")
                    st.caption(
                        "Opens the infographic in a new tab and starts the "
                        "print dialog. Choose *Save as PDF*."
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

    with tab_ask:

        document_text = st.session_state["document_text"]

        st.caption(
            "The agent only answers from this document. Anything else gets "
            "a polite refusal, labelled so you can see how it classified "
            "your question."
        )

        question = st.text_input(
            "Your question",
            placeholder="What does the document say about costs?",
        )

        if st.button("Ask", type="primary", disabled=not question.strip()):

            slot = st.empty()
            show_progress(slot, ["Checking the document"], est=8)

            data, error = call_n8n(
                ASK_URL,
                ASK_TIMEOUT,
                payload={
                    "question": question,
                    "document_text": document_text,
                    "doc_title": result.get("title") or "this document",
                },
            )

            slot.empty()

            if error:
                st.error(error)
            else:
                st.session_state["history"].insert(0, (
                    question,
                    data.get("answer", "No answer returned."),
                    data.get("category", "unknown"),
                    data.get("category_label", "Unknown"),
                    data.get("tone", "warn"),
                    data.get("source_quote", ""),
                ))

        history = st.session_state["history"]

        if history:
            st.divider()

            for asked, answer, category, label, tone, quote in history:

                st.markdown(f"**{asked}**")

                if tone == "ok":
                    st.success(f"{label}")
                elif tone == "stop":
                    st.error(f"{label}")
                else:
                    st.warning(f"{label}")

                st.write(answer)

                if quote:
                    st.caption(f"From the document: {quote}")

                st.divider()
