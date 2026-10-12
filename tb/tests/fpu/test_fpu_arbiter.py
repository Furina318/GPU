"""Tests for ``hw/rtl/fpu/fpu_arbiter.v``.

This is the write-back arbiter shared by every FPU unit: it picks the *oldest*
pending request (so a long-latency divide can never be starved by a stream of
single-cycle adds), drives a one-hot grant, and lets a ``flush`` cancel a
request by instruction id.

The DUT is purely combinational, so no clock is involved.  All test code reads
the bus widths off the ports, which lets the same tests cover both the 4-port
default configuration and the 5-port instance used inside ``fpu``.
"""

from __future__ import annotations

import random

import cocotb

from harness.lib.tb import Scoreboard, settle

RANDOM_VECTORS = 4000


def is_older(a: int, b: int, idw: int) -> bool:
    """Transcription of the RTL's circular 'a comes before b' relation."""
    mask = (1 << idw) - 1
    diff = (b - a) & mask
    return ((diff >> (idw - 1)) == 0) and diff != 0


def arb_model(ports: dict) -> dict:
    """Golden model of ``fpu_arbiter``.

    ``ports`` carries ``valid``, ``ids``, ``data``, ``fflags``, ``flush`` and
    ``flush_id`` plus the resolved bus widths.
    """
    n = ports["num_ports"]
    idw = ports["idw"]
    dataw = ports["dataw"]
    flush = ports["flush"]
    flush_id = ports["flush_id"]

    # flush cancels exactly the port(s) carrying the flushed instruction id
    req = [
        bool(ports["valid"][i]) and not (flush and ports["ids"][i] == flush_id)
        for i in range(n)
    ]

    best = None
    for j in range(n):
        if req[j] and (best is None or is_older(ports["ids"][j], ports["ids"][best], idw)):
            best = j

    if best is None:
        return {
            "grant": 0,
            "wb_valid": 0,
            "wb_id": 0,
            "wb_data": 0,
            "wb_fflags": 0,
        }
    return {
        "grant": 1 << best,
        "wb_valid": 1,
        "wb_id": ports["ids"][best],
        "wb_data": ports["data"][best] & ((1 << dataw) - 1),
        "wb_fflags": ports["fflags"][best] & 0x1F,
    }


def widths(dut) -> dict:
    """Derive NUM_PORTS / ID_WIDTH / DATA_WIDTH from the port declarations."""
    num_ports = len(dut.req_valid_i)
    idw = len(dut.req_id_i) // num_ports
    dataw = len(dut.req_data_i) // num_ports
    return {"num_ports": num_ports, "idw": idw, "dataw": dataw}


async def drive_and_read(dut, w: dict, valid, ids, data, fflags, flush, flush_id) -> dict:
    n, idw, dataw = w["num_ports"], w["idw"], w["dataw"]
    packed_ids = sum((ids[i] & ((1 << idw) - 1)) << (idw * i) for i in range(n))
    packed_data = sum((data[i] & ((1 << dataw) - 1)) << (dataw * i) for i in range(n))
    packed_flags = sum((fflags[i] & 0x1F) << (5 * i) for i in range(n))

    dut.req_valid_i.value = sum((1 << i) for i in range(n) if valid[i])
    dut.req_id_i.value = packed_ids
    dut.req_data_i.value = packed_data
    dut.req_fflags_i.value = packed_flags
    dut.flush.value = flush
    dut.flush_id.value = flush_id
    await settle()

    return {
        "grant": int(dut.grant_o.value),
        "wb_valid": int(dut.wb_valid_o.value),
        "wb_id": int(dut.wb_id_o.value),
        "wb_data": int(dut.wb_data_o.value),
        "wb_fflags": int(dut.wb_fflags_o.value),
    }


async def check(dut, sb: Scoreboard, w: dict, ports: dict, note: str) -> None:
    got = await drive_and_read(
        dut, w, ports["valid"], ports["ids"], ports["data"],
        ports["fflags"], ports["flush"], ports["flush_id"],
    )
    exp = arb_model({**w, **ports})
    ctx = (
        f"{note} valid={ports['valid']} ids={ports['ids']} "
        f"flush={ports['flush']}/{ports['flush_id']}"
    )
    for field in exp:
        sb.check_int(got[field], exp[field], f"{ctx} .{field}")
    sb.check_equal(
        bin(got["grant"]).count("1") <= 1, True,
        f"{ctx} grant must be one-hot-or-zero",
    )


def blank(w: dict, **over):
    n = w["num_ports"]
    base = {
        "valid": [0] * n,
        "ids": [0] * n,
        "data": [0] * n,
        "fflags": [0] * n,
        "flush": 0,
        "flush_id": 0,
    }
    base.update(over)
    return base


