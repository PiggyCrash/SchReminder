"""
Name parser for SchReminder Scout.

Detects the uni-to-uni naming pattern: (Scholarship Body) University Name
Uses balanced-parenthesis tracking so nested parens are handled correctly.
"""

import re as _re

# Parenthesised prefixes that are category tags, NOT scholarship body names.
# Entries starting with these are treated as centralised scholarships.
_UNI_TO_UNI_SKIP_PREFIXES = {
    "uni-funded",   # e.g. (Uni-Funded) Leiden University Excellence Scholarships
}


def _find_balanced_close(s: str) -> int:
    """
    Return the index of the ')' that BALANCES the '(' at s[0].
    Returns -1 if the string doesn't start with '(' or is unbalanced.

    Examples
    --------
    '(MEXT Scholarship) foo'
        -> 17  (first ')')

    '(Intl Grad Program (IGP) Special MEXT Scholarship) Hokkaido'
        -> 50  (last ')', skipping the nested one after 'IGP')
    """
    if not s or s[0] != '(':
        return -1
    depth = 0
    for i, ch in enumerate(s):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return i
    return -1  # unbalanced


def parse_scholarship_name(name: str) -> dict:
    """
    Detects uni-to-uni naming pattern: (Scholarship Body) University Name

    Uses balanced-parenthesis tracking instead of a greedy/non-greedy regex so
    that nested parens in either part are handled correctly:

      '(MEXT Scholarship) Intl Grad Program (IGP) Special - Hokkaido'
          scholarship = 'MEXT Scholarship'
          university  = 'Intl Grad Program (IGP) Special - Hokkaido'

      '(Intl Grad Program (IGP) Special MEXT Scholarship) Hokkaido Univ'
          scholarship = 'Intl Grad Program (IGP) Special MEXT Scholarship'
          university  = 'Hokkaido Univ'

    Returns:
      { "type": "centralized", "display_name": name }        -> normal scholarship
      { "type": "uni_to_uni",  "scholarship": "...",
        "university": "...",   "display_name": name }         -> uni-specific entry
    """
    s = name.strip()
    if not s.startswith('('):
        return {"type": "centralized", "display_name": name}

    close_idx = _find_balanced_close(s)
    if close_idx == -1:
        return {"type": "centralized", "display_name": name}

    scholarship = s[1:close_idx].strip()          # text between outer ( and )
    rest        = s[close_idx + 1:].strip()       # text after the outer )

    if not rest or not scholarship:
        return {"type": "centralized", "display_name": name}

    if scholarship.lower() in _UNI_TO_UNI_SKIP_PREFIXES:
        return {"type": "centralized", "display_name": name}

    return {
        "type":         "uni_to_uni",
        "scholarship":  scholarship,
        "university":   rest,
        "display_name": name,
    }
