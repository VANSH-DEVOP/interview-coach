"""Resume text extraction from PDF and DOCX files."""

import logging
from io import BytesIO

from docx import Document
from pypdf import PdfReader

logger = logging.getLogger(__name__)


class ResumeParser:
    """Extract text from PDF and DOCX resumes."""

    @staticmethod
    def parse_pdf(content: bytes) -> str:
        """Extract text from a PDF file.

        Args:
            content: The PDF file bytes.

        Returns:
            Extracted text from all pages.

        Raises:
            ValueError: If PDF is invalid or cannot be read.
        """
        try:
            pdf_reader = PdfReader(BytesIO(content))
            text_pages = []

            for page in pdf_reader.pages:
                text = page.extract_text()
                if text:
                    text_pages.append(text)

            return "\n".join(text_pages).strip()
        except Exception as e:
            logger.error(f"Failed to parse PDF: {e}")
            raise ValueError(f"Failed to parse PDF: {e}") from e

    @staticmethod
    def parse_docx(content: bytes) -> str:
        """Extract text from a DOCX file.

        Args:
            content: The DOCX file bytes.

        Returns:
            Extracted text from all paragraphs.

        Raises:
            ValueError: If DOCX is invalid or cannot be read.
        """
        try:
            doc = Document(BytesIO(content))
            paragraphs = [paragraph.text for paragraph in doc.paragraphs if paragraph.text]
            return "\n".join(paragraphs).strip()
        except Exception as e:
            logger.error(f"Failed to parse DOCX: {e}")
            raise ValueError(f"Failed to parse DOCX: {e}") from e

    @staticmethod
    def parse(content: bytes, content_type: str) -> str:
        """Parse resume based on content type.

        Args:
            content: The file bytes.
            content_type: MIME type of the file.

        Returns:
            Extracted text from the resume.

        Raises:
            ValueError: If content type is unsupported or parsing fails.
        """
        if content_type == "application/pdf":
            return ResumeParser.parse_pdf(content)
        elif content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            return ResumeParser.parse_docx(content)
        else:
            raise ValueError(f"Unsupported content type: {content_type}")
