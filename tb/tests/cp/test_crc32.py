"""`hw/rtl/cp/crc32_word.sv` 的用例:CRC-32/ISO-HDLC 必须与 `zlib.crc32` 逐位一致。

`LOAD_KERNEL` 用它校验镜像头的 `code_crc32`(CMDS §6.4),所以这条对拍是"加载时就能
定位错误"的前提:如果 CRC 实现不对,所有镜像都会被判成坏的(或更糟:坏的镜像被放行)。
"""

from __future__ import annotations

import struct
import zlib

import cocotb
from cocotb.triggers import FallingEdge, RisingEdge

from harness.lib.tb import Scoreboard, reset_dut
from tests.core.dut_drivers import start_clock_once
from tests.cp import cp_env as E


async def crc_of_bytes(dut, data: bytes) -> int:
    """逐字(小端)喂进去, 返回最终 CRC。"""
    dut.init.value = 0
    dut.en.value = 0
    dut.data.value = 0
    await reset_dut(dut)
    await FallingEdge(dut.clk)
    dut.init.value = 1
    await RisingEdge(dut.clk)
    dut.init.value = 0
    for i in range(0, len(data) // 4 * 4, 4):
        await FallingEdge(dut.clk)
        dut.en.value = 1
        dut.data.value = struct.unpack_from("<I", data, i)[0]
        await RisingEdge(dut.clk)
        dut.en.value = 0
    await FallingEdge(dut.clk)
    return int(dut.crc.value)


@cocotb.test()
async def test_crc32_matches_zlib(dut):
    """已知向量 + 真镜像的指令区 + 随机字节流, 都要与 `zlib.crc32` 一致。"""
    sb = Scoreboard("crc32")
    await start_clock_once(dut)
    await reset_dut(dut)

    # 标准向量自检 + 按字喂的等价向量(模块是"逐字"接口, 所以比较的是同一段 12 字节)
    sb.check_int(zlib.crc32(b"123456789") & 0xFFFFFFFF, 0xCBF43926,
                 "测试自身的锚:CRC-32/ISO-HDLC('123456789') = 0xCBF43926")
    vec = b"1234" + b"5678" + b"9\x00\x00\x00"
    sb.check(await crc_of_bytes(dut, vec), zlib.crc32(vec) & 0xFFFFFFFF, "12 字节向量")

    raw = E.assemble_image("nested7.S")
    f = E.image_fields(raw)
    code = raw[f["entry"]:f["entry"] + f["csize"]]
    sb.check(await crc_of_bytes(dut, code), zlib.crc32(code) & 0xFFFFFFFF,
             "nested7 指令区")
    sb.check_int(f["crc"], zlib.crc32(code) & 0xFFFFFFFF, "镜像头里的 code_crc32 也是这个值")

    # 随机字节流(长度是 4 的倍数)
    import random
    rng = random.Random(7)
    for k in range(20):
        n = rng.randrange(4, 200) * 4
        data = bytes(rng.getrandbits(8) for _ in range(n))
        sb.check(await crc_of_bytes(dut, data), zlib.crc32(data) & 0xFFFFFFFF,
                 f"随机流 #{k}({n} 字节)")
    sb.report()
