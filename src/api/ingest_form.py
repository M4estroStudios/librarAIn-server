from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

_STREAM_CHUNK_SIZE = 256 * 1024
_MAX_TEXT_FIELD_BYTES = 256 * 1024


class InvalidPagesSpec(ValueError):
    pass


class InvalidRangeField(Exception):
    def __init__(self, field: str, message: str):
        self.field = field
        self.message_text = message
        super().__init__(message)


def _multipart_boundary(content_type: str) -> bytes:
    if "multipart/form-data" not in content_type.lower():
        raise ValueError("Content-Type must be multipart/form-data")
    for segment in content_type.split(";"):
        segment = segment.strip()
        if segment.lower().startswith("boundary="):
            boundary_value = segment.split("=", 1)[1].strip().strip('"')
            return boundary_value.encode("utf-8")
    raise ValueError("multipart boundary not found")


def _parse_content_disposition(value: str) -> tuple[str | None, str | None]:
    name_match = re.search(r'\bname="([^"]+)"', value)
    filename_match = re.search(r'\bfilename="([^"]*)"', value)
    name = name_match.group(1) if name_match else None
    filename = filename_match.group(1) if filename_match else None
    return name, filename


def parse_multipart_form(
    body: bytes, content_type: str
) -> tuple[dict[str, str], dict[str, tuple[str | None, bytes]]]:
    boundary = _multipart_boundary(content_type)
    delimiter = b"--" + boundary
    raw_parts = body.split(delimiter)
    text_fields: dict[str, str] = {}
    files: dict[str, tuple[str | None, bytes]] = {}
    for raw in raw_parts:
        chunk = raw.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        sep = chunk.find(b"\r\n\r\n")
        if sep == -1:
            continue
        headers_blob = chunk[:sep].decode("utf-8", errors="replace")
        payload = chunk[sep + 4 :]
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        content_disposition: str | None = None
        for line in headers_blob.split("\r\n"):
            lower = line.lower()
            if lower.startswith("content-disposition:"):
                content_disposition = line.split(":", 1)[1].strip()
                break
        if not content_disposition:
            continue
        field_name, filename = _parse_content_disposition(content_disposition)
        if not field_name:
            continue
        if filename is not None:
            files[field_name] = (filename, payload)
        else:
            text_fields[field_name] = payload.decode("utf-8")
    return text_fields, files


@dataclass(frozen=True)
class StreamedPdfUpload:
    filename: str | None
    path: Path
    size: int


@dataclass(frozen=True)
class StreamedMultipartForm:
    text_fields: dict[str, str]
    pdf: StreamedPdfUpload | None


