"""Public repository safeguards for operator-owned files."""

from scripts.check_public_hygiene import path_problems


def test_resume_document_formats_are_rejected_anywhere() -> None:
    for path in ("resume.docx", "resume.odt", "resume.pdf", "resume.rtf"):
        assert path_problems(path) == "binary or credential file type"
