"""Firmware image files on disk (data/firmware/), written as they are
uploaded and checksummed on the way in."""

import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._()+-]{0,199}$")


class ImageError(Exception):
    pass


def valid_filename(name: str) -> str:
    name = name.strip()
    if not _FILENAME_RE.match(name) or ".." in name:
        raise ImageError("File name may only use letters, digits and . _ - ( ) + (max 200)")
    return name


def check_expected_hash(value: str) -> str:
    """An optional vendor checksum: MD5 (32 hex) or SHA-512 (128 hex)."""
    value = value.strip().lower()
    if value and not re.fullmatch(r"[0-9a-f]{32}|[0-9a-f]{128}", value):
        raise ImageError("Checksum must be an MD5 (32 hex characters) or SHA-512 (128) value")
    return value


@dataclass
class StoredImage:
    size: int
    md5: str
    sha512: str


class ImageWriter:
    """Streams one upload to a temporary file, hashing as it goes. Call
    finish() to verify and move it into place, or abort() to discard it."""

    def __init__(self, directory: Path, filename: str, max_bytes: int):
        self.directory = directory
        self.final = directory / filename
        self.part = directory / f".{filename}.{secrets.token_hex(4)}.part"
        self.max_bytes = max_bytes
        self.size = 0
        self._md5, self._sha512 = hashlib.md5(), hashlib.sha512()
        directory.mkdir(parents=True, exist_ok=True)
        if self.final.exists():
            raise ImageError("An image with that file name already exists")
        self._fh = open(self.part, "xb")

    def write(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self.size > self.max_bytes:
            raise ImageError(f"Image is larger than the {self.max_bytes // 2**20} MB limit")
        self._fh.write(chunk)
        self._md5.update(chunk)
        self._sha512.update(chunk)

    def finish(self, expected: str = "") -> StoredImage:
        self._fh.close()
        if self.size == 0:
            self.abort()
            raise ImageError("The uploaded file is empty")
        md5, sha512 = self._md5.hexdigest(), self._sha512.hexdigest()
        if expected and expected not in (md5, sha512):
            self.abort()
            raise ImageError("Checksum does not match the uploaded file - "
                             "the download may be corrupt, or the checksum is for another file")
        os.chmod(self.part, 0o640)
        try:
            os.link(self.part, self.final)  # fails if the name was taken meanwhile
        except FileExistsError:
            self.abort()
            raise ImageError("An image with that file name already exists") from None
        self.part.unlink()
        return StoredImage(self.size, md5, sha512)

    def abort(self) -> None:
        if not self._fh.closed:
            self._fh.close()
        self.part.unlink(missing_ok=True)


def delete_image(directory: Path, filename: str) -> None:
    (directory / valid_filename(filename)).unlink(missing_ok=True)
