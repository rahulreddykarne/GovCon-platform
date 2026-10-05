"""Symbols GovCon OCR uses from pytesseract.

The installed package has no library stubs. ``image_to_data`` is typed for
``Output.DICT``, which is the only output mode this project requests.
``file_to_dict`` parses numeric TSV cells as ``int`` and leaves the text
column as ``str``; confidence stays ``int | str`` because ``float()`` is
applied by the caller.
"""

from typing import TypedDict

class ImageData(TypedDict):
    text: list[str]
    block_num: list[int]
    par_num: list[int]
    line_num: list[int]
    conf: list[int | str]


class Output:
    DICT: str


class _TesseractModule:
    tesseract_cmd: str | None


pytesseract: _TesseractModule


def image_to_data(
    image: object,
    lang: str | None = ...,
    config: str = ...,
    nice: int = ...,
    output_type: str = ...,
    timeout: int = ...,
    pandas_config: object | None = ...,
) -> ImageData: ...