class _MultipartStreamParser:
    def __init__(
        self,
        stream: BinaryIO,
        *,
        boundary: bytes,
        content_length: int,
        chunk_size: int,
    ) -> None:
        self._stream = stream
        self._dash_boundary = b"--" + boundary
        self._part_sep = b"\r\n" + self._dash_boundary
        self._closing = self._dash_boundary + b"--"
        self._content_length = content_length
        self._chunk_size = chunk_size
        self._bytes_read = 0
        self._buffer = bytearray()
        self._overlap = len(self._part_sep) + 8

    def _read_more(self) -> bool:
        if self._bytes_read >= self._content_length:
            return False
        chunk = self._stream.read(
            min(self._chunk_size, self._content_length - self._bytes_read)
        )
        if not chunk:
            raise ValueError("unexpected end of request body")
        self._bytes_read += len(chunk)
        self._buffer.extend(chunk)
        return True

    def _ensure(self, minimum: int) -> None:
        while len(self._buffer) < minimum and self._bytes_read < self._content_length:
            self._read_more()

    def _consume(self, count: int) -> bytes:
        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        return data

    def _drop_through(self, marker: bytes) -> None:
        while True:
            self._ensure(len(marker))
            index = self._buffer.find(marker)
            if index == -1:
                if self._bytes_read >= self._content_length:
                    raise ValueError("multipart form could not be parsed")
                if len(self._buffer) > len(marker):
                    del self._buffer[: len(self._buffer) - len(marker) + 1]
                self._read_more()
                continue
            self._consume(index + len(marker))
            return

    def _read_part_headers(self) -> tuple[str | None, str | None]:
        while True:
            self._ensure(4)
            sep = self._buffer.find(b"\r\n\r\n")
            if sep == -1:
                if self._bytes_read >= self._content_length:
                    raise ValueError("multipart form could not be parsed")
                self._read_more()
                continue
            header_blob = self._consume(sep + 4).decode("utf-8", errors="replace")
            content_disposition: str | None = None
            for line in header_blob.split("\r\n"):
                if line.lower().startswith("content-disposition:"):
                    content_disposition = line.split(":", 1)[1].strip()
                    break
            if not content_disposition:
                continue
            return _parse_content_disposition(content_disposition)

    def _read_text_part(self) -> bytes:
        collected = bytearray()
        while True:
            self._ensure(self._overlap)
            index = self._buffer.find(self._part_sep)
            if index == -1:
                if self._bytes_read >= self._content_length:
                    closing = self._buffer.find(self._closing)
                    if closing == -1:
                        raise ValueError("multipart form could not be parsed")
                    payload = self._consume(closing)
                    if len(collected) + len(payload) > _MAX_TEXT_FIELD_BYTES:
                        raise ValueError("multipart text field too large")
                    collected.extend(payload)
                    return bytes(collected)
                flush_len = max(0, len(self._buffer) - self._overlap)
                if flush_len:
                    chunk = self._consume(flush_len)
                    if len(collected) + len(chunk) > _MAX_TEXT_FIELD_BYTES:
                        raise ValueError("multipart text field too large")
                    collected.extend(chunk)
                self._read_more()
                continue
            payload = self._consume(index)
            if len(collected) + len(payload) > _MAX_TEXT_FIELD_BYTES:
                raise ValueError("multipart text field too large")
            collected.extend(payload)
            return bytes(collected)

    def _stream_file_part(self, destination: Path) -> int:
        destination.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with destination.open("wb") as handle:
            while True:
                self._ensure(self._overlap)
                index = self._buffer.find(self._part_sep)
                if index == -1:
                    if self._bytes_read >= self._content_length:
                        closing = self._buffer.find(self._closing)
                        if closing == -1:
                            raise ValueError("multipart form could not be parsed")
                        payload = self._consume(closing)
                        if payload:
                            handle.write(payload)
                            written += len(payload)
                        return written
                    flush_len = max(0, len(self._buffer) - self._overlap)
                    if flush_len:
                        chunk = self._consume(flush_len)
                        handle.write(chunk)
                        written += len(chunk)
                    self._read_more()
                    continue
                payload = self._consume(index)
                if payload:
                    handle.write(payload)
                    written += len(payload)
                return written

    def parse(
        self,
        *,
        pdf_part_path: Path,
    ) -> StreamedMultipartForm:
        self._drop_through(self._dash_boundary)
        if self._buffer.startswith(b"\r\n"):
            self._consume(2)
        elif self._buffer.startswith(b"--"):
            raise ValueError("multipart form could not be parsed")

        text_fields: dict[str, str] = {}
        pdf_upload: StreamedPdfUpload | None = None

        while True:
            if self._buffer.startswith(self._closing):
                self._consume(len(self._closing))
                break
            field_name, filename = self._read_part_headers()
            if not field_name:
                continue
            if filename is not None:
                size = self._stream_file_part(pdf_part_path)
                pdf_upload = StreamedPdfUpload(
                    filename=filename or None,
                    path=pdf_part_path,
                    size=size,
                )
            else:
                payload = self._read_text_part()
                text_fields[field_name] = payload.decode("utf-8")

            if self._buffer.startswith(b"\r\n"):
                self._consume(2)
            if self._buffer.startswith(self._closing):
                self._consume(len(self._closing))
                break
            if not self._buffer.startswith(self._dash_boundary):
                self._drop_through(self._part_sep)
            else:
                self._consume(len(self._dash_boundary))
                if self._buffer.startswith(b"\r\n"):
                    self._consume(2)

        if self._bytes_read < self._content_length:
            drain = self._content_length - self._bytes_read
            leftover = self._stream.read(drain)
            if len(leftover) != drain:
                raise ValueError("unexpected end of request body")

        return StreamedMultipartForm(text_fields=text_fields, pdf=pdf_upload)


def parse_multipart_form_stream(
    stream: BinaryIO,
    content_type: str,
    *,
    content_length: int,
    max_bytes: int,
    pdf_part_path: Path,
    chunk_size: int = _STREAM_CHUNK_SIZE,
) -> StreamedMultipartForm:
    if content_length < 0 or content_length > max_bytes:
        raise ValueError("invalid Content-Length")
    boundary = _multipart_boundary(content_type)
    parser = _MultipartStreamParser(
        stream,
        boundary=boundary,
        content_length=content_length,
        chunk_size=chunk_size,
    )
    return parser.parse(pdf_part_path=pdf_part_path)


