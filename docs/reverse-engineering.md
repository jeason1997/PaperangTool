# 逆向过程记录

这份文档记录从零到"用自己的程序打印出 Hello World"的完整过程，包括走过的弯路。
希望对想折腾其它型号的人有帮助。

## 0. 目标与约束

- 目标：不依赖官方 App，直接控制手里的喵喵机 P2 和 P2 HD
- 手头只有 Windows 电脑 + 蓝牙适配器 + 安卓手机（可 adb）
- 不想 root 手机，不想拆机
- 官方 App 有登录和联网逻辑，但打印本身走本地蓝牙

## 1. 先确定传输方式

最初的假设是 BLE（低功耗蓝牙），因为手机上的打印机类设备大多是 BLE。
实际验证发现两条线索互相矛盾：

- P2 HD 在 BLE 上能连上、能枚举 GATT，服务是 `0000ff00-c243-40b9-ac41-a10ba3ad5d1a`
- 但手机 `dumpsys bluetooth_manager` 显示配对用的是 **BR/EDR**：

```
Bond state changed : ... transport:BT_TRANSPORT_BR_EDR is_encrypted:T
```

结论：**官方 App 走经典蓝牙 SPP**。BLE 虽然也能连，但只用于查询。

### BLE 为什么走不通

P2 HD 的 BLE MTU 只有 23，而一个 A5 打印数据帧约 170 字节。

- 一次写 170 字节 → `E_INVALIDARG`（超过 MTU）
- 拆成 20 字节分片 → 模块收到半帧直接断链

所以 BLE 这条路只能发短命令，打不了图。

## 2. 拿到 APK

手机连电脑，开启 USB 调试：

```bash
adb devices
adb shell pm list packages -3          # 找到 cn.paperang.mm
adb shell pm path cn.paperang.mm       # 拿到 base.apk 路径
adb pull /data/app/.../base.apk
```

APK 有 166 MB，包含 10 个 dex 和 158 个 so。

## 3. 先在 dex 里找线索

不解包、不反编译，先按字符串扫一遍：

```python
import zipfile, re
z = zipfile.ZipFile("base.apk")
for name in z.namelist():
    if name.endswith(".dex"):
        data = z.read(name)
        for pat in (b"00001101", b"0000ff00", b"FF02"):
            if pat in data:
                print(name, pat)
```

结果：`classes7.dex` 同时包含：

- `00001101-...`（SPP UUID）
- `0000FF00-C243-40B9-AC41-A10BA3AD5D1A`（BLE 服务）
- `FF01` / `FF02` / `FF03`
- `addBytesToList_Protocol07`、`Protocol_A5` 等类名

基本可以确定协议实现就在这个 dex 里。

## 4. 反编译

