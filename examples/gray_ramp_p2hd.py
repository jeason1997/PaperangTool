#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 HD 灰阶测试条（12 级）。

打印一条 576×96 的渐变带，从上到下为 12 个竖条，等级 0–11。

用法：
    python gray_ramp_p2hd.py COM19

协议细节见 docs/protocol-p2hd.md 的「灰阶模式」。
"""

from __future__ import annotations

import argparse
import struct
import time
import zlib

import serial

CRC_SEED = 0x35769521
WIDTH = 576
HEIGHT = 96
LEVELS = 12
WIDTH_BYTES = WIDTH // 2          # 4bpp：每字节两个像素


def a5_frame(content: bytes) -> bytes:
    crc = zlib.crc32(content, CRC_SEED) & 0xFFFFFFFF
    return (b"\xa5\x01" + struct.pack("<H", len(content))
            + content + struct.pack("<I", crc) + b"\x5a")


def a5_content(parent: int, child: int, data: bytes = b"", msg_type: int = 1) -> bytes:
    return bytes([parent, child, msg_type]) + struct.pack("<H", len(data)) + data


def build_ramp() -> bytes:
    bar = WIDTH // LEVELS
    data = bytearray()
    for _row in range(HEIGHT):
        for x in range(0, WIDTH, 2):
            left = min(LEVELS - 1, x // bar)
            right = min(LEVELS - 1, (x + 1) // bar)
            data.append(((left & 0xF) << 4) | (right & 0xF))
    return bytes(data)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("port", help="串口，例如 COM19")
    args = parser.parse_args()

    data = build_ramp()
    ser = serial.Serial(args.port, 115200, timeout=1)
    time.sleep(0.3)
    ser.reset_input_buffer()

    ser.write(a5_frame(a5_content(5, 25)))       # 打印开始
    ser.flush()
    time.sleep(0.2)

    chunk = 4 * WIDTH_BYTES
    total = (len(data) + chunk - 1) // chunk
    for idx in range(total):
        part = data[idx * chunk:(idx + 1) * chunk]
        body = (bytes([4])                       # 精度 4 = 4bpp 灰阶
                + struct.pack("<H", WIDTH_BYTES)
                + struct.pack("<H", 0)
                + bytes([0])
                + struct.pack("<H", len(part))
                + part)
        content = struct.pack("<H", idx + 1) + struct.pack("<H", len(body)) + body
        ser.write(a5_frame(a5_content(5, 27, content)))
        ser.flush()
        time.sleep(0.12)

    time.sleep(0.2)
    ser.write(a5_frame(a5_content(5, 26)))       # 打印结束
    ser.flush()
    time.sleep(0.3)
    ser.write(a5_frame(a5_content(5, 22, struct.pack("<H", 30))))  # 走纸
    ser.flush()
    time.sleep(2)
    ser.close()
    print("done")


if __name__ == "__main__":
    main()
