#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2（第一代）命令行打印示例。

用法：
    python print_hello_p2.py COM11
    python print_hello_p2.py COM11 --text "你好"

协议：02 … 03，详见 docs/protocol-p2.md
"""

from __future__ import annotations

import argparse
import struct
import time
import zlib

import serial
from PIL import Image, ImageDraw, ImageFont

CRC_SEED = 0x35769521
WIDTH = 576
WIDTH_BYTES = WIDTH // 8


def frame(cmd: int, payload: bytes = b"", remain: int = 0) -> bytes:
    crc = zlib.crc32(payload, CRC_SEED) & 0xFFFFFFFF
    return (bytes([0x02, cmd, remain])
            + struct.pack("<H", len(payload))
            + payload
            + struct.pack("<I", crc)
            + b"\x03")


def render(text: str, font_path: str = "C:/Windows/Fonts/arialbd.ttf", size: int = 56) -> bytes:
    height = 96
    img = Image.new("1", (WIDTH, height), 1)
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(font_path, size)
    bbox = draw.textbbox((0, 0), text, font=font)
    x = (WIDTH - (bbox[2] - bbox[0])) // 2 - bbox[0]
    y = (height - (bbox[3] - bbox[1])) // 2 - bbox[1]
    draw.text((x, y), text, font=font, fill=0)

    px = img.load()
    out = bytearray()
    for row in range(height):
        for xb in range(0, WIDTH, 8):
            value = 0
            for i in range(8):
                if px[xb + i, row] == 0:       # 0 = 黑
                    value |= 1 << (7 - i)      # MSB 在前
            out.append(value)
    return bytes(out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("port", help="串口，例如 COM11")
    parser.add_argument("--text", default="Hello World")
    args = parser.parse_args()

    data = render(args.text)
    ser = serial.Serial(args.port, 115200, timeout=1)
    time.sleep(0.3)
    ser.reset_input_buffer()

    ser.write(frame(0x2C, b"\x00"))            # 纸张类型：普通
    ser.flush()
    time.sleep(0.3)
    ser.write(frame(0x19, struct.pack("<H", 75)))   # 浓度 75
    ser.flush()
    time.sleep(0.3)
    ser.write(frame(0x1A, struct.pack("<H", 30)))   # 先走 30 行
    ser.flush()
    time.sleep(0.5)

    chunk = 14 * WIDTH_BYTES                   # 14 行/包
    total = (len(data) + chunk - 1) // chunk
    for idx in range(total):
        part = data[idx * chunk:(idx + 1) * chunk]
        ser.write(frame(0x00, part, total - idx - 1))
        ser.flush()
        time.sleep(0.02)

    time.sleep(0.3)
    ser.write(frame(0x1A, struct.pack("<H", 120)))  # 打完走纸
    ser.flush()
    time.sleep(2)
    ser.close()
    print("done")


if __name__ == "__main__":
    main()
