#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""モニタのEDIDを .bin ファイルに出力するツール (Linux用)。

使い方::
    python3 edid_dump.py

モニタ (EDIDを持つコネクタ) を一覧表示し、保存したいモニタの番号を
標準入力から選ぶと、そのEDIDを <コネクタ名>_edid.bin として保存します。

EDID は /sys/class/drm/card*/ の各コネクタの edid ファイルから
バイナリとして読み取ります。
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

DRM_DIR = Path("/sys/class/drm")
EDID_HEADER = bytes.fromhex("00ffffffffffff00")
BLOCK_SIZE = 128


# ---------------------------------------------------------------- 表示ヘルパ

def width(s: str) -> int:
    """文字列の表示幅 (全角=2, 半角=1) を返す。"""
    return sum(2 if unicodedata.east_asian_width(c) in "FW" else 1 for c in s)


def pad(s: str, n: int) -> str:
    """表示幅 n になるように右側を半角空白で埋める。"""
    return s + " " * max(0, n - width(s))


def kv(label: str, value) -> None:
    print(f"  {pad(label, 15)}: {value}")


# ---------------------------------------------------------------- コネクタ探索

def scan_connectors() -> list[dict]:
    """/sys/class/drm からコネクタとEDIDの一覧を返す。"""
    connectors: list[dict] = []
    if not DRM_DIR.is_dir():
        return connectors
    paths = [p for p in DRM_DIR.iterdir() if re.fullmatch(r"card\d+-.+", p.name)]
    for path in sorted(paths, key=lambda p: p.name):
        try:
            status = (path / "status").read_text().strip() or "unknown"
        except OSError:
            status = "unknown"
        try:
            edid = (path / "edid").read_bytes()
        except OSError:
            edid = b""
        connectors.append({"name": path.name, "status": status, "edid": edid})
    return connectors


# ---------------------------------------------------------------- EDID解析

def decode_manufacturer(data: bytes) -> str:
    """メーカーID (オフセット8, ビッグエンディアン) を3文字に展開する。"""
    value = (data[8] << 8) | data[9]
    letters = []
    for shift in (10, 5, 0):
        n = (value >> shift) & 0x1F
        letters.append(chr(0x40 + n) if 1 <= n <= 26 else "?")
    return "".join(letters)


def decode_string_descriptor(desc: bytes) -> str:
    """文字列ディスクリプタ (18バイト) からテキスト部を取り出す。"""
    text = desc[5:18].decode("ascii", errors="replace")
    return text.split("\x00")[0].strip()


def parse_edid(data: bytes) -> dict:
    """EDID (128バイト以上) の基本情報と検証結果を返す。"""
    info = {
        "header_ok": False,
        "manufacturer": "?",
        "product_code": 0,
        "serial": 0,
        "name": "",
        "serial_text": "",
        "week": 0,
        "year": None,
        "version": "",
        "extension_count": 0,
        "checksum_errors": [],
    }
    if len(data) < BLOCK_SIZE:
        return info

    info["header_ok"] = data[:8] == EDID_HEADER
    info["manufacturer"] = decode_manufacturer(data)
    info["product_code"] = data[10] | (data[11] << 8)
    info["serial"] = int.from_bytes(data[12:16], "little")
    info["week"] = data[16]
    info["year"] = data[17] + 1990 if data[17] else None
    info["version"] = f"{data[18]}.{data[19]}"
    info["extension_count"] = data[126]

    # ディスクリプタ4個 (オフセット 54, 72, 90, 108)
    for offset in (54, 72, 90, 108):
        desc = data[offset:offset + 18]
        if desc[:3] == b"\x00\x00\x00":
            if desc[3] == 0xFC and not info["name"]:          # 製品名
                info["name"] = decode_string_descriptor(desc)
            elif desc[3] == 0xFF and not info["serial_text"]:  # シリアル番号 (文字列)
                info["serial_text"] = decode_string_descriptor(desc)

    # 128バイトごとのチェックサム検証 (合計 % 256 == 0 が正常)
    blocks = len(data) // BLOCK_SIZE
    info["checksum_errors"] = [
        i for i in range(blocks)
        if sum(data[i * BLOCK_SIZE:(i + 1) * BLOCK_SIZE]) & 0xFF
    ]
    return info


