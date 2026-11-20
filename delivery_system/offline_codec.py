"""Phrase dictionaries and the two-instruction body codec.

A frozen translation chunk is a UTF-8 string.  For offline delivery it is
encoded as a *body*: a sequence of instructions over the raw UTF-8 bytes.

Instruction set (both instruction kinds are one-byte distinguished):

* **literal** ``0x01..0x7F`` -- a length byte in ``1..127`` followed by that
  many raw bytes (so a literal instruction is ``1 + L`` bytes).
* **dictionary** ``0x80..0xFE`` -- one byte standing for item ``opcode-0x80``
  of the selected dictionary (items are indexed from 0).  ``0xFF`` and
  ``0x00`` are not valid instruction heads.

Literal heads all have the high bit clear, dictionary heads all have it set,
so the two kinds can never be confused.  127 dictionary items fit in
``0x80..0xFE``.

A dictionary body is a different, self-delimiting format: one length byte
(``1..127``) plus the item bytes for every item, in order.  That makes a
single item at most 127 bytes and the dictionary body size
``sum(1 + len(item))``.

The encoder computes the *shortest* body that reproduces the exact original
UTF-8 bytes, either with pure literals or with one dictionary (mixing literal
and dictionary instructions freely).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

LITERAL_MIN = 1
LITERAL_MAX = 127
DICT_OPCODE_BASE = 0x80
DICT_OPCODE_LAST = 0xFE
MAX_DICTIONARY_ITEMS = 127
MAX_ITEM_BYTES = 127


class CodecError(ValueError):
    """A body or dictionary body is malformed / cannot be decoded."""


class DictionaryError(ValueError):
    """A phrase dictionary violates the accepted bounds."""


# ----------------------------------------------------------------------
# identity helpers (domain separated so kinds/dependencies never collide)
# ----------------------------------------------------------------------
def dictionary_object_id(body: bytes) -> str:
    return hashlib.sha256(b"DICT\x00" + body).hexdigest()


def chunk_object_id(dependency: Optional[str], body: bytes) -> str:
    """Chunk object identity includes its dictionary dependency.

    The same instruction bytes under two different dictionaries are *not* the
    same usable representation, so the dependency is part of the identity.
    """

    dep = (dependency or "").encode("utf-8")
    return hashlib.sha256(b"CHUNK\x00" + dep + b"\x00" + body).hexdigest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ----------------------------------------------------------------------
# phrase dictionaries
# ----------------------------------------------------------------------
def _validate_items(items: Sequence[bytes]) -> tuple[bytes, ...]:
    if not (1 <= len(items) <= MAX_DICTIONARY_ITEMS):
        raise DictionaryError(
            f"a dictionary must hold 1..{MAX_DICTIONARY_ITEMS} items, got {len(items)}"
        )
    seen: set[bytes] = set()
    normalised: list[bytes] = []
    for item in items:
        if not isinstance(item, (bytes, bytearray)):
            raise DictionaryError("dictionary items must be bytes")
        raw = bytes(item)
        if not raw:
            raise DictionaryError("dictionary items must be non-empty")
        if len(raw) > MAX_ITEM_BYTES:
            raise DictionaryError(
                f"item is {len(raw)} bytes; a single item is at most "
                f"{MAX_ITEM_BYTES} bytes"
            )
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DictionaryError("dictionary items must be valid UTF-8") from exc
        if raw in seen:
            raise DictionaryError("dictionary items must not repeat")
        seen.add(raw)
        normalised.append(raw)
    return tuple(normalised)


@dataclass(frozen=True)
class PhraseDictionary:
    """An immutable, ordered phrase dictionary.

    ``dictionary_id`` is the publisher's label; the object identity is derived
    only from the canonical body, so two labels with identical content denote
    the same on-device object.
    """

    dictionary_id: str
    items: tuple[bytes, ...]

    def __post_init__(self) -> None:
        if not self.dictionary_id:
            raise DictionaryError("dictionary_id must be non-empty")
        object.__setattr__(self, "items", _validate_items(tuple(self.items)))

    @classmethod
    def from_strings(cls, dictionary_id: str, items: Iterable[str]) -> "PhraseDictionary":
        return cls(
            dictionary_id=dictionary_id,
            items=tuple(item.encode("utf-8") for item in items),
        )

    @property
    def body(self) -> bytes:
        out = bytearray()
        for item in self.items:
            out.append(len(item))
            out += item
        return bytes(out)

    @property
    def size(self) -> int:
        return sum(1 + len(item) for item in self.items)

    @property
    def content_sha256(self) -> str:
        return sha256_hex(self.body)

    @property
    def source_sha256(self) -> str:
        # A dictionary has no source text; its canonical item body is its digest.
        return self.content_sha256

    @property
    def object_id(self) -> str:
        return dictionary_object_id(self.body)

    def to_manifest_entry(self) -> dict[str, object]:
        return {
            "dictionary_id": self.dictionary_id,
            "object_id": self.object_id,
            "size": self.size,
            "item_count": len(self.items),
            "dependencies": [],
            "content_sha256": self.content_sha256,
            "source_sha256": self.source_sha256,
        }


def parse_dictionary_body(body: bytes) -> tuple[bytes, ...]:
    """Parse the canonical dictionary body back into ordered items."""

    items: list[bytes] = []
    i = 0
    n = len(body)
    while i < n:
        length = body[i]
        i += 1
        if not (LITERAL_MIN <= length <= MAX_ITEM_BYTES):
            raise CodecError(f"invalid dictionary item length {length} at offset {i - 1}")
        if i + length > n:
            raise CodecError("dictionary body ends inside an item")
        items.append(body[i : i + length])
        i += length
    if not (1 <= len(items) <= MAX_DICTIONARY_ITEMS):
        raise CodecError(
            f"dictionary body holds {len(items)} items; expected 1..{MAX_DICTIONARY_ITEMS}"
        )
    return _validate_items(items)


# ----------------------------------------------------------------------
# shortest-body encoder / exact decoder
# ----------------------------------------------------------------------
def shortest_body(data: bytes, items: Optional[Sequence[bytes]] = None) -> bytes:
    """Return the shortest instruction body reproducing ``data`` exactly.

    With ``items=None`` only literal instructions are used.  With a dictionary,
    literal and dictionary instructions are mixed as needed.  Ties are resolved
    deterministically (prefer a dictionary instruction, then the lowest item
    index, then the shortest literal).
    """

    n = len(data)
    vocab = tuple(items or ())
    if len(vocab) > MAX_DICTIONARY_ITEMS:
        raise DictionaryError(
            f"dictionary holds {len(vocab)} items; at most {MAX_DICTIONARY_ITEMS}"
        )
    inf = 1 << 60
    cost = [inf] * (n + 1)
    choice: list[Optional[tuple[str, int]]] = [None] * (n + 1)
    cost[n] = 0
    for i in range(n - 1, -1, -1):
        best = inf
        best_choice: Optional[tuple[str, int]] = None
        max_literal = min(LITERAL_MAX, n - i)
        # Literal: 1 length byte + L bytes of payload, then the rest.
        for length in range(1, max_literal + 1):
            candidate = 1 + length + cost[i + length]
            if candidate < best:
                best = candidate
                best_choice = ("L", length)
        # Dictionary item: exactly one byte, then the rest.
        for index, item in enumerate(vocab):
            if not item or not data.startswith(item, i):
                continue
            candidate = 1 + cost[i + len(item)]
            # Prefer a dictionary instruction on ties.
            if candidate < best or (candidate == best and (best_choice is None or best_choice[0] == "L")):
                best = candidate
                best_choice = ("D", index)
        cost[i] = best
        choice[i] = best_choice

    if cost[0] >= inf:
        raise CodecError("no instruction sequence reproduces the input")

    out = bytearray()
    i = 0
    while i < n:
        current = choice[i]
        if current is None:  # pragma: no cover - defensive
            raise AssertionError("broken shortest-body reconstruction")
        kind, value = current
        if kind == "L":
            out.append(value)
            out += data[i : i + value]
            i += value
        else:
            out.append(DICT_OPCODE_BASE + value)
            i += len(vocab[value])
    return bytes(out)


def decode_body(body: bytes, items: Optional[Sequence[bytes]] = None) -> bytes:
    """Decode a body back to the exact original bytes."""

    vocab = tuple(items or ())
    out = bytearray()
    i = 0
    n = len(body)
    while i < n:
        head = body[i]
        i += 1
        if LITERAL_MIN <= head <= LITERAL_MAX:
            if i + head > n:
                raise CodecError("literal instruction runs past the end of the body")
            out += body[i : i + head]
            i += head
        elif DICT_OPCODE_BASE <= head <= DICT_OPCODE_LAST:
            index = head - DICT_OPCODE_BASE
            if index >= len(vocab):
                raise CodecError(
                    f"dictionary instruction index {index} is out of range "
                    f"for a {len(vocab)}-item dictionary"
                )
            out += vocab[index]
        else:
            raise CodecError(f"invalid instruction head 0x{head:02x} at offset {i - 1}")
    return bytes(out)
