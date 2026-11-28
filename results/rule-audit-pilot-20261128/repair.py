def comma_gloss(answer, evidence):
    if "," not in answer:
        return answer
    head, sep, tail = answer.partition(",")
    head = head.strip()
    tail = tail.strip()
    if not head or len(head) < 3:
        return answer
    if head[-1] in "-/" or any(ch.isdigit() for ch in head):
        return answer
    if len(head.split()) > 5:
        return answer
    # Preserve meaningful commas inside entity names: multi-part names
    # (e.g. 'Smith, Jones and Company') and 'County, State' style qualifiers.
    if re.search(r"\b(and|&)\b", tail, re.IGNORECASE):
        return answer
    if re.search(r"\b(County|Parish|Borough|Municipality)\b", head, re.IGNORECASE):
        return answer
    # Only drop a clean trailing gloss/appositive.
    if tail and (tail[0].islower() or tail.lower().startswith(("the ", "a ", "an "))):
        return head
    return answer