def _parse_pages_spec(raw: str) -> list[int]:
    if not raw.strip():
        return []
    pages: set[int] = set()
    for piece in raw.replace(";", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "-" in piece:
            left, right = piece.split("-", 1)
            try:
                start = int(left.strip())
                end = int(right.strip())
            except ValueError as exc:
                raise InvalidPagesSpec(f"invalid page range token: {piece!r}") from exc
            if start < 1 or end < 1:
                raise InvalidPagesSpec("page numbers must be >= 1")
            if start > end:
                raise InvalidPagesSpec("in a page range x-y, x must be <= y")
            pages.update(range(start, end + 1))
        else:
            try:
                n = int(piece)
            except ValueError as exc:
                raise InvalidPagesSpec(f"invalid page token: {piece!r}") from exc
            if n < 1:
                raise InvalidPagesSpec("page numbers must be >= 1")
            pages.add(n)
    return sorted(pages)


def _sorted_pages_form_single_interval(pages: list[int]) -> bool:
    if len(pages) <= 1:
        return True
    expected = pages[-1] - pages[0] + 1
    return expected == len(pages)


def _parse_contiguous_range_field(raw: str, field: str) -> tuple[int, int]:
    stripped = raw.strip()
    if not stripped:
        raise InvalidRangeField(field, "value is required")
    try:
        pages = _parse_pages_spec(stripped)
    except InvalidPagesSpec as exc:
        raise InvalidRangeField(field, str(exc)) from exc
    if not pages:
        raise InvalidRangeField(field, "value is required")
    if not _sorted_pages_form_single_interval(pages):
        raise InvalidRangeField(
            field,
            "must be a single contiguous interval (e.g. 10-18 or 10,11,12)",
        )
    return pages[0], pages[-1]


def _split_str_list(raw: str) -> list[str] | None:
    pieces = [segment.strip() for segment in raw.replace(";", ",").split(",")]
    cleaned = [segment for segment in pieces if segment]
    return cleaned or None


def _optional_trimmed(fields: dict[str, str], key: str) -> str | None:
    raw = fields.get(key, "").strip()
    return raw or None


def build_ingest_payload_from_form(fields: dict[str, str]) -> dict[str, Any]:
    pages_raw = fields.get("pages_to_remove", "").strip()
    toc_spec = fields.get("toc_range", "").strip()
    index_spec = fields.get("index_range", "").strip()
    toc_start, toc_end = _parse_contiguous_range_field(toc_spec, "toc_range")
    index_start, index_end = _parse_contiguous_range_field(index_spec, "index_range")

    reicat_payload: dict[str, Any] = {
        "titolo": fields.get("titolo", "").strip(),
        "autore": _split_str_list(fields.get("autore", "")) or [],
    }

    subtitle = _optional_trimmed(fields, "sottotitolo")
    complements = _optional_trimmed(fields, "complementi_del_titolo")
    editors = _split_str_list(fields.get("curatore", "") or "")
    translators = _split_str_list(fields.get("traduttore", "") or "")
    edition = _optional_trimmed(fields, "numero_edizione")
    publication_year_raw = fields.get("anno_di_pubblicazione", "").strip()
    publication_type = _optional_trimmed(fields, "tipo_di_pubblicazione")
    publication_place = _optional_trimmed(fields, "luogo_di_pubblicazione")
    publisher = _optional_trimmed(fields, "editore")
    page_count_raw = fields.get("numero_pagine", "").strip()
    series_title = _optional_trimmed(fields, "titolo_collana")
    series_number = _optional_trimmed(fields, "numero_nella_collana")
    isbn = _optional_trimmed(fields, "isbn")

    if subtitle:
        reicat_payload["sottotitolo"] = subtitle
    if complements:
        reicat_payload["complementi_del_titolo"] = complements
    if editors:
        reicat_payload["curatore"] = editors
    if translators:
        reicat_payload["traduttore"] = translators
    if edition:
        reicat_payload["numero_edizione"] = edition
    if publication_year_raw:
        reicat_payload["anno_di_pubblicazione"] = int(publication_year_raw)
    if publication_type:
        reicat_payload["tipo_di_pubblicazione"] = publication_type
    if publication_place:
        reicat_payload["luogo_di_pubblicazione"] = publication_place
    if publisher:
        reicat_payload["editore"] = publisher
    if page_count_raw:
        reicat_payload["numero_pagine"] = int(page_count_raw)
    if series_title:
        reicat_payload["titolo_collana"] = series_title
    if series_number:
        reicat_payload["numero_nella_collana"] = series_number
    if isbn:
        reicat_payload["isbn"] = isbn

    book_id_hint_raw = fields.get("book_id_hint", "").strip()
    notes_raw = fields.get("notes", "").strip()
    index_notes_raw = fields.get("index_notes", "").strip()
    page_notes_raw = fields.get("page_notes", "").strip()
    force_meta = fields.get("force_metadata_update_on_duplicate_hash")
    if force_meta is None:
        force_flag = True
    else:
        force_flag = str(force_meta).lower() in ("1", "true", "on", "yes")

    ingest_payload: dict[str, Any] = {
        "schema_version": "1.0",
        "pages_to_remove": _parse_pages_spec(pages_raw) if pages_raw else [],
        "toc_range": {"start": toc_start, "end": toc_end},
        "index_range": {"start": index_start, "end": index_end},
        "reicat": reicat_payload,
        "options": {"force_metadata_update_on_duplicate_hash": force_flag},
    }
    if book_id_hint_raw:
        ingest_payload["book_id_hint"] = book_id_hint_raw
    if notes_raw:
        ingest_payload["notes"] = notes_raw
    if index_notes_raw:
        ingest_payload["index_notes"] = index_notes_raw
    if page_notes_raw:
        ingest_payload["page_notes"] = page_notes_raw
    return ingest_payload
