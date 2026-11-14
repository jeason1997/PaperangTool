# -*- coding: utf-8 -*-
"""
喵喵机控制台（P2 / P2 HD 二合一）
=================================

通过经典蓝牙 SPP 控制喵喵机，不依赖官方 App。

两种机型使用不同协议，界面顶部选择机型后自动切换：

P2（第一代）::

    02 | cmd | remain | len(2 LE) | data | CRC32(4 LE) | 03
    CRC32 种子 0x35769521，只算 data 段

P2 HD::

    A5 01 | len(2 LE) | content | CRC32(4 LE) | 5A
    content = [parent, child, type, len(2 LE), data]
    CRC32 种子 0x35769521

注意：必须在 Windows 设置里配对**经典蓝牙**那个设备（同名会出现两个，
另一个是 BLE）。BLE 链路上打印会被固件拒绝。

运行：双击本文件，或 `pythonw paperang_tool.pyw`。
依赖：pyserial。
"""

from __future__ import annotations

import struct
import threading
import time
import zlib
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tkinter.scrolledtext import ScrolledText

from PIL import Image

import serial
import serial.tools.list_ports

CRC_SEED = 0x35769521
MODEL_P2 = "P2"
MODEL_HD = "P2 HD"
PRINT_WIDTH = 576
WARN_IMAGE_ROWS = 3000

# P2 命令
P2_PRINT_BITMAP = 0x00
P2_GET_MODEL = 0x06
P2_GET_STATUS = 0x0C
P2_GET_BATTERY = 0x10
P2_SET_HEAT = 0x19
P2_FEED = 0x1A
P2_PRINT_TEST = 0x1B
P2_SET_PAPER = 0x2C

# P2 HD（A5，parent=5）
HD_MOVE_PAPER = 22
HD_SELF_TEST = 23
HD_PRINT_START = 25
HD_PRINT_END = 26
HD_PRINT_DATA = 27
HD_SET_DENSITY = 17
HD_GET_INFO = 1
HD_GET_DPI = 4
HD_GET_HOTSPOT = 5
HD_GET_TEMP = 10
HD_GET_DENSITY = 16
HD_GET_SPEED = 20


# ───────────────────────────── 帧构造 ─────────────────────────────

def p2_frame(cmd: int, payload: bytes = b"", remain: int = 0) -> bytes:
    crc = zlib.crc32(payload, CRC_SEED) & 0xFFFFFFFF
    return (bytes([0x02, cmd & 0xFF, remain & 0xFF])
            + struct.pack("<H", len(payload))
            + payload
            + struct.pack("<I", crc)
            + b"\x03")


def a5_content(parent: int, child: int, data: bytes = b"", msg_type: int = 1) -> bytes:
    return bytes([parent & 0xFF, child & 0xFF, msg_type & 0xFF]) + struct.pack("<H", len(data)) + data


def a5_frame(content: bytes) -> bytes:
    crc = zlib.crc32(content, CRC_SEED) & 0xFFFFFFFF
    return b"\xa5\x01" + struct.pack("<H", len(content)) + content + struct.pack("<I", crc) + b"\x5a"


def parse_p2(raw: bytes):
    frames = []
    i = 0
    while i + 10 <= len(raw):
        if raw[i] != 0x02:
            i += 1
            continue
        cmd = raw[i + 1]
        length = struct.unpack("<H", raw[i + 3:i + 5])[0]
        end = i + 5 + length + 4 + 1
        if end > len(raw):
            break
        if raw[end - 1] != 0x03:
            i += 1
            continue
        frames.append((cmd, raw[i + 5:i + 5 + length]))
        i = end
    return frames


def parse_a5(raw: bytes):
    i = raw.find(b"\xa5\x01")
    if i < 0 or len(raw) < i + 9:
        return None
    length = struct.unpack("<H", raw[i + 2:i + 4])[0]
    end = i + 4 + length + 4 + 1
    if end > len(raw) or raw[end - 1] != 0x5A:
        return None
    content = raw[i + 4:i + 4 + length]
    if len(content) < 5:
        return None
    data_len = struct.unpack("<H", content[3:5])[0]
    return content[0], content[1], content[2], content[5:5 + data_len]


