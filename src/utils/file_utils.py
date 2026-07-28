import pypandoc
import tempfile
import os


_PANDOC_EXTRA = ["--from", "markdown-raw_tex"]


def markdown_to_docx(md: str) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
        outpath = tmp.name
    try:
        pypandoc.convert_text(md, to="docx", format="md", outputfile=outpath, extra_args=_PANDOC_EXTRA)
        with open(outpath, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(outpath):
            os.unlink(outpath)


def markdown_to_pdf(md: str) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        outpath = tmp.name
    try:
        pypandoc.convert_text(md, to="pdf", format="md", outputfile=outpath, extra_args=_PANDOC_EXTRA)
        with open(outpath, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(outpath):
            os.unlink(outpath)