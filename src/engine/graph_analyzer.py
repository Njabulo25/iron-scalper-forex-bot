"""Basic image analysis utilities for uploaded trading charts."""

from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image


UPLOAD_FOLDER = "data/uploads"
_upload_directory = Path(UPLOAD_FOLDER)
_upload_directory.mkdir(parents=True, exist_ok=True)
_allowed_extensions = {".png", ".jpg", ".jpeg"}


def analyze_chart(image_path):
    """Analyze chart brightness distribution and pixel volatility."""
    try:
        with Image.open(image_path) as image:
            grayscale_image = image.convert("L")
            image_array = np.asarray(grayscale_image, dtype=np.float64)
            image_size = grayscale_image.size

        if image_array.ndim != 2 or image_array.size == 0:
            raise ValueError("The uploaded image contains no usable pixels.")

        average_brightness = float(image_array.mean())
        image_height = image_array.shape[0]
        midpoint = max(image_height // 2, 1)
        top_half = image_array[:midpoint]
        bottom_half = image_array[midpoint:]

        if bottom_half.size == 0:
            bottom_half = top_half

        top_brightness = float(top_half.mean())
        bottom_brightness = float(bottom_half.mean())

        if bottom_brightness - top_brightness > 10:
            trend = "UPTREND - Price action is rising"
        elif top_brightness - bottom_brightness > 10:
            trend = "DOWNTREND - Price action is falling"
        else:
            trend = "SIDEWAYS - Price is consolidating"

        standard_deviation = float(image_array.std())
        if standard_deviation > 70:
            volatility = "HIGH"
        elif standard_deviation >= 40:
            volatility = "MEDIUM"
        else:
            volatility = "LOW"

        return {
            "trend": trend,
            "volatility": volatility,
            "brightness": average_brightness,
            "image_size": image_size,
        }
    except Exception as error:
        return {"error": str(error), "trend": "UNABLE TO ANALYZE"}


def save_upload(file):
    """Save a PNG or JPG upload with a timestamp-based name and return its path."""
    filename = getattr(file, "filename", "")
    extension = Path(filename).suffix.lower()
    if extension not in _allowed_extensions:
        raise ValueError("Only PNG and JPG image uploads are supported.")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    upload_path = _upload_directory / f"chart_{timestamp}{extension}"
    file.save(str(upload_path))
    return str(upload_path)
