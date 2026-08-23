"""
High-performance PDF Processor.
Handles rasterization, parallel multi-threaded page cleaning,
and clean PDF compilation using PyMuPDF and Pillow.
"""

import os
import fitz  # PyMuPDF
import cv2
import numpy as np
from typing import List, Optional, Callable, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image

from .engine import DocumentCleaner, CleaningMode


class PDFProcessor:
    def __init__(self, cleaner: Optional[DocumentCleaner] = None, dpi: int = 300):
        self.cleaner = cleaner or DocumentCleaner()
        self.dpi = dpi

    def get_page_count(self, pdf_path: str) -> int:
        """Returns the total number of pages in the PDF."""
        with fitz.open(pdf_path) as doc:
            return len(doc)

    def extract_page_image(self, pdf_path: str, page_num: int, dpi: Optional[int] = None) -> np.ndarray:
        """
        Renders a single PDF page into an OpenCV BGR or Grayscale numpy array.
        page_num is 0-indexed.
        """
        target_dpi = dpi or self.dpi
        with fitz.open(pdf_path) as doc:
            page = doc[page_num]
            pix = page.get_pixmap(dpi=target_dpi)
            
            # Convert pixmap to numpy array
            if pix.alpha:
                img_data = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 4)
                img = cv2.cvtColor(img_data, cv2.COLOR_RGBA2BGR)
            elif pix.n == 3:
                img_data = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
                img = cv2.cvtColor(img_data, cv2.COLOR_RGB2BGR)
            else:
                img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
                
            return img

    def clean_single_page(self, img: np.ndarray) -> np.ndarray:
        """Runs the AI cleaning & layout restoration pipeline on a single image."""
        return self.cleaner.clean_image(img)

    def process_page_task(
        self,
        pdf_path: str,
        page_num: int,
        dpi: int
    ) -> Dict[str, Any]:
        """Task executed by worker thread for a single page."""
        raw_img = self.extract_page_image(pdf_path, page_num, dpi=dpi)
        cleaned_img = self.clean_single_page(raw_img)
        return {
            "page_num": page_num,
            "cleaned_img": cleaned_img
        }

    def process_pdf(
        self,
        input_pdf_path: str,
        output_pdf_path: str,
        pages: Optional[List[int]] = None,
        max_workers: Optional[int] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None
    ) -> str:
        """
        Processes entire PDF or selected pages and writes a new clean PDF.
        pages: list of 0-indexed page numbers. If None, processes all pages.
        progress_callback: callback function(current_step, total_steps, message)
        """
        if not os.path.exists(input_pdf_path):
            raise FileNotFoundError(f"Input PDF not found: {input_pdf_path}")

        total_doc_pages = self.get_page_count(input_pdf_path)
        page_indices = pages if pages is not None else list(range(total_doc_pages))
        num_pages = len(page_indices)

        if num_pages == 0:
            raise ValueError("No pages to process.")

        if progress_callback:
            progress_callback(0, num_pages, f"Starting processing {num_pages} pages...")

        workers = max_workers or min(8, max(1, (os.cpu_count() or 4)))
        cleaned_pages_dict: Dict[int, np.ndarray] = {}
        completed_count = 0

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_page = {
                executor.submit(self.process_page_task, input_pdf_path, p_idx, self.dpi): p_idx
                for p_idx in page_indices
            }

            for future in as_completed(future_to_page):
                p_idx = future_to_page[future]
                try:
                    result = future.result()
                    cleaned_pages_dict[result["page_num"]] = result["cleaned_img"]
                    completed_count += 1
                    if progress_callback:
                        progress_callback(
                            completed_count,
                            num_pages,
                            f"Processed page {p_idx + 1} ({completed_count}/{num_pages})"
                        )
                except Exception as e:
                    if progress_callback:
                        progress_callback(
                            completed_count,
                            num_pages,
                            f"Error on page {p_idx + 1}: {str(e)}"
                        )
                    raise e

        # Compile pages into output PDF in exact original order
        if progress_callback:
            progress_callback(num_pages, num_pages, "Compiling output PDF document...")

        output_doc = fitz.open()
        for p_idx in page_indices:
            clean_img = cleaned_pages_dict[p_idx]
            
            # Encode image to memory PNG for PyMuPDF insertion
            if len(clean_img.shape) == 2:
                is_success, buffer = cv2.imencode(".png", clean_img, [cv2.IMWRITE_PNG_COMPRESSION, 6])
            else:
                is_success, buffer = cv2.imencode(".jpg", clean_img, [cv2.IMWRITE_JPEG_QUALITY, 95])
                
            if not is_success:
                raise RuntimeError(f"Failed to encode image for page {p_idx + 1}")
                
            img_bytes = buffer.tobytes()
            
            # Standard 72 DPI PDF point conversion
            h, w = clean_img.shape[:2]
            pt_w = (w / self.dpi) * 72.0
            pt_h = (h / self.dpi) * 72.0
            
            page = output_doc.new_page(width=pt_w, height=pt_h)
            page.insert_image(fitz.Rect(0, 0, pt_w, pt_h), stream=img_bytes)

        output_doc.save(output_pdf_path, deflate=True, garbage=3)
        output_doc.close()

        if progress_callback:
            progress_callback(num_pages, num_pages, f"Successfully created: {output_pdf_path}")

        return output_pdf_path
