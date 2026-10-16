# -*- coding: utf-8 -*-
"""Redaction for anything published on /eval-dashboard. The people are invented, but the page
is public, so it follows the same rule as real data would: account and mobile numbers keep only
their last 4 digits, upload-link tokens and bearer tokens are removed.

    python eval/redact.py      # rewrite eval/results/*.json in place
"""
import os
import re
import json
import glob

_LONG_NUMBER = re.compile(r"(?<![\dA-Za-z])(\d{5,14})(\d{4})(?!\d)")       # 9-18 digits
_UPLOAD = re.compile(r"(/u/)[A-Za-z0-9_\-]{12,}")
_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]{16,}")


def redact(obj):
    if isinstance(obj, str):
        s = _JWT.sub("[token]", obj)
        s = _BEARER.sub(r"\1[token]", s)
        s = _UPLOAD.sub(r"\1[link-token]", s)
        return _LONG_NUMBER.sub(lambda m: "X" * len(m.group(1)) + m.group(2), s)
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    if isinstance(obj, dict):
        return {k: redact(v) for k, v in obj.items()}
    return obj


if __name__ == "__main__":
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
    for path in glob.glob(os.path.join(here, "*.json")):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(redact(data), fh, indent=1, ensure_ascii=False)
        print("redacted", os.path.basename(path))