@cocotb.test()
async def test_arbiter_directed(dut):
    """One request at a time, then priority, flush and idle behaviour."""
    w = widths(dut)
    n, idw = w["num_ports"], w["idw"]
    sb = Scoreboard("arbiter.directed")

    # -- nobody asks ------------------------------------------------------
    await check(dut, sb, w, blank(w), "idle")

    # -- each port alone gets the grant, and its own payload ---------------
    for p in range(n):
        data = [(0xA0000000 + i) & 0xFFFFFFFF for i in range(n)]
        flags = [(i + 1) & 0x1F for i in range(n)]
        ids = [7] * n
        ports = blank(w, valid=[1 if i == p else 0 for i in range(n)],
                      ids=ids, data=data, fflags=flags)
        await check(dut, sb, w, ports, f"single port {p}")
        got = await drive_and_read(dut, w, ports["valid"], ids, data, flags, 0, 0)
        sb.check_int(got["wb_data"], data[p], f"port {p} payload forwarding")

    # -- every subset of ports asking, with distinct ids ------------------
    ids = [(3 + i * 5) % (1 << idw) for i in range(n)]
    for pattern in range(1, 1 << n):
        valid = [(pattern >> i) & 1 for i in range(n)]
        ports = blank(w, valid=valid, ids=ids)
        await check(dut, sb, w, ports, f"subset {pattern:0{n}b}")

    # -- flush cancels a matching id only ---------------------------------
    ids = [5 + i for i in range(n)]
    ports = blank(w, valid=[1] * n, ids=ids, flush=1, flush_id=5)
    await check(dut, sb, w, ports, "flush id 5 (present)")
    ports = blank(w, valid=[1] * n, ids=ids, flush=1, flush_id=9)
    await check(dut, sb, w, ports, "flush id 9 (absent)")
    # flushing the only requester must produce silence
    ports = blank(w, valid=[1] + [0] * (n - 1), ids=ids, flush=1, flush_id=5)
    got = await drive_and_read(dut, w, ports["valid"], ids, ports["data"],
                               ports["fflags"], 1, 5)
    sb.check_int(got["wb_valid"], 0, "flush of the sole requester -> no writeback")
    sb.check_int(got["grant"], 0, "flush of the sole requester -> no grant")

    sb.log_summary(dut)
    sb.report()


@cocotb.test()
async def test_arbiter_age_order(dut):
    """The oldest id must win even when it sits on the highest port index."""
    w = widths(dut)
    n, idw = w["num_ports"], w["idw"]
    mask = (1 << idw) - 1
    sb = Scoreboard("arbiter.age_order")

    # Explicitly walk a request from port n-1 (oldest) up to port 0 (newest).
    ids = [0] * n
    for age in range(n):
        # port (n-1-age) holds the oldest id
        ids = [(age + k) % (1 << idw) for k in range(n)]
        ids = [id_ % (1 << idw) for id_ in reversed(range(age, age + n))]
        ports = blank(w, valid=[1] * n, ids=ids)
        await check(dut, sb, w, ports, f"age walk {age}")

    # Wrap-around: with 5-bit ids, 30 is *older* than 2 across the wrap.
    if idw == 5:
        for older, newer in ((30, 2), (31, 0), (0, 15), (16, 31)):
            ids = [older] + [newer] * (n - 1)
            ports = blank(w, valid=[1] * n, ids=ids)
            await check(dut, sb, w, ports, f"wrap {older} vs {newer}")
        # Half-range apart is *not* ordered, so the low port index wins.
        sb.check_equal(is_older(0, 16, idw), False, "0 vs 16 is a full-half tie")
        sb.check_equal(is_older(16, 0, idw), False, "16 vs 0 is a full-half tie")

    # Equal ids: the lowest port index must keep the grant (stable choice).
    ids = [9] * n
    ports = blank(w, valid=[1] * n, ids=ids, data=list(range(n)))
    await check(dut, sb, w, ports, "all ids equal")

    sb.log_summary(dut)
    sb.report()


@cocotb.test()
async def test_arbiter_random(dut):
    """Randomised valid/id/payload/flush combinations against the model."""
    w = widths(dut)
    n, idw = w["num_ports"], w["idw"]
    sb = Scoreboard("arbiter.random")
    rng = random.Random(0xA2B1)

    for _ in range(RANDOM_VECTORS):
        valid = [rng.getrandbits(1) for _ in range(n)]
        ids = [rng.randrange(1 << idw) for _ in range(n)]
        data = [rng.getrandbits(32) for _ in range(n)]
        flags = [rng.getrandbits(5) for _ in range(n)]
        flush = rng.getrandbits(1) if rng.random() < 0.3 else 0
        # Bias flush_id towards an id that is actually present.
        flush_id = rng.choice(ids) if (flush and rng.random() < 0.7) else rng.randrange(1 << idw)
        ports = blank(w, valid=valid, ids=ids, data=data, fflags=flags,
                      flush=flush, flush_id=flush_id)
        await check(dut, sb, w, ports, "random")

    sb.log_summary(dut)
    sb.report()