用 [jadx](https://github.com/skylot/jadx)。

第一次失败：

```
UnsupportedClassVersionError: class file version 55.0
this Java Runtime only recognizes up to 52.0
```

本机 Java 是 8，jadx 1.5.6 需要 11+。解决方式是下载**自带 JRE 的发行包**
（`jadx-gui-1.5.6-with-jre-win.zip`），用里面的 `jre/bin/java.exe` 跑 CLI：

```bash
jre/bin/java.exe -cp lib/jadx-gui-1.5.6-all.jar jadx.cli.JadxCLI \
    --no-res -d out classes7.dex
```

3772 个类，几分钟完成。

## 5. 读代码

关键类：

| 类 | 作用 |
|---|---|
| `CommUtil` | 三套协议的帧构造（02 / 07 / A5） |
| `Protocol_A5` | A5 命令表、打印开始/结束/数据 |
| `CRC32Util` | CRC 参数 |
| `GrayPrinterDataUtil` | 灰阶打包入口 |
| `PrintModeGrayRank4/8/12/16` | 灰阶模式的每点字节数与精度 |

### 三套协议

`CommUtil.getProtocolByDataHead()` 按首字节区分：

- `0x02` → 老协议（第一代 P2）
- `0x07` → Protocol07（新，头部带行宽和校验和）
- `0xA5` → A5（P2 HD）

### A5 帧

```java
packData(content):
    A5 01 | len(content) | content | CRC32 | 5A
```

CRC 的初始值在 `CRC32Util` 里：

```java
public static final long standardKey = 896963873;   // = 0x35769521
```

和第一代 P2 用的是同一个种子。

### 命令表

`Protocol_A5` 里的常量直接给出了功能：

```java
data_thermal_child_set_move_paper   = 22;   // 走纸
data_thermal_child_self_test_page   = 23;   // 自检
data_thermal_child_ctrl_print_start = 25;
data_thermal_child_ctrl_print_end   = 26;
data_thermal_child_ctrl_print_data  = 27;
```

打印数据格式在 `CommUtil.packData_A5()`：

```
[seq u16][contentLen u16][precision][width u16][offset u16][0][chunkLen u16][chunk]
```

### 灰阶

`PaperangPrintModeFactory` 把 `grayRank` 映射到四种模式：

| 模式 | 每点字节数 | 精度 |
|---|---:|---:|
| Normal | 8 | 1 |
| Rank16 | 2 | 4 |
| Rank12 | 2 | 4 |
| Rank8 | 2 | 4 |
| Rank4 | 4 | 2 |

设备信息里的 `TPColorSupport = [0,0,0,0,1,12,0]` 中的 `12` 与之对应。

## 6. 实机验证

### 配对

P2 HD 的经典蓝牙需要 Windows 配对（设置 → 添加设备）。
配对完成后系统创建 `COMx`，用 pyserial 打开。

> Windows 的程序化配对 API（`BluetoothAuthenticateDeviceEx`、
> WinRT `PairAsync`）在非交互场景下都会失败（返回 1244 或 `can_pair=False`），
> 这一步必须由用户在系统向导里确认。

### 先发最短的命令

```
5/22 走纸 50 行
```

响应：

```
A5 01 08 00 | 05 16 02 03 00 | 01 00 00 | CRC | 5A
                                └ 响应码 1 = 成功
```

### 打印

```
5/25 打印开始 → 1
5/27 数据 × N
5/26 打印结束 → 1
```

用 576×96 的 "Hello World"（1bpp、MSB 在前、1=黑、72 字节/行）一次成功。

### 灰阶

`precision = 4`、`width = 288`、每字节两个 4bit 像素，打出了 12 级渐变。

## 7. 走过的弯路

### 7.1 把命令号搞反了

早期以为 25 是走纸、27 是"打印数据但格式未知"，于是：

- 把 25 当走纸发 → 返回码 3（无效）
- 把 27 当数据发，格式用了第一代 P2 的 A5 变体 → 没反应

实际上走纸是 22，25 是打印开始。读代码后一次就对上了。

### 7.2 以为打印前有服务器鉴权

代码里确实有 `shakeHand` → App 把随机数/SN 发给服务器换 `code/sign` 再下发。
一度以为绕不过去。后来发现：

- 未加密的 BLE 链路上，打印开始返回 3（无效）
- 经典蓝牙配对后的加密链路上，同样命令返回 1（成功）

所以那道门是**链路加密**，不是服务器鉴权。

### 7.3 把"出纸距离"当成"步进单位"

P2 HD 打完要再走约 10 行才能看到图，我一度推断"1 行比 0.0847mm 大"。
这是错的：10 行是**打印头到出纸口的机械距离**，与每行的步进长度无关。

要标定步进，正确做法是：走 100 行 → 量实际毫米数 → 除以 100。

### 7.4 以为浓度可以 0–100 连续调

协议字段确实是 1 字节，但官方 App 只映射三档：

```java
if (i == 0)      density = 55;
else if (i == 1) density = 75;
else             density = 95;
```

实测其它值没有可见变化，所以工具里只保留三档。

## 8. 复现步骤（简版）

```bash
# 1. 抓 APK
adb shell pm path cn.paperang.mm
adb pull <path>/base.apk

# 2. 反编译（用带 JRE 的 jadx）
jre/bin/java.exe -cp lib/jadx-gui-1.5.6-all.jar jadx.cli.JadxCLI \
    --no-res -d out classes7.dex

# 3. 搜关键类
grep -r "Protocol_A5\|addBytesToList_Protocol" out/sources

# 4. Windows 配对经典蓝牙，拿到 COM 口
# 5. 按 docs/protocol-p2hd.md 发帧
```

## 9. 工具

- `adb`：取 APK、看 dumpsys
- `jadx`：反编译 dex
- Python `ctypes`：调用 Win32 蓝牙 API 做设备枚举
- `bleak`：BLE 侧验证
- `pyserial`：SPP 通信
- `Pillow`：渲染文字/图片

## 10. 法律与边界

- 只针对自己拥有的设备，目的是互操作
- 不修改、不重新分发官方 App
- 协议信息来自公开 APK 的静态分析和实机测试
- 不绕过任何账号或付费功能