def load_image(path: str) -> Image.Image:
    """读图并适配 576 点宽：超宽裁掉右侧，偏窄右侧补白。高度不限制。"""
    img = Image.open(path).convert("L")
    width, height = img.size
    if width > PRINT_WIDTH:
        img = img.crop((0, 0, PRINT_WIDTH, height))
    elif width < PRINT_WIDTH:
        canvas = Image.new("L", (PRINT_WIDTH, height), 255)
        canvas.paste(img, (0, 0))
        img = canvas
    return img


def pack_1bpp(img: Image.Image) -> bytes:
    """Floyd–Steinberg 抖动后打包成 1bpp，MSB 在前，1 = 黑。"""
    bw = img.convert("1")
    px = bw.load()
    width, height = bw.size
    out = bytearray()
    for y in range(height):
        for xb in range(0, width, 8):
            value = 0
            for i in range(8):
                if xb + i < width and px[xb + i, y] == 0:
                    value |= 1 << (7 - i)
            out.append(value)
    return bytes(out)


def pack_2bpp(img: Image.Image, levels: int = 4) -> bytes:
    """灰度打包成 2bpp：每字节四个像素，高位在左。"""
    px = img.load()
    width, height = img.size
    max_level = max(1, levels - 1)
    out = bytearray()
    for y in range(height):
        for x in range(0, width, 4):
            value = 0
            for i in range(4):
                if x + i < width:
                    level = round((255 - px[x + i, y]) * max_level / 255)
                else:
                    level = 0
                value |= (level & 0x3) << (6 - 2 * i)
            out.append(value)
    return bytes(out)


def pack_4bpp(img: Image.Image, levels: int = 12) -> bytes:
    """灰度打包成 4bpp：每字节两个像素，高 nibble 为左侧像素。"""
    px = img.load()
    width, height = img.size
    max_level = max(1, levels - 1)
    out = bytearray()
    for y in range(height):
        for x in range(0, width, 2):
            left = px[x, y]
            right = px[x + 1, y] if x + 1 < width else 255
            lv_left = round((255 - left) * max_level / 255)
            lv_right = round((255 - right) * max_level / 255)
            out.append(((lv_left & 0xF) << 4) | (lv_right & 0xF))
    return bytes(out)


# ───────────────────────────── 设备 ─────────────────────────────

