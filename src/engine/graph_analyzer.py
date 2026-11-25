"""Validate uploaded chart images and report basic image measurements.

Pixel brightness and contrast are not market data, so this module deliberately
does not infer price direction or trading volatility from an image.
"""

from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image


UPLOAD_FOLDER = "data/uploads"
_upload_directory = Path(UPLOAD_FOLDER)
_upload_directory.mkdir(parents=True, exist_ok=True)
_allowed_extensions = {".png", ".jpg", ".jpeg"}
_allowed_formats = {"PNG", "JPEG"}
_max_image_pixels = 20_000_000


def _validate_image(file):
    """Validate the decoded format and dimensions before writing an upload."""
    stream = getattr(file, "stream", None)
    if stream is None:
        raise ValueError("The uploaded file could not be read.")
    stream.seek(0)
    try:
        with Image.open(stream) as image:
            image_format = image.format
            width, height = image.size
            if image_format not in _allowed_formats:
                raise ValueError("The file contents are not a PNG or JPEG image.")
            if width <= 0 or height <= 0 or width * height > _max_image_pixels:
                raise ValueError("The image dimensions are invalid or too large.")
            image.verify()
    except (OSError, Image.DecompressionBombError) as error:
        raise ValueError("The uploaded file is not a valid image.") from error
    finally:
        stream.seek(0)


def analyze_chart(image_path):
    """Return descriptive pixel measurements without claiming price analysis."""
    try:
        with Image.open(image_path) as image:
            if image.format not in _allowed_formats:
                raise ValueError("The uploaded file is not a PNG or JPEG image.")
            if image.width * image.height > _max_image_pixels:
                raise ValueError("The image dimensions are too large.")
            grayscale_image = image.convert("L")
            image_array = np.asarray(grayscale_image, dtype=np.float64)
            image_size = {"width": grayscale_image.width, "height": grayscale_image.height}

        if image_array.ndim != 2 or image_array.size == 0:
            raise ValueError("The uploaded image contains no usable pixels.")

        return {
            "price_trend": "NOT ANALYZED",
            "image_brightness": round(float(image_array.mean()), 1),
            "image_contrast": round(float(image_array.std()), 1),
            "image_size": image_size,
            "analysis_note": (
                "These are image measurements only. This tool does not identify "
                "price direction or produce a trading signal."
            ),
        }
    except Exception as error:
        return {"error": str(error), "price_trend": "NOT ANALYZED"}


def save_upload(file):
    """Validate and save a PNG or JPEG upload with a unique timestamp name."""
    filename = getattr(file, "filename", "")
    extension = Path(filename).suffix.lower()
    if extension not in _allowed_extensions:
        raise ValueError("Only PNG and JPG image uploads are supported.")
    _validate_image(file)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    upload_path = _upload_directory / f"chart_{timestamp}{extension}"
    file.save(str(upload_path))
    return str(upload_path)
