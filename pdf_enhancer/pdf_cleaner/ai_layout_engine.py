"""
AI & Computer Vision Layout Intelligence Engine.
Provides projection profile baseline deskewing, contextual bleed-through dot removal,
stroke boldness enhancement, and 0.00% gray pixel uniform standard A4 canvas normalization.
"""

import cv2
import numpy as np
from typing import Tuple, List, Dict, Any, Optional


class AILayoutEngine:
    def __init__(
        self,
        target_dpi: int = 300,
        enable_deskew: bool = True,
        enable_bleedthrough_removal: bool = True,
        enable_layout_normalization: bool = True,
        enable_boldness: bool = True,
        bold_strength: int = 1,
        standard_canvas_ratio: float = 1.414  # A4 aspect ratio (H / W = 3508 / 2480)
    ):
        self.target_dpi = target_dpi
        self.enable_deskew = enable_deskew
        self.enable_bleedthrough_removal = enable_bleedthrough_removal
        self.enable_layout_normalization = enable_layout_normalization
        self.enable_boldness = enable_boldness
        self.bold_strength = bold_strength
        self.standard_canvas_ratio = standard_canvas_ratio

    def estimate_deskew_angle(self, gray_or_bin_img: np.ndarray, max_angle: float = 7.0, step: float = 0.1) -> float:
        """
        Robust text baseline angle estimation using Horizontal Projection Profile (Radon)
        variance maximization on the central text region.
        """
        h, w = gray_or_bin_img.shape[:2]
        if len(gray_or_bin_img.shape) == 3:
            gray = cv2.cvtColor(gray_or_bin_img, cv2.COLOR_BGR2GRAY)
        else:
            gray = gray_or_bin_img

        # Binarize if grayscale
        if np.unique(gray).size > 2:
            _, bin_inv = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        else:
            bin_inv = 255 - gray

        # Focus on central text area (15% to 85% width/height) to eliminate margin interference
        cy1, cy2 = int(h * 0.15), int(h * 0.85)
        cx1, cx2 = int(w * 0.15), int(w * 0.85)
        central = bin_inv[cy1:cy2, cx1:cx2]
        ch, cw = central.shape
        center = (cw // 2, ch // 2)

        best_angle = 0.0
        max_variance = -1.0

        for ang in np.arange(-max_angle, max_angle + step, step):
            rot_mat = cv2.getRotationMatrix2D(center, float(ang), 1.0)
            rotated = cv2.warpAffine(central, rot_mat, (cw, ch), flags=cv2.INTER_NEAREST, borderValue=0)
            proj = np.sum(rotated, axis=1, dtype=np.float64)
            var = np.var(proj)
            if var > max_variance:
                max_variance = var
                best_angle = float(ang)

        return best_angle

    def deskew_image(self, img: np.ndarray, angle: float) -> np.ndarray:
        """
        Rotates image by angle degrees on an expanded canvas to avoid cropping corner text.
        """
        if abs(angle) < 0.05:
            return img

        h, w = img.shape[:2]
        center = (w // 2, h // 2)

        rot_mat = cv2.getRotationMatrix2D(center, angle, 1.0)
        cos = np.abs(rot_mat[0, 0])
        sin = np.abs(rot_mat[0, 1])

        new_w = int((h * sin) + (w * cos))
        new_h = int((h * cos) + (w * sin))

        rot_mat[0, 2] += (new_w / 2) - center[0]
        rot_mat[1, 2] += (new_h / 2) - center[1]

        border_val = 255 if len(img.shape) == 2 else (255, 255, 255)
        rotated = cv2.warpAffine(
            img,
            rot_mat,
            (new_w, new_h),
            flags=cv2.INTER_NEAREST if np.unique(img).size <= 2 else cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=border_val
        )
        return rotated

    def remove_contextual_bleedthrough(
        self,
        binary_img: np.ndarray,
        min_dot_area: int = 1,
        max_dot_area: int = 45,
        max_isolated_dist: int = 20
    ) -> np.ndarray:
        """
        Removes isolated bleed-through ink dots and paper grain that lie far away from any text line,
        while strictly preserving i-dots, punctuation, decimals, and math symbols close to words.
        """
        h, w = binary_img.shape[:2]
        inv = 255 - binary_img
        num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(inv, connectivity=8)

        if num_labels <= 1:
            return binary_img

        # 1. Build text-only character mask (excluding tiny isolated dots)
        text_only_mask = np.zeros_like(binary_img, dtype=np.uint8)
        for i in range(1, num_labels):
            area = stats[i, cv2.CC_STAT_AREA]
            cw = stats[i, cv2.CC_STAT_WIDTH]
            ch = stats[i, cv2.CC_STAT_HEIGHT]
            if area >= 35 or cw >= 10 or ch >= 10:
                text_only_mask[labels == i] = 255

        # 2. Build text line envelopes from character clusters
        kernel_h = cv2.getStructuringElement(cv2.MORPH_RECT, (max(15, int(w / 25)), 5))
        line_envelopes = cv2.dilate(text_only_mask, kernel_h, iterations=2)

        # 3. Distance Transform from Text Line Envelopes
        envelope_inv = 255 - line_envelopes
        dist_map = cv2.distanceTransform(envelope_inv, cv2.DIST_L2, 5)

        # 4. Contextual Dot Classification
        mask_to_erase = np.zeros_like(binary_img, dtype=np.uint8)
        for i in range(1, num_labels):
            area = stats[i, cv2.CC_STAT_AREA]
            cw = stats[i, cv2.CC_STAT_WIDTH]
            ch = stats[i, cv2.CC_STAT_HEIGHT]
            cx = int(centroids[i][0])
            cy = int(centroids[i][1])

            cx_clamped = min(w - 1, max(0, cx))
            cy_clamped = min(h - 1, max(0, cy))

            if min_dot_area <= area <= max_dot_area and cw <= 16 and ch <= 16:
                dist = dist_map[cy_clamped, cx_clamped]
                if dist > max_isolated_dist:
                    mask_to_erase[labels == i] = 255
            elif area <= 2:
                mask_to_erase[labels == i] = 255

        cleaned = np.where(mask_to_erase == 255, 255, binary_img)
        return cleaned

    def enhance_stroke_boldness(self, binary_img: np.ndarray) -> np.ndarray:
        """
        Morphologically thickens text strokes with a cross-structuring element
        to produce deep, rich, bold black text with high contrast and readability.
        """
        if not self.enable_boldness or self.bold_strength <= 0:
            return binary_img

        inv = 255 - binary_img
        kernel_size = 3 if self.bold_strength == 1 else 5
        kernel = cv2.getStructuringElement(cv2.MORPH_CROSS, (kernel_size, kernel_size))
        bold_inv = cv2.dilate(inv, kernel, iterations=1)
        return 255 - bold_inv

    def normalize_to_standard_canvas(
        self,
        binary_img: np.ndarray,
        target_w: int = 2480,
        target_h: int = 3508,
        margin_pct: float = 0.06
    ) -> np.ndarray:
        """
        Places the clean text block neatly centered onto a standardized, uniform A4 canvas
        with elegant publishing margins. Guarantees 0.00% intermediate gray pixels.
        """
        h, w = binary_img.shape[:2]
        inv = 255 - binary_img
        num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(inv, connectivity=8)

        if num_labels <= 1:
            return np.full((target_h, target_w), 255, dtype=np.uint8)

        min_x, min_y = w, h
        max_x, max_y = 0, 0

        for i in range(1, num_labels):
            x, y, cw, ch, area = stats[i]
            if area >= 3:
                min_x = min(min_x, x)
                min_y = min(min_y, y)
                max_x = max(max_x, x + cw)
                max_y = max(max_y, y + ch)

        if min_x >= max_x or min_y >= max_y:
            return np.full((target_h, target_w), 255, dtype=np.uint8)

        content = binary_img[min_y:max_y, min_x:max_x]
        ch_h, ch_w = content.shape[:2]

        max_print_w = int(target_w * (1.0 - 2 * margin_pct))
        max_print_h = int(target_h * (1.0 - 2 * margin_pct))

        scale = min(max_print_w / float(ch_w), max_print_h / float(ch_h), 1.0)
        scaled_w = max(1, int(ch_w * scale))
        scaled_h = max(1, int(ch_h * scale))

        scaled_content = cv2.resize(content, (scaled_w, scaled_h), interpolation=cv2.INTER_NEAREST)
        scaled_content = np.where(scaled_content < 200, 0, 255).astype(np.uint8)

        canvas = np.full((target_h, target_w), 255, dtype=np.uint8)
        offset_x = (target_w - scaled_w) // 2
        offset_y = (target_h - scaled_h) // 2

        canvas[offset_y:offset_y + scaled_h, offset_x:offset_x + scaled_w] = scaled_content
        return canvas
