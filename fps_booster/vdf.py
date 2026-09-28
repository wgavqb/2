"""Минимальный парсер текстового формата Valve KeyValues (VDF/ACF)."""

from __future__ import annotations

_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}


class VDFError(ValueError):
    pass


def _tokenize(text: str):
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end == -1 else end + 1
        elif c in "{}":
            yield c, c
            i += 1
        elif c == '"':
            i += 1
            buf = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    buf.append(_ESCAPES.get(text[i + 1], text[i + 1]))
                    i += 2
                else:
                    buf.append(text[i])
                    i += 1
            i += 1
            yield "str", "".join(buf)
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '{}"':
                j += 1
            token = text[i:j]
            i = j
            if not token.startswith("["):  # условия вида [$WIN32] игнорируем
                yield "str", token


def parse(text: str) -> dict:
    tokens = list(_tokenize(text))
    pos = 0

    def parse_object(top_level: bool) -> dict:
        nonlocal pos
        result: dict = {}
        while pos < len(tokens):
            kind, value = tokens[pos]
            if kind == "}":
                pos += 1
                if top_level:
                    raise VDFError("лишняя закрывающая скобка")
                return result
            if kind == "{":
                raise VDFError("неожиданная открывающая скобка")
            key = value
            pos += 1
            if pos >= len(tokens):
                raise VDFError(f"нет значения для ключа {key!r}")
            kind, value = tokens[pos]
            pos += 1
            if kind == "{":
                result[key] = parse_object(False)
            elif kind == "str":
                result[key] = value
            else:
                raise VDFError(f"неожиданный токен после {key!r}")
        if not top_level:
            raise VDFError("незакрытый блок")
        return result

    return parse_object(True)


def get_ci(mapping: dict, key: str, default=None):
    """Регистронезависимый доступ — Steam пишет ключи в разном регистре."""
    lowered = key.lower()
    for k, v in mapping.items():
        if k.lower() == lowered:
            return v
    return default
