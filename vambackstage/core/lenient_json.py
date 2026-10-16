from __future__ import annotations

import json
from typing import Any


def loads_lenient(text: str | bytes) -> Any:
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", errors="replace")
    if text.startswith("﻿"):
        text = text[1:]
    try:
        return json.loads(text, strict=False)
    except ValueError:
        pass
    return json.loads(_clean(text), strict=False)


def _clean(text: str) -> str:
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    quote = ""
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                in_string = False
            i += 1
            continue
        if ch == '"' or ch == "'":
            in_string = True
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if ch == ",":
            j = i + 1
            while j < n:
                c = text[j]
                if c in " \t\r\n":
                    j += 1
                elif c == "/" and j + 1 < n and text[j + 1] == "/":
                    k = text.find("\n", j)
                    j = n if k == -1 else k
                elif c == "/" and j + 1 < n and text[j + 1] == "*":
                    k = text.find("*/", j + 2)
                    j = n if k == -1 else k + 2
                else:
                    break
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)