def collect_warnings(data: bytes, info: dict) -> list[str]:
    """検証結果から警告文のリストを作る。"""
    warnings = []
    if not info["header_ok"]:
        warnings.append("EDIDヘッダが不正です (先頭は 00 FF FF FF FF FF FF 00 のはず)")
    if len(data) % BLOCK_SIZE:
        warnings.append(f"サイズ {len(data)} は128バイトの倍数ではありません")
    for i in info["checksum_errors"]:
        warnings.append(f"ブロック{i} のチェックサムが不正です")
    if not len(data) % BLOCK_SIZE:
        blocks = len(data) // BLOCK_SIZE
        if blocks != info["extension_count"] + 1:
            warnings.append(
                f"拡張ブロック数 ({info['extension_count']}) と"
                f"実際のブロック数-1 ({blocks - 1}) が一致しません")
    return warnings


# ---------------------------------------------------------------- 一覧表示と選択

def print_list(targets: list[dict]) -> None:
    """EDIDを持つコネクタの一覧を表示する。"""
    header = (pad("#", 4) + pad("コネクタ", 20)
              + pad("EDID", 7) + pad("メーカー", 10) + "製品名")
    print(header)
    for i, c in enumerate(targets, 1):
        info = parse_edid(c["edid"])
        maker = info["manufacturer"] if info["header_ok"] else "?"
        product = info["name"] or "-"
        row = (pad(str(i), 4) + pad(c["name"], 20)
               + pad(f"{len(c['edid'])}B", 7) + pad(maker, 10) + product)
        print(row)


def choose_monitor(targets: list[dict]) -> dict | None:
    """保存するモニタを番号で標準入力から選ばせる。中止なら None を返す。"""
    n = len(targets)
    prompt = f"番号を入力してください (1-{n}, qで中止): "
    while True:
        try:
            answer = input(prompt).strip()
        except EOFError:
            print("\n中止しました。")
            return None
        if answer.lower() in ("q", "quit"):
            print("中止しました。")
            return None
        if answer.isdigit() and 1 <= int(answer) <= n:
            return targets[int(answer) - 1]
        print(f"1〜{n} の番号を入力してください。")


def print_report(connector: dict, data: bytes, info: dict) -> None:
    """保存したEDIDの概要を表示する。"""
    blocks, rem = divmod(len(data), BLOCK_SIZE)
    if rem:
        size_desc = f"{len(data)} バイト (128の倍数ではない)"
    elif blocks > 1:
        size_desc = f"{len(data)} バイト (基本128 + 拡張{blocks - 1})"
    else:
        size_desc = "128 バイト (基本ブロックのみ)"

    kv("コネクタ", f"{connector['name']} ({connector['status']})")
    kv("サイズ", size_desc)
    kv("メーカー", info["manufacturer"])
    if info["name"]:
        kv("製品名", info["name"])
    kv("製品コード", f"{info['product_code']} (0x{info['product_code']:04X})")
    serial = str(info["serial"])
    if info["serial_text"]:
        serial += f" (文字列: {info['serial_text']})"
    kv("シリアル", serial)
    if info["year"]:
        kv("製造年週", f"{info['year']}年第{info['week']}週")
    kv("EDIDバージョン", info["version"])
    for w in collect_warnings(data, info):
        kv("警告", w)


# ---------------------------------------------------------------- EDID保存

def dump_edid(connector: dict) -> int:
    """コネクタのEDIDを <コネクタ名>_edid.bin に保存する。戻り値は終了コード。"""
    data = connector["edid"]
    if len(data) < BLOCK_SIZE:
        print(f"エラー: {connector['name']} のEDIDが短すぎます "
              f"({len(data)} バイト)。", file=sys.stderr)
        return 1

    out_path = Path(f"{connector['name']}_edid.bin")
    out_path.write_bytes(data)

    info = parse_edid(data)
    print(f"{connector['name']} のEDIDを保存しました → {out_path}")
    print_report(connector, data, info)
    return 0


# ---------------------------------------------------------------- メイン

def main() -> int:
    if len(sys.argv) > 1:
        print(f"エラー: このツールは引数を取りません。"
              f"使い方: python3 {Path(sys.argv[0]).name}", file=sys.stderr)
        return 1

    connectors = scan_connectors()
    if not connectors:
        print(f"エラー: {DRM_DIR} にDRMコネクタが見つかりません "
              "(このツールはLinux用です)。", file=sys.stderr)
        return 1

    # EDIDを持つ (モニタが接続されている) コネクタだけを対象にする
    targets = [c for c in connectors if c["edid"]]
    if not targets:
        print("エラー: EDIDを持つモニタが見つかりません。", file=sys.stderr)
        return 1

    print_list(targets)
    connector = choose_monitor(targets)
    if connector is None:
        return 1
    return dump_edid(connector)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n中止しました。")
        sys.exit(130)