class Printer:
    def __init__(self) -> None:
        self.ser: serial.Serial | None = None
        self.lock = threading.Lock()
        self.model = MODEL_P2
        self.port: str | None = None

    @property
    def connected(self) -> bool:
        return self.ser is not None and self.ser.is_open

    def connect(self, port: str, model: str) -> None:
        self.close()
        self.model = model
        self.ser = serial.Serial(port, 115200, timeout=1.0, write_timeout=1.0)
        self.port = port
        time.sleep(0.2)
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass

    def close(self) -> None:
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.port = None

    # ── 底层 ────────────────────────────────────────────────

    def _raw_send(self, data: bytes) -> None:
        if not self.connected:
            raise RuntimeError("未连接")
        with self.lock:
            assert self.ser is not None
            self.ser.write(data)
            self.ser.flush()

    def _raw_query(self, data: bytes, wait: float = 0.6) -> bytes:
        if not self.connected:
            raise RuntimeError("未连接")
        with self.lock:
            assert self.ser is not None
            self.ser.reset_input_buffer()
            self.ser.write(data)
            self.ser.flush()
            time.sleep(wait)
            return self.ser.read(1024)

    # ── 动作 ────────────────────────────────────────────────

    def feed(self, lines: int) -> None:
        lines = max(0, min(65535, int(lines)))
        if self.model == MODEL_P2:
            self._raw_send(p2_frame(P2_FEED, struct.pack("<H", lines)))
        else:
            self._raw_send(a5_frame(a5_content(5, HD_MOVE_PAPER, struct.pack("<H", lines))))

    def set_density(self, value: int) -> None:
        value = max(0, min(100, int(value)))
        if self.model == MODEL_P2:
            self._raw_send(p2_frame(P2_SET_HEAT, struct.pack("<H", value)))
        else:
            self._raw_send(a5_frame(a5_content(5, HD_SET_DENSITY, bytes([value]))))

    def self_test(self) -> None:
        if self.model == MODEL_P2:
            self._raw_send(p2_frame(P2_PRINT_TEST))
        else:
            self._raw_send(a5_frame(a5_content(5, HD_SELF_TEST)))

    def print_black_rows(self, rows: int) -> None:
        rows = max(1, min(2000, int(rows)))
        self._send_bitmap_1bpp(b"\xff" * (72 * rows))

    def print_image_bw(self, img: Image.Image) -> None:
        self._send_bitmap_1bpp(pack_1bpp(img))

    def print_image_gray(self, img: Image.Image, levels: int = 12) -> None:
        if self.model != MODEL_HD:
            raise RuntimeError("灰阶仅支持 P2 HD")
        if levels <= 4:
            data = pack_2bpp(img, levels)
            width_bytes = PRINT_WIDTH // 4
            precision = 2
        else:
            data = pack_4bpp(img, levels)
            width_bytes = PRINT_WIDTH // 2
            precision = 4
        self._raw_send(a5_frame(a5_content(5, HD_PRINT_START)))
        time.sleep(0.2)
        chunk = 4 * width_bytes
        total = (len(data) + chunk - 1) // chunk
        for idx in range(total):
            part = data[idx * chunk:(idx + 1) * chunk]
            body = (bytes([precision])
                    + struct.pack("<H", width_bytes)
                    + struct.pack("<H", 0)
                    + bytes([0])
                    + struct.pack("<H", len(part))
                    + part)
            content = struct.pack("<H", idx + 1) + struct.pack("<H", len(body)) + body
            self._raw_send(a5_frame(a5_content(5, HD_PRINT_DATA, content)))
            time.sleep(0.12)
        time.sleep(0.2)
        self._raw_send(a5_frame(a5_content(5, HD_PRINT_END)))

    def _send_bitmap_1bpp(self, data: bytes) -> None:
        if len(data) % 72 != 0:
            raise ValueError("1bpp 数据长度必须是 72 的倍数")
        if self.model == MODEL_P2:
            chunk = 14 * 72                     # 14 × 72 = 1008 ≤ 1023
            packets = [data[i:i + chunk] for i in range(0, len(data), chunk)]
            for start in range(0, len(packets), 255):
                batch = packets[start:start + 255]
                for idx, part in enumerate(batch):
                    self._raw_send(p2_frame(P2_PRINT_BITMAP, part, len(batch) - idx - 1))
                    time.sleep(0.02)
        else:
            self._raw_send(a5_frame(a5_content(5, HD_PRINT_START)))
            time.sleep(0.2)
            chunk = 4 * 72                      # 4 行/包
            total = (len(data) + chunk - 1) // chunk
            for idx in range(total):
                part = data[idx * chunk:(idx + 1) * chunk]
                body = (bytes([1])
                        + struct.pack("<H", 72)
                        + struct.pack("<H", 0)
                        + bytes([0])
                        + struct.pack("<H", len(part))
                        + part)
                content = struct.pack("<H", (idx % 65535) + 1) + struct.pack("<H", len(body)) + body
                self._raw_send(a5_frame(a5_content(5, HD_PRINT_DATA, content)))
                time.sleep(0.12)
            time.sleep(0.2)
            self._raw_send(a5_frame(a5_content(5, HD_PRINT_END)))

    # ── 查询 ────────────────────────────────────────────────

    def query_info(self) -> str:
        if self.model == MODEL_P2:
            parts = []
            raw = self._raw_query(p2_frame(P2_GET_MODEL, b"\x01"))
            for cmd, data in parse_p2(raw):
                if cmd == P2_GET_MODEL + 1:
                    parts.append("型号=" + data.rstrip(b"\x00").decode("utf-8", "replace"))
                    break
            raw = self._raw_query(p2_frame(P2_GET_BATTERY, b"\x01"))
            for cmd, data in parse_p2(raw):
                if cmd == P2_GET_BATTERY + 1 and data:
                    parts.append(f"电量={data[0]}%")
                    break
            raw = self._raw_query(p2_frame(P2_GET_STATUS, b"\x01"))
            for cmd, data in parse_p2(raw):
                if cmd == P2_GET_STATUS + 1:
                    parts.append("状态=" + data.hex())
                    break
            return " | ".join(parts) if parts else "无响应"

        parts = []
        for child, name in ((HD_GET_DPI, "DPI"), (HD_GET_HOTSPOT, "加热点数"),
                            (HD_GET_DENSITY, "浓度"), (HD_GET_TEMP, "温度"), (HD_GET_SPEED, "速度")):
            parsed = parse_a5(self._raw_query(a5_frame(a5_content(5, child))))
            if not parsed:
                parts.append(f"{name}=?")
                continue
            data = parsed[3]
            if len(data) >= 3:
                length = struct.unpack("<H", data[1:3])[0]
                value = data[3:3 + length]
            else:
                value = b""
            if len(value) == 2:
                num = struct.unpack("<H", value)[0]
            elif len(value) == 1:
                num = value[0]
            else:
                num = int.from_bytes(value, "little") if value else "?"
            parts.append(f"{name}={num}")
        parsed = parse_a5(self._raw_query(a5_frame(a5_content(5, HD_GET_INFO)), wait=1.0))
        if parsed and len(parsed[3]) >= 3:
            length = struct.unpack("<H", parsed[3][1:3])[0]
            text = parsed[3][3:3 + length].decode("utf-8", "ignore").strip("\x00")
            if text:
                parts.append("INFO=" + text)
        return " | ".join(parts) if parts else "无响应"


