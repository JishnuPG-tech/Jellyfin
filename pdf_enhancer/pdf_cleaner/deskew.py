"""
Document deskewing module.
Detects rotation tilt angles from text baseline orientation and rectifies page geometry.
"""

import cv2
import numpy as np
from typing import Tuple


def detect_skew_angle(image: np.ndarray, max_angle: float = 15.0) -> float:
    """
    Detects skew angle in degrees using Hough transform on edge contours.
    """
    if len(image.shape) == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image.copy()
        
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=100, minLineLength=100, maxLineGap=10)
    
    if lines is None:
        return 0.0
        
    angles = []
    for line in lines:
        pts = line.flatten()
        if len(pts) >= 4:
            x1, y1, x2, y2 = pts[:4]
            if x2 - x1 == 0:
                continue
            angle = np.degrees(np.arctan2(float(y2 - y1), float(x2 - x1)))
            if abs(angle) <= max_angle:
                angles.append(angle)
            
    if not angles:
        return 0.0
        
    return float(np.median(angles))


def rotate_image(image: np.ndarray, angle: float, background_color: int = 255) -> np.ndarray:
    """
    Rotates an image by the given angle in degrees, filling empty space with background_color.
    """
    if abs(angle) < 0.1:
        return image
        
    h, w = image.shape[:2]
    center = (w // 2, h // 2)
    
    rot_mat = cv2.getRotationMatrix2D(center, angle, 1.0)
    cos = np.abs(rot_mat[0, 0])
    sin = np.abs(rot_mat[0, 1])
    
    new_w = int((h * sin) + (w * cos))
    new_h = int((h * cos) + (w * sin))
    
    rot_mat[0, 2] += (new_w / 2) - center[0]
    rot_mat[1, 2] += (new_h / 2) - center[1]
    
    border_val = (background_color, background_color, background_color) if len(image.shape) == 3 else background_color
    rotated = cv2.warpAffine(
        image,
        rot_mat,
        (new_w, new_h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=border_val
    )
    return rotated


def auto_deskew(image: np.ndarray, max_angle: float = 15.0) -> Tuple[np.ndarray, float]:
    """
    Automatically detects skew and rotates the image to upright position.
    """
    angle = detect_skew_angle(image, max_angle=max_angle)
    if abs(angle) >= 0.1:
        return rotate_image(image, angle), angle
    return image, 0.0
