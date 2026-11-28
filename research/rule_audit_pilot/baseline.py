"""Extracted unchanged from historical grounded_answer.py; see FROZEN.json."""

def _drop_trailing_gloss(text: str) -> str:
    """``Northenden, Manchester`` -> ``Northenden`` (only for a clean head)."""
    if "," not in text:
        return text
    head = text.partition(",")[0].strip()
    if (
        head
        and len(head) >= 3
        and head[-1] not in "-/"
        and not any(ch.isdigit() for ch in head)
        and len(head.split()) <= 5
    ):
        return head
    return text

def comma_gloss(answer, evidence):
    return _drop_trailing_gloss(answer)

def trim_surface(answer, evidence):
    return answer.strip().strip("\"*").strip().rstrip(".").strip()
