"""Interfaces used by OCR, checked against the installed pytesseract API."""

from typing import Any

from PIL.Image import Image

from . import pytesseract as pytesseract

class Output:
    DICT: str

def image_to_data(image: Image, *, lang: str, output_type: str) -> dict[str, list[Any]]: ...
