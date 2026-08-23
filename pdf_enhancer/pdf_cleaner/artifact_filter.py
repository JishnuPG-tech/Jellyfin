"""
Artifact filter module.
Provides detection and removal of scanner framing lines, scanner lid shadows,
spiral binding coils, punch holes, and dark boundary artifacts without clipping text.
"""

import cv2
import numpy as np
from typing import Tuple, List, Optional


class ArtifactFilter:
    def __init__(
        self,
        remove_spirals: bool = True,
        remove_dark_bands: bool = True,
        auto_center: bool = False,
        max_spiral_search_ratio: float = 0.15,
        margin_crop_ratio: float = 0.008
    ):
        self.remove_spirals = remove_spirals
        self.remove_dark_bands = remove_dark_bands
        self.auto_center = auto_center
        self.max_spiral_search_ratio = max_spiral_search_ratio
        self.margin_crop_ratio = margin_crop_ratio

    def clean_structural_scanner_artifacts(self, binary_img: np.ndarray) -> np.ndarray:
        """
        Detects and erases scanner lid bars, camera frame edges, and outer border lines.
        """
        h, w = binary_img.shape[:2]
        inv = 255 - binary_img
        num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(inv, connectivity=8)
        erase_mask = np.zeros_like(binary_img, dtype=np.uint8)

        for i in range(1, num_labels):
            x, y, cw, ch, area = stats[i]

            # Rule A: Wide horizontal scanner lid bars spanning > 55% width in top/bottom 22%
            if cw >= w * 0.55 and (y <= h * 0.22 or (y + ch) >= h * 0.78):
                erase_mask[labels == i] = 255

            # Rule B: Outer frame edge components touching boundary in outer 3%
            elif (x <= 6 or y <= 6 or (x + cw) >= w - 6 or (y + ch) >= h - 6) and (cw > w * 0.35 or ch > h * 0.35 or area > 1000):
                erase_mask[labels == i] = 255

            # Rule C: Spiral gutter coils & holes on far left edge
            elif x <= w * 0.08 and (x + cw) <= w * 0.14 and area >= 30:
                erase_mask[labels == i] = 255

        return np.where(erase_mask == 255, 255, binary_img)

    def find_gutter_spiral_boundary(self, binary_img: np.ndarray) -> int:
        """
        Finds the vertical whitespace column that separates spiral bindings from text.
        """
        h, w = binary_img.shape[:2]
        inv = 255 - binary_img
        search_w = int(w * self.max_spiral_search_ratio)
        if search_w <= 10:
            return 0

        gutter_slice = inv[:, :search_w]
        col_dark_density = np.mean(gutter_slice, axis=0) / 255.0

        in_spiral_region = False
        spiral_end_x = 0

        for x in range(2, search_w):
            density = col_dark_density[x]
            if density > 0.025:
                in_spiral_region = True
            elif in_spiral_region and density < 0.004:
                spiral_end_x = x

        return spiral_end_x

    def clean_artifacts(self, binary_img: np.ndarray) -> np.ndarray:
        """
        Cleans structural scanner artifacts, spiral coils, and border framing bands.
        """
        img = binary_img.copy()

        # 1. Structural scanner lid and frame removal
        if self.remove_dark_bands:
            img = self.clean_structural_scanner_artifacts(img)

        # 2. Spiral Gutter Boundary clearance
        if self.remove_spirals:
            gutter_boundary = self.find_gutter_spiral_boundary(img)
            if gutter_boundary > 0:
                img[:, :gutter_boundary] = 255

        # 3. Outer margin clean
        if self.margin_crop_ratio > 0:
            h, w = img.shape[:2]
            mx = int(w * self.margin_crop_ratio)
            my = int(h * self.margin_crop_ratio)
            if mx > 0 and my > 0:
                img[:my, :] = 255
                img[-my:, :] = 255
                img[:, :mx] = 255
                img[:, -mx:] = 255

        return img
