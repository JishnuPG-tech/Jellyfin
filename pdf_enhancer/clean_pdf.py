#!/usr/bin/env python3
"""
Clean PDF CLI Tool
Production Document Restoration Toolkit.
Transforms scanned and photographed document PDFs with dark backgrounds, shadows,
and back-page bleed-through dots into ultra-clean, pure-white (#FFFFFF) and deep-black (#000000)
print-ready documents with authentic page layout, geometry, and crisp typography.
"""

import os
import sys
import argparse
import time
from typing import List

from pdf_cleaner import DocumentCleaner, CleaningMode, PDFProcessor


def parse_page_range(page_str: str, total_pages: int) -> List[int]:
    """
    Parses a page range string (e.g. '1-5', '1,3,5-7', '2') into 0-indexed page numbers.
    """
    pages = set()
    parts = page_str.split(",")
    for part in parts:
        part = part.strip()
        if "-" in part:
            start_str, end_str = part.split("-", 1)
            start = int(start_str.strip()) - 1
            end = int(end_str.strip()) - 1
            for p in range(max(0, start), min(total_pages, end + 1)):
                pages.add(p)
        else:
            p = int(part) - 1
            if 0 <= p < total_pages:
                pages.add(p)
    return sorted(list(pages))


def cli_progress(current: int, total: int, message: str):
    """Prints a clean CLI progress bar."""
    bar_len = 30
    filled_len = int(bar_len * current // max(1, total))
    bar = "=" * filled_len + "-" * (bar_len - filled_len)
    percent = 100.0 * current / max(1, total)
    sys.stdout.write(f"\r[{bar}] {percent:5.1f}% | {message[:45]:<45}")
    sys.stdout.flush()
    if current >= total:
        sys.stdout.write("\n")


def main():
    parser = argparse.ArgumentParser(
        description="Transform camera-shot & scanned PDFs into ultra-clean, pure white & deep black printable PDFs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python clean_pdf.py "RATIO & Proportion.pdf"
  python clean_pdf.py document.pdf -o cleaned.pdf --mode laser --dpi 300
  python clean_pdf.py book.pdf --no-adaptive
  python clean_pdf.py book.pdf --pages 1-10
  python clean_pdf.py --gui
        """
    )

    parser.add_argument("input", nargs="?", help="Path to input PDF file")
    parser.add_argument("-o", "--output", help="Path for cleaned output PDF file")
    parser.add_argument(
        "-m", "--mode",
        choices=["laser", "smooth", "color", "adaptive"],
        default="laser",
        help="Cleaning mode: 'laser' (pure binary 0/255 for razor-sharp printing), 'smooth' (anti-aliased pure white background), 'color' (pure white background preserving colored graphics), 'adaptive' (adaptive gaussian)"
    )
    parser.add_argument("--dpi", type=int, default=300, help="Rendering resolution DPI (default: 300 for crisp print quality)")
    parser.add_argument("-k", "--sauvola-k", type=float, default=0.15, help="Sauvola binarization sensitivity (default: 0.15, range 0.05 - 0.35)")
    parser.add_argument("-w", "--sauvola-window", type=int, default=31, help="Sauvola local window size in pixels (default: 31)")
    parser.add_argument("--white-cutoff", type=int, default=210, help="White level cutoff for smooth/color mode (default: 210, 0-255)")
    parser.add_argument("--black-cutoff", type=int, default=80, help="Black level cutoff for smooth/color mode (default: 80, 0-255)")
    parser.add_argument("--no-despeckle", action="store_true", help="Disable salt-and-pepper noise filter")
    parser.add_argument("--min-speckle", type=int, default=3, help="Minimum connected component pixel size to keep (default: 3)")
    parser.add_argument("--no-bleedthrough", action="store_true", help="Disable optical contrast bleed-through dot removal")
    parser.add_argument("--no-anomalies", action="store_true", help="Disable AI spatial anomaly tracking")
    parser.add_argument("--no-adaptive", action="store_true", help="Disable AI per-page adaptive noise energy profiling")
    parser.add_argument("--contrast-thresh", type=float, default=38.0, help="Static optical contrast threshold (default: 38.0)")
    parser.add_argument("--margin", type=float, default=0.008, help="Outer margin shadow crop fraction (default: 0.008 = 0.8%%)")
    parser.add_argument("--contrast", type=float, default=1.0, help="Contrast boost power (default: 1.0)")
    parser.add_argument("-p", "--pages", help="Specific pages to process, e.g. '1-5', '1,3,7-10'")
    parser.add_argument("-t", "--threads", type=int, default=None, help="Number of worker threads (default: CPU count)")
    parser.add_argument("--gui", action="store_true", help="Launch interactive graphical user interface")

    args = parser.parse_args()

    # If --gui or no arguments provided, launch GUI
    if args.gui or not args.input:
        print("Launching PDF Cleaner Interactive GUI...")
        try:
            import app_gui
            app_gui.main(args.input)
            return
        except Exception as e:
            print(f"Error launching GUI: {e}")
            if not args.input:
                parser.print_help()
                return

    if not os.path.exists(args.input):
        print(f"Error: Input file '{args.input}' does not exist.")
        sys.exit(1)

    # Determine default output path if not specified
    if not args.output:
        base, ext = os.path.splitext(args.input)
        args.output = f"{base}_cleaned.pdf"

    print("=" * 60)
    print("  Document PDF Cleaner & AI Adaptive Restorer")
    print("=" * 60)
    print(f"Input PDF          : {args.input}")
    print(f"Output PDF         : {args.output}")
    print(f"Mode               : {args.mode.upper()}")
    print(f"DPI                : {args.dpi}")
    print(f"AI Page Profiler   : {'Disabled (Static)' if args.no_adaptive else 'Enabled (Dynamic Per-Page Energy Profiling)'}")
    print(f"Bleed-Through Dots : {'Disabled' if args.no_bleedthrough else 'Enabled (Word-Level Envelopes)'}")
    print(f"Despeckle          : {'Disabled' if args.no_despeckle else f'Enabled (min size: {args.min_speckle})'}")
    print(f"Margin Crop        : {args.margin * 100:.1f}%")
    print("-" * 60)

    cleaner = DocumentCleaner(
        mode=args.mode,
        sauvola_k=args.sauvola_k,
        sauvola_window=args.sauvola_window,
        white_cutoff=args.white_cutoff,
        black_cutoff=args.black_cutoff,
        despeckle=not args.no_despeckle,
        min_speckle_size=args.min_speckle,
        margin_percent=args.margin,
        contrast_boost=args.contrast,
        filter_bleedthrough=not args.no_bleedthrough,
        contrast_threshold=args.contrast_thresh,
        clean_anomalies=not args.no_anomalies,
        adaptive_thresholding=not args.no_adaptive
    )

    processor = PDFProcessor(cleaner=cleaner, dpi=args.dpi)
    total_pages = processor.get_page_count(args.input)
    print(f"Total Pages in Document: {total_pages}")

    selected_pages = None
    if args.pages:
        selected_pages = parse_page_range(args.pages, total_pages)
        print(f"Processing Selected Pages : {len(selected_pages)} pages ({args.pages})")

    t_start = time.time()
    try:
        processor.process_pdf(
            input_pdf_path=args.input,
            output_pdf_path=args.output,
            pages=selected_pages,
            max_workers=args.threads,
            progress_callback=cli_progress
        )
        elapsed = time.time() - t_start
        pages_processed = len(selected_pages) if selected_pages else total_pages
        speed = pages_processed / max(0.001, elapsed)

        print("-" * 60)
        print(f"Done! Cleaned PDF saved to: {args.output}")
        print(f"Processed {pages_processed} pages in {elapsed:.2f}s ({speed:.2f} pages/sec)")
        print("Ready for crisp, high-contrast, pure-white laser printing!")
        print("=" * 60)
    except Exception as e:
        print(f"\nError during processing: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
