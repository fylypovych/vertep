"""Issue #282/#308 (#178.3-#178.4): decode-proven CPU media fixtures.

The previous ``MP4`` fixture walked the ISO BMFF box tree but carried a
zero-filled ``mdat``: ``verify_media`` accepted it as ``video/mp4`` while no
decoder could actually decode it — a structural pass is not a decodability
proof.  These fixtures are different:

* ``PNG_VALID`` is a real 2x2 RGB PNG whose pixels are decoded in pure Python
  (zlib + unfilter) on every run;
* ``MP4_DECODEABLE`` is a genuine H.264 (baseline, I_PCM macroblock) frame
  wrapped in a complete MP4 container (``avc1`` + ``avcC`` sample tables with
  a real sample in ``mdat``).  A pure-Python decoder parses the container,
  the SPS, the sample tables and the slice, and reconstructs the exact frame —
  so the decode proof runs everywhere, not only where FFmpeg happens to be
  installed.  When FFmpeg *is* available the same fixture must also decode
  with the real decoder (verified against ffmpeg 7.1);
* the negatives (signature stubs, junk ``mdat``, truncated and corrupted
  variants of the real fixture) are all refused by the decoder.

``MP4_JUNK_MDAT`` is kept as the documented regression input: the transport
gate (``verify_media``) still accepts it structurally, but the decode layer
refuses it — acceptance by the structural gate is never claimed as a
decodability proof.
"""

from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess
import zlib

import pytest


# ── byte/bit plumbing ───────────────────────────────────────────────────────


def _box(box_type: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + box_type + payload


def _full_box(box_type: bytes, version: int, flags: int, payload: bytes) -> bytes:
    return _box(box_type, struct.pack(">B3B", version, flags >> 16 & 0xFF,
                                      flags >> 8 & 0xFF, flags & 0xFF) + payload)


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + chunk_type + payload
            + struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF))


class _BitWriter:
    """MSB-first bit writer with exp-Golomb and byte alignment."""

    def __init__(self) -> None:
        self._bits: list[int] = []

    def u(self, value: int, count: int) -> "_BitWriter":
        for index in range(count - 1, -1, -1):
            self._bits.append((value >> index) & 1)
        return self

    def ue(self, value: int) -> "_BitWriter":
        code = value + 1
        length = code.bit_length()
        self.u(0, length - 1)
        self.u(code, length)
        return self

    def se(self, value: int) -> "_BitWriter":
        return self.ue(2 * value - 1 if value > 0 else -2 * value)

    def align_zero(self) -> "_BitWriter":
        while len(self._bits) % 8:
            self._bits.append(0)
        return self

    def trailing(self) -> "_BitWriter":
        self._bits.append(1)          # rbsp_stop_one_bit
        self.align_zero()
        return self

    def raw_bytes(self, data: bytes) -> "_BitWriter":
        assert len(self._bits) % 8 == 0, "raw payload must be byte aligned"
        for byte in data:
            self.u(byte, 8)
        return self

    def to_bytes(self) -> bytes:
        assert len(self._bits) % 8 == 0
        return bytes(
            sum(bit << (7 - index) for index, bit in enumerate(self._bits[start:start + 8]))
            for start in range(0, len(self._bits), 8))


