#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 HD 命令行打印示例（A5 协议）。

用法：
    python print_hello_p2hd.py COM19
    python print_hello_p2hd.py COM19 --text "Hello HD"

协议：A5 … 5A，详见 docs/protocol-p2hd.md
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


def a5_frame(content: bytes) -> bytes:
    crc = zlib.crc32(content, CRC_SEED) & 0xFFFFFFFF
    return (b"\xa5\x01" + struct.pack("<H", len(content))
            + content + struct.pack("<I", crc) + b"\x5a")


def a5_content(parent: int, child: int, data: bytes = b"", msg_type: int = 1) -> bytes:
    return bytes([parent, child, msg_type]) + struct.pack("<H", len(data)) + data


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
                if px[xb + i, row] == 0:
                    value |= 1 << (7 - i)
            out.append(value)
    return bytes(out)


def send(ser: serial.Serial, content: bytes) -> None:
    ser.write(a5_frame(content))
    ser.flush()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("port", help="串口，例如 COM19")
    parser.add_argument("--text", default="Hello World")
    args = parser.parse_args()

    data = render(args.text)
    ser = serial.Serial(args.port, 115200, timeout=1)
    time.sleep(0.3)
    ser.reset_input_buffer()

    send(ser, a5_content(5, 25))                # 打印开始
    time.sleep(0.2)

    chunk = 4 * WIDTH_BYTES                     # 4 行/包
    total = (len(data) + chunk - 1) // chunk
    for idx in range(total):
        part = data[idx * chunk:(idx + 1) * chunk]
        body = (bytes([1])                      # 精度 1 = 黑白
                + struct.pack("<H", WIDTH_BYTES)
                + struct.pack("<H", 0)          # 偏移
                + bytes([0])
                + struct.pack("<H", len(part))
                + part)
        content = struct.pack("<H", idx + 1) + struct.pack("<H", len(body)) + body
        send(ser, a5_content(5, 27, content))
        time.sleep(0.12)

    time.sleep(0.2)
    send(ser, a5_content(5, 26))                # 打印结束
    time.sleep(0.3)
    send(ser, a5_content(5, 22, struct.pack("<H", 30)))  # 走纸，把图送出来
    time.sleep(2)
    ser.close()
    print("done")


if __name__ == "__main__":
    main()
