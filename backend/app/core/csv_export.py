"""Shared CSV export helper: RFC4180 quoting plus CSV/formula-injection neutralization.

These CSVs leave the app as attachments — a treasurer or secretary emails one to an
accountant or an exec board member who opens it in Excel or Google Sheets. Ledger
descriptions, categories, meeting titles, and display names are all member-supplied
free text. A cell beginning with =, +, -, @, TAB (0x09), or CR (0x0D) is parsed as a
FORMULA by spreadsheet apps ("CSV injection" a.k.a. formula/DDE injection) — a live
code-execution path on the reader's machine, not just a display glitch.

sanitize_csv_text() neutralizes that by prefixing a dangerous-leading-char field with a
single apostrophe, which spreadsheet apps treat as "force text" and strip on display.

Apply it to TEXT fields ONLY. The ledger's amount column is legitimately negative for
money out (SPEC: "positive = in, negative = out") — sanitizing a numeric column would
turn every expense into a quoted-looking string and corrupt the export. Callers format
numeric/date columns themselves and pass the resulting plain strings through unsanitized.

Quoting is layered on top of the stdlib csv module rather than hand-rolled, so commas,
double quotes, and embedded newlines in a field are RFC4180-correct automatically.
"""
import csv
import io
from collections.abc import Iterable, Sequence

from fastapi import Response

# OWASP CSV injection guidance: a leading =, +, -, @, TAB, or CR triggers formula
# evaluation in Excel / Google Sheets / LibreOffice.
_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def sanitize_csv_text(value: str | None) -> str:
    """Neutralize formula/CSV injection in a free-text field; None becomes "".

    TEXT fields only — never call this on an already-formatted numeric or date string
    (e.g. a ledger amount), or a legitimate negative amount comes out looking like
    a quoted formula and the export corrupts the financial record.
    """
    if not value:
        return ""
    if value.startswith(_DANGEROUS_PREFIXES):
        return "'" + value
    return value


def build_csv(header: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    """Render header + rows to an RFC4180 CSV string via csv.writer.

    Callers must sanitize free-text cells with sanitize_csv_text() before calling this;
    numeric/date cells should already be plain formatted strings.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


def csv_response(filename: str, header: Sequence[str], rows: Iterable[Sequence[str]]) -> Response:
    """Wrap build_csv() in a downloadable text/csv attachment response."""
    return Response(
        content=build_csv(header, rows),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