class _BitReader:
    """MSB-first bit reader with exp-Golomb and emulation prevention."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._position = 0

    def u(self, count: int) -> int:
        value = 0
        for _ in range(count):
            if self._position >= len(self._data) * 8:
                raise MediaDecodeError("bitstream ended before the frame was decoded")
            byte = self._data[self._position // 8]
            value = (value << 1) | ((byte >> (7 - self._position % 8)) & 1)
            self._position += 1
        return value

    def ue(self) -> int:
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
            if zeros > 31:
                raise MediaDecodeError("invalid exp-Golomb code")
        return (1 << zeros) - 1 + (self.u(zeros) if zeros else 0)

    def se(self) -> int:
        code = self.ue()
        return (code + 1) // 2 if code % 2 else -(code // 2)

    def align_zero(self) -> None:
        while self._position % 8:
            self.u(1)

    def read_bytes(self, count: int) -> bytes:
        self.align_zero()
        start = self._position // 8
        if start + count > len(self._data):
            raise MediaDecodeError("bitstream ended before the frame was decoded")
        self._position += count * 8
        return self._data[start:start + count]


class MediaDecodeError(ValueError):
    """The payload cannot be decoded as the media it claims to be."""


def _escape_nal(payload: bytes) -> bytes:
    """Insert emulation-prevention bytes (00 00 0X -> 00 00 03 0X)."""
    out = bytearray()
    zeros = 0
    for byte in payload:
        if zeros >= 2 and byte <= 3:
            out.append(3)
            zeros = 0
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def _unescape_nal(payload: bytes) -> bytes:
    """Remove emulation-prevention bytes."""
    out = bytearray()
    zeros = 0
    for byte in payload:
        if zeros >= 2 and byte == 3:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


# ── PNG fixture ─────────────────────────────────────────────────────────────


#: The exact pixels the PNG fixture encodes (2x2, 8-bit RGB).
PNG_PIXELS = ((30, 40, 50), (30, 40, 50), (30, 40, 50), (30, 40, 50))

#: Structurally valid PNG: IHDR + IDAT (both scanlines) + IEND.
PNG_VALID = (
    b"\x89PNG\r\n\x1a\n"
    + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
    + _png_chunk(b"IDAT", zlib.compress(
        b"".join(b"\x00" + bytes((30, 40, 50)) * 2 for _ in range(2))))
    + _png_chunk(b"IEND", b""))
PNG_TRUNCATED = PNG_VALID[:-1]
PNG_SIGNATURE_ONLY = b"\x89PNG\r\n\x1a\n"


def decode_png(data: bytes) -> tuple[int, int, list[list[tuple[int, int, int]]]]:
    """Decode a PNG fixture to its pixels (pure Python, no imaging library).

    The IDAT stream is decompressed, the per-scanline filters are undone and
    the exact pixel values are reconstructed, so a fabricated or corrupt
    payload cannot pass as a decodable image.
    """
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise MediaDecodeError("not a PNG signature")
    offset = 8
    idat = bytearray()
    header: dict[str, int] = {}
    seen: list[bytes] = []
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        chunk_type = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise MediaDecodeError("truncated PNG chunk stream")
        payload = data[offset + 8:offset + 8 + length]
        stored_crc = int.from_bytes(data[offset + 8 + length:end], "big")
        if zlib.crc32(chunk_type + payload) & 0xFFFFFFFF != stored_crc:
            raise MediaDecodeError("PNG chunk CRC mismatch")
        if chunk_type == b"IHDR":
            if length != 13:
                raise MediaDecodeError("invalid IHDR")
            header = {"width": int.from_bytes(payload[0:4], "big"),
                      "height": int.from_bytes(payload[4:8], "big"),
                      "depth": payload[8], "color": payload[9]}
        elif chunk_type == b"IDAT":
            idat.extend(payload)
        seen.append(chunk_type)
        offset = end
        if chunk_type == b"IEND":
            break
    if not seen or seen[0] != b"IHDR" or b"IDAT" not in seen or seen[-1] != b"IEND":
        raise MediaDecodeError("incomplete PNG chunk stream")
    if not header or header["color"] != 2 or header["depth"] != 8:
        raise MediaDecodeError("unsupported PNG pixel format")
    width, height = header["width"], header["height"]
    if width < 1 or height < 1 or width * height > 1 << 20:
        raise MediaDecodeError("invalid PNG dimensions")
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as error:
        raise MediaDecodeError(f"IDAT does not decompress: {error}") from None
    if len(raw) != height * (1 + width * 3):
        raise MediaDecodeError("decompressed scanline stream has the wrong size")
    pixels: list[list[tuple[int, int, int]]] = []
    stride = width * 3
    previous = bytearray(stride)
    position = 0
    for _row in range(height):
        filter_type = raw[position]
        position += 1
        scanline = bytearray(raw[position:position + stride])
        if len(scanline) != stride:
            raise MediaDecodeError("truncated scanline")
        position += stride
        for index in range(stride):
            left = scanline[index - 3] if index >= 3 else 0
            up = previous[index]
            up_left = previous[index - 3] if index >= 3 else 0
            if filter_type == 0:
                pass
            elif filter_type == 1:
                scanline[index] = (scanline[index] + left) & 0xFF
            elif filter_type == 2:
                scanline[index] = (scanline[index] + up) & 0xFF
            elif filter_type == 3:
                scanline[index] = (scanline[index] + (left + up) // 2) & 0xFF
            elif filter_type == 4:
                p = left + up - up_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - up_left)
                predictor = left if pa <= pb and pa <= pc else (up if pb <= pc else up_left)
                scanline[index] = (scanline[index] + predictor) & 0xFF
            else:
                raise MediaDecodeError(f"unsupported PNG filter {filter_type}")
        previous = scanline
        pixels.append([tuple(scanline[column:column + 3]) for column in range(0, stride, 3)])
    return width, height, pixels


# ── H.264 / MP4 fixture builders ────────────────────────────────────────────


#: The exact luma/chroma samples the I_PCM frame carries.
FRAME_LUMA = bytes(((x * 16 + y) & 0xFF for y in range(16) for x in range(16)))
FRAME_CB = bytes(80 + (index % 16) for index in range(64))
FRAME_CR = bytes(160 + (index % 16) for index in range(64))


def _sps_nal() -> bytes:
    """Baseline SPS for a 16x16 4:2:0 picture (profile 66, level 1.0)."""
    writer = _BitWriter()
    writer.u(66, 8)        # profile_idc: baseline
    writer.u(0x80, 8)      # constraint_set0_flag=1
    writer.u(10, 8)        # level_idc 1.0
    writer.ue(0)           # seq_parameter_set_id
    writer.ue(0)           # log2_max_frame_num_minus4
    writer.ue(0)           # pic_order_cnt_type = 0
    writer.ue(0)           # log2_max_pic_order_cnt_lsb_minus4
    writer.ue(0)           # max_num_ref_frames
    writer.u(0, 1)         # gaps_in_frame_num_value_allowed_flag
    writer.ue(0)           # pic_width_in_mbs_minus1  -> 16 px
    writer.ue(0)           # pic_height_in_map_units_minus1 -> 16 px
    writer.u(1, 1)         # frame_mbs_only_flag
    writer.u(1, 1)         # direct_8x8_inference_flag
    writer.u(0, 1)         # frame_cropping_flag
    writer.u(0, 1)         # vui_parameters_present_flag
    writer.trailing()
    return _escape_nal(bytes([0x67]) + writer.to_bytes())


def _pps_nal() -> bytes:
    writer = _BitWriter()
    writer.ue(0)           # pic_parameter_set_id
    writer.ue(0)           # seq_parameter_set_id
    writer.u(0, 1)         # entropy_coding_mode_flag: CAVLC
    writer.u(0, 1)         # bottom_field_pic_order_in_frame_present_flag
    writer.ue(0)           # num_slice_groups_minus1
    writer.ue(0)           # num_ref_idx_l0_default_active_minus1
    writer.ue(0)           # num_ref_idx_l1_default_active_minus1
    writer.u(0, 1)         # weighted_pred_flag
    writer.u(0, 2)         # weighted_bipred_idc
    writer.se(0)           # pic_init_qp_minus26
    writer.se(0)           # pic_init_qs_minus26
    writer.se(0)           # chroma_qp_index_offset
    writer.u(0, 1)         # deblocking_filter_control_present_flag
    writer.u(0, 1)         # constrained_intra_pred_flag
    writer.u(0, 1)         # redundant_pic_cnt_present_flag
    writer.trailing()
    return _escape_nal(bytes([0x68]) + writer.to_bytes())


def _idr_nal() -> bytes:
    """One IDR slice whose single macroblock is I_PCM (raw sample bytes)."""
    writer = _BitWriter()
    writer.ue(0)           # first_mb_in_slice
    writer.ue(7)           # slice_type = I (all slices)
    writer.ue(0)           # pic_parameter_set_id
    writer.u(0, 4)         # frame_num (log2_max_frame_num = 4)
    writer.ue(0)           # idr_pic_id
    writer.u(0, 4)         # pic_order_cnt_lsb (log2_max_poc_lsb = 4)
    writer.u(0, 1)         # no_output_of_prior_pics_flag
    writer.u(0, 1)         # long_term_reference_flag
    writer.se(0)           # slice_qp_delta
    writer.ue(25)          # mb_type = I_PCM
    writer.align_zero()    # pcm_alignment_zero_bits
    writer.raw_bytes(FRAME_LUMA)
    writer.raw_bytes(FRAME_CB)
    writer.raw_bytes(FRAME_CR)
    writer.trailing()
    return _escape_nal(bytes([0x65]) + writer.to_bytes())


def _avcc_box() -> bytes:
    sps, pps = _sps_nal(), _pps_nal()
    payload = (bytes([1, 66, 0x80, 10, 0xFF, 0xE1])
               + struct.pack(">H", len(sps)) + sps
               + b"\x01" + struct.pack(">H", len(pps)) + pps)
    return _box(b"avcC", payload)


def _matrix36() -> bytes:
    return struct.pack(">9i", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)


def _moov_box(sample_size: int, mdat_payload_offset: int) -> bytes:
    sample_entry = (
        b"\x00" * 6 + struct.pack(">H", 1)              # reserved + data_ref_index
        + b"\x00" * 16                                  # pre_defined / reserved
        + struct.pack(">HH", 16, 16)                     # width / height
        + struct.pack(">II", 0x00480000, 0x00480000)    # dpi
        + b"\x00" * 4                                   # reserved
        + struct.pack(">H", 1)                           # frame_count
        + bytes([5]) + b"probe" + b"\x00" * 26           # compressorname
        + struct.pack(">hh", 0x0018, -1)                 # depth, pre_defined
        + _avcc_box())
    stsd = _full_box(b"stsd", 0, 0, struct.pack(">I", 1) + _box(b"avc1", sample_entry))
    stts = _full_box(b"stts", 0, 0, struct.pack(">IIII", 1, 1, 1, 1000))
    stsc = _full_box(b"stsc", 0, 0, struct.pack(">IIII", 1, 1, 1, 1))
    stsz = _full_box(b"stsz", 0, 0, struct.pack(">III", 0, 1, sample_size))
    stco = _full_box(b"stco", 0, 0, struct.pack(">II", 1, mdat_payload_offset))
    stbl = _box(b"stbl", stsd + stts + stsc + stsz + stco)
    vmhd = _full_box(b"vmhd", 0, 1, struct.pack(">HHHH", 0, 0, 0, 0))
    dref = _full_box(b"dref", 0, 0, struct.pack(">I", 1) + _full_box(b"url ", 0, 1, b""))
    minf = _box(b"minf", vmhd + _box(b"dinf", dref) + stbl)
    hdlr = _full_box(b"hdlr", 0, 0, b"\x00" * 4 + b"vide" + b"\x00" * 12 + b"probe\x00")
    mdhd = _full_box(b"mdhd", 0, 0, struct.pack(">IIII", 0, 0, 1000, 1000)
                     + struct.pack(">HH", 0x55C4, 0))
    mdia = _box(b"mdia", mdhd + hdlr + minf)
    tkhd = _full_box(b"tkhd", 0, 7,
                     struct.pack(">IIIII", 0, 0, 1, 0, 1000) + b"\x00" * 8
                     + struct.pack(">hhhh", 0, 0, 0, 0) + _matrix36()
                     + struct.pack(">II", 16 << 16, 16 << 16))
    mvhd = _full_box(b"mvhd", 0, 0,
                     struct.pack(">IIII", 0, 0, 1000, 1000)
                     + struct.pack(">IH", 0x00010000, 0x0100) + b"\x00" * 10
                     + _matrix36() + b"\x00" * 24 + struct.pack(">I", 2))
    return _box(b"moov", mvhd + _box(b"trak", tkhd + mdia))


def _build_mp4(sample: bytes) -> bytes:
    """A complete MP4 around one AVCC sample (4-byte NAL length prefix)."""
    ftyp = _box(b"ftyp", b"isom\x00\x00\x02\x00" + b"isomiso2avc1mp41")
    moov = _moov_box(len(sample), len(ftyp) + len(_moov_box(len(sample), 0)) + 8)
    return ftyp + moov + _box(b"mdat", sample)


#: A genuinely decodable video: one real H.264 baseline I_PCM frame in MP4.
MP4_DECODEABLE = _build_mp4(struct.pack(">I", len(_idr_nal())) + _idr_nal())

#: The former "valid" stub: real box tree, junk zero-filled ``mdat``, no
#: sample tables.  The transport gate accepts it structurally; the decode
#: layer must refuse it (regression input of the #308 audit).
_HDLR = _box(b"hdlr", b"\x00" * 8 + b"vide" + b"\x00" * 12 + b"\x00")
_MOOV_JUNK = _box(b"moov", _box(b"trak", _box(b"mdia", _HDLR)))
MP4_JUNK_MDAT = (_box(b"ftyp", b"isom\x00\x00\x02\x00" + b"isomiso2avc1mp41")
                 + _MOOV_JUNK + _box(b"mdat", b"\x00" * 64))
MP4_FTYP_ONLY = b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x00"
MP4_FTYP_PLUS_FREE = (_box(b"ftyp", b"isom\x00\x00\x02\x00" + b"isomiso2avc1mp41")
                      + _box(b"free", b"\x00" * 8))
MP4_TRUNCATED_FTYP = b"\x00\x00\x00\x0cftypmp42"


# ── pure-Python MP4/H.264 decoder ───────────────────────────────────────────


def _walk_boxes(data: bytes, start: int, end: int) -> list[tuple[bytes, int, int]]:
    """Return ``(type, payload_start, payload_end)`` for each box in a range."""
    boxes = []
    offset = start
    while offset + 8 <= end:
        size = int.from_bytes(data[offset:offset + 4], "big")
        box_type = data[offset + 4:offset + 8]
        header = 8
        if size == 1:
            if offset + 16 > end:
                raise MediaDecodeError("truncated extended box header")
            size = int.from_bytes(data[offset + 8:offset + 16], "big")
            header = 16
        elif size == 0:
            size = end - offset
        if size < header or offset + size > end:
            raise MediaDecodeError("truncated or implausible box")
        boxes.append((box_type, offset + header, offset + size))
        offset += size
    if offset != end:
        raise MediaDecodeError("trailing bytes do not form complete boxes")
    return boxes


def _find_box(boxes: list[tuple[bytes, int, int]], box_type: bytes):
    for candidate in boxes:
        if candidate[0] == box_type:
            return candidate
    raise MediaDecodeError(f"required box {box_type!r} is missing")


def _parse_sps(nal: bytes) -> tuple[int, int]:
    """Return ``(width, height)`` decoded from a baseline SPS."""
    rbsp = _unescape_nal(nal)
    if not rbsp or rbsp[0] & 0x80:
        raise MediaDecodeError("SPS forbidden bit is set")
    if rbsp[0] & 0x1F != 7:
        raise MediaDecodeError("not a sequence parameter set")
    reader = _BitReader(rbsp[1:])
    profile = reader.u(8)
    if profile not in (66, 77, 88):
        raise MediaDecodeError(f"unsupported H.264 profile {profile}")
    reader.u(8)                        # constraint flags
    reader.u(8)                        # level_idc
    reader.ue()                        # seq_parameter_set_id
    if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128):
        raise MediaDecodeError("high-profile chroma layout is not supported")
    reader.ue()                        # log2_max_frame_num_minus4
    poc_type = reader.ue()
    if poc_type == 0:
        reader.ue()                    # log2_max_pic_order_cnt_lsb_minus4
    elif poc_type == 1:
        raise MediaDecodeError("unsupported pic_order_cnt_type")
    reader.ue()                        # max_num_ref_frames
    reader.u(1)                        # gaps_in_frame_num_value_allowed_flag
    width_mbs = reader.ue() + 1
    height_map_units = reader.ue() + 1
    frame_mbs_only = reader.u(1)
    if not frame_mbs_only:
        raise MediaDecodeError("interlaced pictures are not supported")
    reader.u(1)                        # direct_8x8_inference_flag
    if reader.u(1):                    # frame_cropping_flag
        reader.ue(), reader.ue(), reader.ue(), reader.ue()
    if reader.u(1):                    # vui_parameters_present_flag
        raise MediaDecodeError("VUI payload is not expected in the fixture")
    return width_mbs * 16, height_map_units * 16


def decode_mp4_frame(data: bytes) -> dict:
    """Decode the single H.264 frame an MP4 payload carries (pure Python).

    The whole chain is parsed for real: the top-level box tree, the video
    track's sample tables (``stsd``/``stts``/``stsc``/``stsz``/``stco``), the
    ``avcC`` parameter sets, the SPS picture geometry, the AVCC sample in
    ``mdat`` and the slice with its I_PCM macroblock.  Any junk, truncation
    or corruption raises :class:`MediaDecodeError`, so acceptance of a video
    payload can be proven by actually decoding it instead of trusting the
    container signature.
    """
    if len(data) < 16 or data[4:8] != b"ftyp":
        raise MediaDecodeError("not an ISO BMFF payload")
    top = _walk_boxes(data, 0, len(data))
    _find_box(top, b"ftyp")
    _mdat = _find_box(top, b"mdat")
    moov = _find_box(top, b"moov")
    moov_boxes = _walk_boxes(data, moov[1], moov[2])
    trak = _find_box(moov_boxes, b"trak")
    trak_boxes = _walk_boxes(data, trak[1], trak[2])
    mdia = _find_box(trak_boxes, b"mdia")
    mdia_boxes = _walk_boxes(data, mdia[1], mdia[2])
    hdlr = _find_box(mdia_boxes, b"hdlr")
    if data[hdlr[1] + 8:hdlr[1] + 12] != b"vide":
        raise MediaDecodeError("the track is not a video track")
    minf = _find_box(mdia_boxes, b"minf")
    minf_boxes = _walk_boxes(data, minf[1], minf[2])
    stbl = _find_box(minf_boxes, b"stbl")
    stbl_boxes = _walk_boxes(data, stbl[1], stbl[2])

    stsd = _find_box(stbl_boxes, b"stsd")
    entries = int.from_bytes(data[stsd[1] + 4:stsd[1] + 8], "big")
    if entries < 1:
        raise MediaDecodeError("the sample description table is empty")
    first_entry = _walk_boxes(data, stsd[1] + 8, stsd[2])[0]
    if first_entry[0] != b"avc1":
        raise MediaDecodeError("the video sample entry is not avc1")
    entry_children = _walk_boxes(data, first_entry[1] + 78, first_entry[2])
    avcc = _find_box(entry_children, b"avcC")
    if data[avcc[1]] != 1:
        raise MediaDecodeError("unsupported avcC configuration version")
    length_size = (data[avcc[1] + 4] & 3) + 1
    num_sps = data[avcc[1] + 5] & 0x1F
    if num_sps < 1:
        raise MediaDecodeError("the avcC carries no sequence parameter set")
    position = avcc[1] + 6
    sps_size = int.from_bytes(data[position:position + 2], "big")
    position += 2
    width, height = _parse_sps(data[position:position + sps_size])
    position += sps_size
    num_pps = data[position]
    position += 1
    if num_pps < 1:
        raise MediaDecodeError("the avcC carries no picture parameter set")
    pps_size = int.from_bytes(data[position:position + 2], "big")
    pps = data[position + 2:position + 2 + pps_size]
    if not pps or _unescape_nal(pps)[0] & 0x1F != 8:
        raise MediaDecodeError("malformed picture parameter set")

    stsz = _find_box(stbl_boxes, b"stsz")
    sample_size = int.from_bytes(data[stsz[1] + 4:stsz[1] + 8], "big")
    sample_count = int.from_bytes(data[stsz[1] + 8:stsz[1] + 12], "big")
    if sample_size == 0:
        if sample_count < 1:
            raise MediaDecodeError("the sample table is empty")
        sample_size = int.from_bytes(data[stsz[1] + 12:stsz[1] + 16], "big")
    if sample_count != 1:
        raise MediaDecodeError("the fixture decoder accepts exactly one sample")
    stco = _find_box(stbl_boxes, b"stco")
    chunk_count = int.from_bytes(data[stco[1] + 4:stco[1] + 8], "big")
    if chunk_count < 1:
        raise MediaDecodeError("no chunk offset table")
    chunk_offset = int.from_bytes(data[stco[1] + 8:stco[1] + 12], "big")
    stsc = _find_box(stbl_boxes, b"stsc")
    entries_stsc = int.from_bytes(data[stsc[1] + 4:stsc[1] + 8], "big")
    if entries_stsc < 1 or int.from_bytes(data[stsc[1] + 8:stsc[1] + 12], "big") != 1 \
            or int.from_bytes(data[stsc[1] + 16:stsc[1] + 20], "big") != 1:
        raise MediaDecodeError("the sample is not the first of its chunk")
    if chunk_offset < _mdat[1] or chunk_offset + sample_size > _mdat[2]:
        raise MediaDecodeError("the declared sample lies outside the mdat box")

    sample = data[chunk_offset:chunk_offset + sample_size]
    if len(sample) < length_size:
        raise MediaDecodeError("the sample is shorter than its NAL length prefix")
    nal_length = int.from_bytes(sample[:length_size], "big")
    if length_size + nal_length > len(sample):
        raise MediaDecodeError("the declared NAL exceeds the sample")
    nal = sample[length_size:length_size + nal_length]
    rbsp = _unescape_nal(nal)
    if not rbsp or rbsp[0] & 0x80:
        raise MediaDecodeError("NAL forbidden bit is set")
    if rbsp[0] & 0x1F != 5:
        raise MediaDecodeError("the sample is not an IDR slice")
    slice_reader = _BitReader(rbsp[1:])
    if slice_reader.ue() != 0:
        raise MediaDecodeError("the frame does not start at the first macroblock")
    if slice_reader.ue() not in (2, 4, 7, 9):
        raise MediaDecodeError("the slice is not an I slice")
    if slice_reader.ue() != 0:
        raise MediaDecodeError("unexpected picture parameter set id")
    slice_reader.u(4)                  # frame_num
    slice_reader.ue()                  # idr_pic_id
    slice_reader.u(4)                  # pic_order_cnt_lsb
    slice_reader.u(1)                  # no_output_of_prior_pics_flag
    slice_reader.u(1)                  # long_term_reference_flag
    slice_reader.se()                  # slice_qp_delta
    if slice_reader.ue() != 25:
        raise MediaDecodeError("the macroblock is not I_PCM")
    slice_reader.align_zero()          # pcm_alignment_zero_bits
    luma = slice_reader.read_bytes(width * height)
    cb = slice_reader.read_bytes((width // 2) * (height // 2))
    cr = slice_reader.read_bytes((width // 2) * (height // 2))
    return {"width": width, "height": height, "luma": luma, "cb": cb, "cr": cr}


# ── FFmpeg cross-check (optional, the real decoder when present) ────────────


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


requires_ffmpeg = pytest.mark.skipif(
    not _ffmpeg_available(), reason="ffmpeg is not installed on this host")


def _ffmpeg_decode(payload: bytes, tmp_path, suffix: str) -> subprocess.CompletedProcess:
    path = tmp_path / f"decode{suffix}"
    path.write_bytes(payload)
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True, timeout=60)


# ── the transport gate keeps its structural contract ────────────────────────


def test_valid_png_passes_structural_verification():
    from worker.role_executor import verify_media
    assert verify_media(PNG_VALID, ".png") == "image/png"


def test_truncated_png_is_refused():
    from worker.role_executor import verify_media
    with pytest.raises(Exception, match="media signature"):
        verify_media(PNG_TRUNCATED, ".png")


def test_signature_only_png_is_refused():
    from worker.role_executor import verify_media
    with pytest.raises(Exception, match="media signature"):
        verify_media(PNG_SIGNATURE_ONLY, ".png")


def test_decodeable_mp4_passes_structural_verification():
    from worker.role_executor import verify_media
    assert verify_media(MP4_DECODEABLE, ".mp4") == "video/mp4"


def test_junk_mdat_mp4_still_passes_only_the_structural_gate():
    """The transport gate stays structural (Issue #178.3); it must not be
    read as a decodability proof — that is what the decode layer below is for."""
    from worker.role_executor import verify_media
    assert verify_media(MP4_JUNK_MDAT, ".mp4") == "video/mp4"


def test_ftyp_only_mp4_is_refused():
    from worker.role_executor import verify_media
    with pytest.raises(Exception, match="media signature"):
        verify_media(MP4_FTYP_ONLY, ".mp4")


def test_ftyp_plus_free_mp4_is_refused():
    from worker.role_executor import verify_media
    with pytest.raises(Exception, match="media signature"):
        verify_media(MP4_FTYP_PLUS_FREE, ".mp4")


def test_truncated_ftyp_mp4_is_refused():
    from worker.role_executor import verify_media
    with pytest.raises(Exception, match="media signature"):
        verify_media(MP4_TRUNCATED_FTYP, ".mp4")


# ── the decode layer: positive proof ─────────────────────────────────────────


def test_png_fixture_decodes_to_its_exact_pixels():
    width, height, pixels = decode_png(PNG_VALID)
    assert (width, height) == (2, 2)
    assert pixels == [[PNG_PIXELS[0], PNG_PIXELS[1]], [PNG_PIXELS[2], PNG_PIXELS[3]]]


def test_decodeable_mp4_reconstructs_the_exact_frame_in_pure_python():
    """The container, sample tables, SPS and slice are decoded for real."""
    frame = decode_mp4_frame(MP4_DECODEABLE)
    assert (frame["width"], frame["height"]) == (16, 16)
    assert frame["luma"] == FRAME_LUMA
    assert frame["cb"] == FRAME_CB
    assert frame["cr"] == FRAME_CR


def test_decodeable_mp4_frame_is_a_real_idr_slice():
    """The mdat sample is a real NAL unit, not padding bytes."""
    frame = decode_mp4_frame(MP4_DECODEABLE)
    assert len(frame["luma"]) == 256 and len(set(frame["luma"])) == 256
    assert min(frame["cb"]) >= 80 and max(frame["cb"]) <= 95


@requires_ffmpeg
def test_decodeable_mp4_decodes_with_ffmpeg(tmp_path):
    """The same fixture must decode with the real FFmpeg decoder."""
    result = _ffmpeg_decode(MP4_DECODEABLE, tmp_path, ".mp4")
    assert result.returncode == 0, result.stderr


@requires_ffmpeg
def test_valid_png_decodes_with_ffmpeg(tmp_path):
    result = _ffmpeg_decode(PNG_VALID, tmp_path, ".png")
    assert result.returncode == 0, result.stderr


@requires_ffmpeg
def test_ffmpeg_decodes_the_exact_frame_bytes(tmp_path):
    """FFmpeg reconstructs the same luma plane the pure-Python decoder does."""
    path = tmp_path / "decode.mp4"
    path.write_bytes(MP4_DECODEABLE)
    raw = tmp_path / "frame.yuv"
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-pix_fmt", "yuv420p",
         "-f", "rawvideo", "-y", str(raw)],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    planes = raw.read_bytes()
    assert len(planes) == 384
    assert planes[:256] == FRAME_LUMA
    assert planes[256:320] == FRAME_CB
    assert planes[320:384] == FRAME_CR


# ── the decode layer: junk, truncation and corruption are refused ──────────


def test_decoder_refuses_the_junk_mdat_stub():
    """The 161-byte audit stub: a real box tree around 64 zero bytes."""
    with pytest.raises(MediaDecodeError):
        decode_mp4_frame(MP4_JUNK_MDAT)


def test_decoder_refuses_signature_and_box_stubs():
    for stub in (MP4_FTYP_ONLY, MP4_FTYP_PLUS_FREE, MP4_TRUNCATED_FTYP,
                 b"ftypmp42", b"\x00\x00\x00"):
        with pytest.raises(MediaDecodeError):
            decode_mp4_frame(stub)


def test_decoder_refuses_a_truncated_sample_table():
    """Cut the payload inside the moov: the sample tables cannot be read."""
    cut = MP4_DECODEABLE[:MP4_DECODEABLE.rfind(b"stco") + 10]
    with pytest.raises(MediaDecodeError):
        decode_mp4_frame(cut)


def test_decoder_refuses_a_truncated_mdat():
    """Cut the payload inside the mdat: the declared sample cannot be read."""
    with pytest.raises(MediaDecodeError):
        decode_mp4_frame(MP4_DECODEABLE[:-8])


def test_decoder_refuses_a_sample_that_escapes_the_mdat():
    """A corrupted stsz (declared sample larger than mdat) is refused."""
    position = MP4_DECODEABLE.find(b"stsz") + 12
    assert position > 0
    corrupted = bytearray(MP4_DECODEABLE)
    corrupted[position:position + 4] = (0xFF).to_bytes(4, "big")
    with pytest.raises(MediaDecodeError):
        decode_mp4_frame(bytes(corrupted))


def test_decoder_refuses_a_shifted_chunk_offset():
    """A corrupted stco (sample no longer aligned with the AVCC prefix)."""
    position = MP4_DECODEABLE.find(b"stco") + 12  # first chunk offset entry
    assert position > 0
    corrupted = bytearray(MP4_DECODEABLE)
    corrupted[position:position + 4] = (int.from_bytes(
        corrupted[position:position + 4], "big") + 3).to_bytes(4, "big")
    with pytest.raises(MediaDecodeError):
        decode_mp4_frame(bytes(corrupted))


def test_decoder_refuses_a_corrupted_sps():
    """A broken SPS geometry is refused, not silently trusted."""
    # avcC payload: [version, profile, compat, level, lenSize, numSPS](6)
    #               + sps size(2) -> the SPS NAL: header byte, then profile_idc.
    payload = MP4_DECODEABLE.find(b"avcC") + 4      # after the box size+type
    position = payload + 8 + 1                      # SPS NAL header, then profile_idc
    corrupted = bytearray(MP4_DECODEABLE)
    assert corrupted[position] == 66
    corrupted[position] = 0xFF                      # profile_idc = 255
    with pytest.raises(MediaDecodeError):
        decode_mp4_frame(bytes(corrupted))


def test_decoder_refuses_a_corrupted_slice():
    """A broken slice (mb_type is not I_PCM) cannot decode to a frame."""
    corrupted = bytearray(MP4_DECODEABLE)
    # The IDR NAL sits inside mdat behind its 4-byte length prefix.
    mdat = MP4_DECODEABLE.find(b"mdat") + 4
    corrupted[mdat + 4 + 1] ^= 0xFF                  # scramble the slice header
    with pytest.raises(MediaDecodeError):
        decode_mp4_frame(bytes(corrupted))


def test_decoder_refuses_png_stubs():
    for stub in (PNG_SIGNATURE_ONLY, PNG_TRUNCATED,
                 b"\x89PNG\r\n\x1a\n" + b"\x00" * 12):
        with pytest.raises(MediaDecodeError):
            decode_png(stub)


def test_png_decoder_refuses_a_broken_scanline_stream():
    """An IDAT that decompresses to the wrong scanline size is junk."""
    junk = (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + _png_chunk(b"IDAT", zlib.compress(b"\x00" + bytes((30, 40, 50))))
            + _png_chunk(b"IEND", b""))
    with pytest.raises(MediaDecodeError):
        decode_png(junk)


# ── determinism of the pinned fixtures ──────────────────────────────────────


def test_mp4_sha256_is_deterministic():
    digest = hashlib.sha256(MP4_DECODEABLE).hexdigest()
    assert len(digest) == 64
    assert hashlib.sha256(MP4_DECODEABLE).hexdigest() == digest


def test_png_sha256_is_deterministic():
    digest = hashlib.sha256(PNG_VALID).hexdigest()
    assert len(digest) == 64
    assert hashlib.sha256(PNG_VALID).hexdigest() == digest