# ───────────────────────────── GUI ─────────────────────────────

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("喵喵机控制台")
        self.geometry("760x640")
        self.minsize(720, 580)

        self.printer = Printer()
        self.total_lines = 0

        self._build_ui()
        self.refresh_ports()
        self._on_model_change()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.log("就绪。")

    # ── 界面 ────────────────────────────────────────────────

    def _build_ui(self) -> None:
        pad = {"padx": 6, "pady": 4}

        model = ttk.LabelFrame(self, text="机型")
        model.pack(fill="x", padx=10, pady=(10, 4))
        self.model_var = tk.StringVar(value=MODEL_P2)
        ttk.Radiobutton(model, text="P2", value=MODEL_P2,
                        variable=self.model_var, command=self._on_model_change).pack(side="left", padx=10, pady=4)
        ttk.Radiobutton(model, text="P2 HD", value=MODEL_HD,
                        variable=self.model_var, command=self._on_model_change).pack(side="left", padx=10, pady=4)

        conn = ttk.LabelFrame(self, text="连接")
        conn.pack(fill="x", padx=10, pady=4)
        ttk.Label(conn, text="端口").grid(row=0, column=0, sticky="w", **pad)
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(conn, textvariable=self.port_var, width=46, state="readonly")
        self.port_box.grid(row=0, column=1, sticky="we", **pad)
        ttk.Button(conn, text="刷新", command=self.refresh_ports, width=8).grid(row=0, column=2, **pad)
        self.btn_conn = ttk.Button(conn, text="连接", command=self.toggle_connect, width=8)
        self.btn_conn.grid(row=0, column=3, **pad)
        conn.columnconfigure(1, weight=1)
        self.conn_state = ttk.Label(conn, text="未连接", foreground="#b00")
        self.conn_state.grid(row=1, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 4))

        feed = ttk.LabelFrame(self, text="走纸")
        feed.pack(fill="x", padx=10, pady=4)
        ttk.Label(feed, text="行数").grid(row=0, column=0, sticky="w", **pad)
        self.n_var = tk.StringVar(value="5")
        ttk.Spinbox(feed, from_=1, to=2000, width=8, textvariable=self.n_var).grid(row=0, column=1, sticky="w", **pad)

        row1 = ttk.Frame(feed)
        row1.grid(row=1, column=0, columnspan=5, sticky="w", pady=(2, 4))
        for n in (1, 3, 5, 10, 20, 50):
            ttk.Button(row1, text=f"走 {n} 行", width=9,
                       command=lambda v=n: self.do_feed(v)).pack(side="left", padx=4)
        ttk.Button(row1, text="按 N 走", width=10, command=self.do_feed_n).pack(side="left", padx=10)

        self.total_label = ttk.Label(feed, text="本次累计走纸：0 行")
        self.total_label.grid(row=2, column=0, columnspan=5, sticky="w", padx=6, pady=(0, 4))

        test = ttk.LabelFrame(self, text="打印测试")
        test.pack(fill="x", padx=10, pady=4)
        ttk.Button(test, text="打 10 行黑带", width=13,
                   command=lambda: self.do_black_band(10)).grid(row=0, column=0, padx=6, pady=4)
        ttk.Button(test, text="打 20 行黑带", width=13,
                   command=lambda: self.do_black_band(20)).grid(row=0, column=1, padx=6, pady=4)
        ttk.Button(test, text="自检页", width=10, command=self.do_self_test).grid(row=0, column=2, padx=6, pady=4)
        self.autofeed_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(test, text="打完走", variable=self.autofeed_var).grid(row=0, column=3, padx=(16, 2))
        self.autofeed_n_var = tk.StringVar(value="10")
        ttk.Spinbox(test, from_=0, to=500, width=5,
                    textvariable=self.autofeed_n_var).grid(row=0, column=4, padx=(0, 2))
        ttk.Label(test, text="行").grid(row=0, column=5, padx=(0, 8))

        img_frame = ttk.LabelFrame(self, text="打印图片")
        img_frame.pack(fill="x", padx=10, pady=4)
        self.image_path_var = tk.StringVar()
        ttk.Button(img_frame, text="选择图片", width=10,
                   command=self.choose_image).grid(row=0, column=0, padx=6, pady=4)
        ttk.Label(img_frame, textvariable=self.image_path_var, width=44,
                  anchor="w").grid(row=0, column=1, sticky="w", padx=4)
        self.image_mode_var = tk.StringVar(value="黑白抖动")
        self.image_mode_box = ttk.Combobox(
            img_frame, textvariable=self.image_mode_var, width=12, state="readonly",
            values=["黑白抖动", "灰阶 4 级", "灰阶 8 级", "灰阶 12 级", "灰阶 16 级"],
        )
        self.image_mode_box.grid(row=0, column=2, padx=6)
        ttk.Button(img_frame, text="打印", width=8,
                   command=self.do_print_image).grid(row=0, column=3, padx=6)

        dens = ttk.LabelFrame(self, text="打印浓度")
        dens.pack(fill="x", padx=10, pady=4)
        self.density_var = tk.IntVar(value=95)
        for value, name in ((55, "淡"), (75, "中"), (95, "浓")):
            ttk.Radiobutton(dens, text=f"{name}（{value}）", value=value,
                            variable=self.density_var,
                            command=self.apply_density).pack(side="left", padx=12, pady=4)
        self.density_label = ttk.Label(dens, text="当前：浓（95）", foreground="#555")
        self.density_label.pack(side="left", padx=14)

        info = ttk.LabelFrame(self, text="设备信息")
        info.pack(fill="x", padx=10, pady=4)
        self.info_label = ttk.Label(info, text="—", justify="left", wraplength=640)
        self.info_label.grid(row=0, column=0, sticky="w", **pad)
        ttk.Button(info, text="查询", command=self.refresh_info).grid(row=0, column=1, padx=6)

        logf = ttk.LabelFrame(self, text="日志")
        logf.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.log_text = ScrolledText(logf, height=9, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)

    def _on_model_change(self) -> None:
        model = self.model_var.get()
        if self.printer.connected and self.printer.model != model:
            self.printer.close()
            self.conn_state.config(text="未连接", foreground="#b00")
            self.btn_conn.config(text="连接")
            self.log("机型已切换，连接已断开。")
        if model == MODEL_P2:
            self.image_mode_var.set("黑白抖动")
            self.image_mode_box.config(state="disabled")
        else:
            self.image_mode_box.config(state="readonly")
        self.log(f"当前机型：{model}")

    # ── 端口/连接 ───────────────────────────────────────────

    def refresh_ports(self) -> None:
        ports = list(serial.tools.list_ports.comports())

        def key(p):
            hwid = (p.hwid or "").upper()
            return (0 if "BTHENUM" in hwid else 1, p.device)

        ports.sort(key=key)
        items = [f"{p.device} — {p.description}" for p in ports]
        self.port_box["values"] = items
        if items:
            self.port_box.current(0)
            self.log(f"发现 {len(items)} 个串口，已选 {items[0]}")
        else:
            self.port_box.set("")
            self.log("没有发现串口。")

    def _selected_port(self):
        text = self.port_var.get().strip()
        if not text:
            return None
        return text.split("—")[0].strip()

    def toggle_connect(self) -> None:
        if self.printer.connected:
            self.printer.close()
            self.conn_state.config(text="未连接", foreground="#b00")
            self.btn_conn.config(text="连接")
            self.log("已断开。")
            return
        port = self._selected_port()
        if not port:
            messagebox.showwarning("没有端口", "请先选择一个串口。")
            return
        model = self.model_var.get()
        try:
            self.printer.connect(port, model)
        except Exception as exc:
            self.log(f"连接 {port} 失败：{exc}")
            messagebox.showerror("连接失败", f"{port}\n\n{exc}")
            return
        self.conn_state.config(text=f"已连接 {port}（{model}）", foreground="#070")
        self.btn_conn.config(text="断开")
        self.log(f"已连接 {port}，机型 {model}。")

    # ── 动作 ────────────────────────────────────────────────

    def do_feed(self, lines: int) -> None:
        try:
            self.printer.feed(lines)
        except Exception as exc:
            self.log(f"走纸失败：{exc}")
            return
        self.total_lines += lines
        self.total_label.config(text=f"本次累计走纸：{self.total_lines} 行")
        self.log(f"走纸 {lines} 行")

    def do_feed_n(self) -> None:
        try:
            n = int(self.n_var.get())
        except ValueError:
            messagebox.showwarning("数值无效", "行数必须是整数。")
            return
        if n <= 0:
            messagebox.showwarning("数值无效", "行数必须大于 0。")
            return
        self.do_feed(n)

    def do_self_test(self) -> None:
        try:
            self.printer.self_test()
        except Exception as exc:
            self.log(f"自检失败：{exc}")
            return
        self.log("已发送自检命令")

    def do_black_band(self, rows: int) -> None:
        try:
            self.printer.print_black_rows(rows)
        except Exception as exc:
            self.log(f"打印失败：{exc}")
            return
        self.log(f"已发送 {rows} 行黑带")
        if self.autofeed_var.get():
            try:
                n = int(self.autofeed_n_var.get())
            except ValueError:
                n = 10
            if n > 0:
                time.sleep(0.3)
                self.do_feed(n)

    def choose_image(self) -> None:
        path = filedialog.askopenfilename(
            title="选择图片",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"), ("所有文件", "*.*")],
        )
        if path:
            self.image_path_var.set(path)
            self.log(f"已选择图片：{path}")

    def do_print_image(self) -> None:
        path = self.image_path_var.get().strip()
        if not path:
            messagebox.showwarning("没有图片", "请先选择一张图片。")
            return
        if not self.printer.connected:
            messagebox.showwarning("未连接", "请先连接打印机。")
            return
        try:
            with Image.open(path) as probe:
                width, height = probe.size
        except Exception as exc:
            messagebox.showerror("打不开图片", str(exc))
            return
        if height > WARN_IMAGE_ROWS:
            mm = height * 25.4 / 300
            if not messagebox.askyesno(
                "图片较长",
                f"图片 {width}×{height} 点，打印后约 {mm / 10:.1f} 厘米。\n确定继续吗？",
            ):
                return
        choice = self.image_mode_var.get()
        if self.printer.model == MODEL_HD and choice.startswith("灰阶"):
            mode = "gray"
            levels = {"灰阶 4 级": 4, "灰阶 8 级": 8, "灰阶 12 级": 12, "灰阶 16 级": 16}.get(choice, 12)
            label = choice
        else:
            mode = "bw"
            levels = 0
            label = "黑白抖动"
        self.log(f"开始处理图片（{label}）")
        threading.Thread(target=self._image_worker, args=(path, mode, levels, label), daemon=True).start()

    def _image_worker(self, path: str, mode: str, levels: int, label: str) -> None:
        try:
            img = load_image(path)
            if mode == "gray":
                self.printer.print_image_gray(img, levels)
            else:
                self.printer.print_image_bw(img)
        except Exception as exc:
            message = str(exc)
            self.after(0, lambda: self.log(f"打印图片失败：{message}"))
            return
        size = img.size
        self.after(0, lambda: self._after_image(label, size))

    def _after_image(self, label: str, size) -> None:
        self.log(f"图片已发送：{label}，{size[0]}×{size[1]} 点")
        if self.autofeed_var.get():
            try:
                n = int(self.autofeed_n_var.get())
            except ValueError:
                n = 10
            if n > 0:
                self.do_feed(n)

    def apply_density(self) -> None:
        value = int(self.density_var.get())
        name = {55: "淡", 75: "中", 95: "浓"}.get(value, str(value))
        self.density_label.config(text=f"当前：{name}（{value}）")
        try:
            self.printer.set_density(value)
        except Exception as exc:
            self.log(f"设置浓度失败：{exc}")
            return
        self.log(f"浓度设为 {name}（{value}）")

    def refresh_info(self) -> None:
        if not self.printer.connected:
            return
        threading.Thread(target=self._info_worker, daemon=True).start()

    def _info_worker(self) -> None:
        try:
            text = self.printer.query_info()
        except Exception as exc:
            text = f"查询异常：{exc}"
        self.after(0, lambda: (self.info_label.config(text=text), self.log(text)))

    # ── 日志/退出 ───────────────────────────────────────────

    def log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def on_close(self) -> None:
        self.printer.close()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
