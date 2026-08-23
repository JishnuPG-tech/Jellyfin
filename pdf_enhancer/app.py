"""
Streamlit Web Application: Document PDF Cleaner & Bleed-Through Restorer
Transforms scanned, shadowed, and bleed-through PDFs into ultra-clean,
pure-white (#FFFFFF) and laser-black (#000000) print-ready documents.
"""

import os
import io
import time
import tempfile
import streamlit as st
import numpy as np
import cv2
from PIL import Image
import fitz

from pdf_cleaner import DocumentCleaner, CleaningMode, PDFProcessor

st.set_page_config(
    page_title="PDF Enhancer & Bleed-Through Restorer",
    page_icon="📄",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS for high-end modern styling
st.markdown("""
<style>
    .main-title {
        font-size: 2.2rem;
        font-weight: 800;
        letter-spacing: -0.02em;
        margin-bottom: 0.2rem;
        background: linear-gradient(135deg, #6366f1 0%, #a855f7 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
    }
    .sub-title {
        font-size: 1.05rem;
        color: #94a3b8;
        margin-bottom: 1.5rem;
    }
    .metric-card {
        background: rgba(255, 255, 255, 0.05);
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-radius: 12px;
        padding: 1rem;
        text-align: center;
    }
    .stButton>button {
        background: linear-gradient(135deg, #6366f1 0%, #a855f7 100%);
        color: white;
        font-weight: 600;
        border: none;
        border-radius: 10px;
        padding: 0.6rem 1.2rem;
        transition: all 0.2s ease;
    }
    .stButton>button:hover {
        transform: translateY(-2px);
        box-shadow: 0 4px 15px rgba(99, 102, 241, 0.4);
    }
</style>
""", unsafe_allow_html=True)


def main():
    st.markdown('<div class="main-title">📄 Document PDF Cleaner & Bleed-Through Restorer</div>', unsafe_allow_html=True)
    st.markdown('<div class="sub-title">AI-Powered Optical Normalization, Word-Level Protection, and Pure White (#FFFFFF) Document Restoration.</div>', unsafe_allow_html=True)

    # ── Sidebar Controls ──────────────────────────────────────────────────────
    with st.sidebar:
        st.header("⚙️ Restoration Controls")
        
        mode_choice = st.selectbox(
            "1. Cleaning Mode",
            ["Laser (Pure 1-bit B&W)", "Smooth (Anti-Aliased Grayscale)", "Color Enhanced", "Adaptive Gaussian"],
            index=0,
            help="Laser creates pure binary 0/255 for razor-sharp printing. Smooth retains anti-aliasing."
        )
        mode_map = {
            "Laser (Pure 1-bit B&W)": CleaningMode.LASER,
            "Smooth (Anti-Aliased Grayscale)": CleaningMode.SMOOTH,
            "Color Enhanced": CleaningMode.COLOR,
            "Adaptive Gaussian": CleaningMode.ADAPTIVE
        }
        selected_mode = mode_map[mode_choice]

        st.subheader("🧠 AI & Filter Settings")
        adaptive_profiling = st.toggle("AI Per-Page Dynamic Auto-Tuner", value=True, help="Automatically meters noise entropy per page and dynamically scales optical contrast.")
        word_envelope_protection = st.toggle("Word-Level Envelope Protection", value=True, help="Protects all punctuation, i-dots, and math symbols while purging inter-word bleed-through dots.")
        despeckle = st.toggle("Despeckle (Salt & Pepper Noise)", value=True, help="Removes tiny isolated dust/scanner pixels.")

        with st.expander("🛠️ Advanced Fine-Tuning", expanded=False):
            sauvola_k = st.slider("Sauvola Sensitivity (k)", 0.05, 0.35, 0.15, 0.01)
            contrast_thresh = st.slider("Base Optical Threshold", 20.0, 65.0, 38.0, 1.0)
            white_cut = st.slider("White Cutoff (Smooth/Color)", 180, 255, 210, 5)
            black_cut = st.slider("Black Cutoff (Smooth/Color)", 0, 150, 80, 5)
            margin_crop = st.slider("Outer Margin Shadow Crop (%)", 0.0, 3.0, 0.8, 0.1) / 100.0

        st.subheader("🖨️ Export Quality")
        dpi_choice = st.selectbox("Resolution DPI", [150, 200, 300], index=2, help="300 DPI gives ultra-crisp print quality.")

    # Instantiate Cleaner
    cleaner = DocumentCleaner(
        mode=selected_mode,
        sauvola_k=sauvola_k,
        white_cutoff=white_cut,
        black_cutoff=black_cut,
        despeckle=despeckle,
        margin_percent=margin_crop,
        filter_bleedthrough=True,
        contrast_threshold=contrast_thresh,
        clean_anomalies=word_envelope_protection,
        adaptive_thresholding=adaptive_profiling
    )

    processor = PDFProcessor(cleaner=cleaner, dpi=dpi_choice)

    # ── Upload Section ────────────────────────────────────────────────────────
    uploaded_file = st.file_uploader("Upload Scanned or Photographed Document PDF", type=["pdf"])

    if uploaded_file is not None:
        # Save to temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_in:
            tmp_in.write(uploaded_file.read())
            input_pdf_path = tmp_in.name

        total_pages = processor.get_page_count(input_pdf_path)

        # Metrics Bar
        col_m1, col_m2, col_m3, col_m4 = st.columns(4)
        with col_m1:
            st.metric("Total Pages", f"{total_pages}")
        with col_m2:
            st.metric("Active Mode", selected_mode.value.upper())
        with col_m3:
            st.metric("Resolution", f"{dpi_choice} DPI")
        with col_m4:
            st.metric("AI Auto-Tuner", "Active" if adaptive_profiling else "Static")

        st.divider()

        # ── Live Page Preview Tab & Full Processing Tab ───────────────────────
        tab_preview, tab_process = st.tabs(["🔍 Live Side-by-Side Preview", "⚡ Process & Download Full PDF"])

        with tab_preview:
            page_to_preview = st.number_input("Select Page for Live Preview", min_value=1, max_value=total_pages, value=1, step=1) - 1
            
            with st.spinner("Rendering and restoring preview..."):
                raw_preview = processor.extract_page_image(input_pdf_path, page_to_preview, dpi=150)
                cleaned_preview = processor.clean_single_page(raw_preview)

                col_orig, col_clean = st.columns(2)
                with col_orig:
                    st.subheader("📷 Original Scanned Document")
                    if len(raw_preview.shape) == 2:
                        st.image(raw_preview, use_container_width=True, channels="GRAY")
                    else:
                        st.image(cv2.cvtColor(raw_preview, cv2.COLOR_BGR2RGB), use_container_width=True)

                with col_clean:
                    st.subheader("✨ Cleaned Output (Pure White & Laser Black)")
                    if len(cleaned_preview.shape) == 2:
                        st.image(cleaned_preview, use_container_width=True, channels="GRAY")
                    else:
                        st.image(cv2.cvtColor(cleaned_preview, cv2.COLOR_BGR2RGB), use_container_width=True)

        with tab_process:
            st.write("Ready to process all pages with authentic aspect ratio and zero character loss.")
            
            col_sel1, col_sel2 = st.columns([2, 1])
            with col_sel1:
                page_selection = st.text_input("Page Range to Process (leave empty or 'all' for all pages):", placeholder="e.g. 1-10, 15, 20-30")
            with col_sel2:
                btn_start = st.button("🚀 Process & Generate Clean PDF", use_container_width=True)

            if btn_start:
                pages_to_clean = None
                if page_selection and page_selection.strip().lower() != "all":
                    try:
                        from clean_pdf import parse_page_range
                        pages_to_clean = parse_page_range(page_selection.strip(), total_pages)
                    except Exception as e:
                        st.error(f"Invalid page range: {e}")
                        pages_to_clean = None

                progress_bar = st.progress(0.0)
                status_text = st.empty()

                def update_progress(current, total, msg):
                    prog = float(current) / float(max(1, total))
                    progress_bar.progress(prog)
                    status_text.text(f"[{current}/{total}] {msg}")

                out_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
                out_path = out_tmp.name
                out_tmp.close()

                t0 = time.time()
                with st.spinner("Processing document..."):
                    processor.process_pdf(
                        input_pdf_path=input_pdf_path,
                        output_pdf_path=out_path,
                        pages=pages_to_clean,
                        progress_callback=update_progress
                    )
                elapsed = time.time() - t0

                st.success(f"🎉 Restoration Complete in {elapsed:.2f} seconds!")
                
                with open(out_path, "rb") as f:
                    pdf_bytes = f.read()

                base_name = os.path.splitext(uploaded_file.name)[0]
                st.download_button(
                    label="📥 Download Cleaned PDF Document",
                    data=pdf_bytes,
                    file_name=f"{base_name}_cleaned.pdf",
                    mime="application/pdf",
                    use_container_width=True
                )

                try:
                    os.remove(out_path)
                except Exception:
                    pass

        # Cleanup input temp file on finish
        try:
            os.remove(input_pdf_path)
        except Exception:
            pass


if __name__ == "__main__":
    main()
