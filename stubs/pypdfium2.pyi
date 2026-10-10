"""Symbols GovCon OCR uses from pypdfium2.

The installed package has no ``py.typed`` marker. This stub covers only
``PdfDocument``, page rendering, and ``PdfBitmap.to_pil``.
"""


class PdfBitmap:
    def to_pil(self) -> object: ...


class PdfPage:
    def render(
        self,
        scale: float = ...,
        rotation: int = ...,
        crop: tuple[float, float, float, float] = ...,
        may_draw_forms: bool = ...,
    ) -> PdfBitmap: ...


class PdfDocument:
    def __init__(self, input: bytes | str, password: str | None = ..., autoclose: bool = ...) -> None: ...

    def __getitem__(self, index: int) -> PdfPage: ...

    def close(self) -> None: ...
