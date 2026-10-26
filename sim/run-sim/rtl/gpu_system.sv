`include "defines.vh"

// ============================================================================
//  gpu_system —— RTL 全系统顶层(CP + 2×SM + 内存子系统 + DDR 模型)
//
//  用途: 给 `sim/run-sim/` 的 Verilator 测试台用, 让 `sw/` 里的程序真正跑在
//  RTL 上。结构与 SPEC §1 一致:
//
//    host ──AXI4-Lite──> CP ──行/字访存──> cp_mem_adapter ─┐
//                         │                                ├─ axi_arb ─→ axi_ram_model(DDR)
//                         │ launch/done/error              │
//                         ├──> core0 ──> global_mem[0] ────┤
//                         └──> core1 ──> global_mem[1] ────┘
//
//  · 指令 SRAM 与共享内存各在 `core` 内部(每 SM 一份, 16KB);
//  · 2D / 矩阵引擎暂无 RTL, 引擎侧端口接到常数(`eng_ready = 0`, 状态恒 0);
//  · host 通过 `ram_*` 旁路口直接读写 DDR 模型(铺镜像/常量/读帧缓冲),
//    通过 AXI4-Lite 口读写 CP 的 MMIO 寄存器。
// ============================================================================

// ----------------------------------------------------------------------------
//  架构地址 → DDR 模型的局部地址
//
//  架构地址是分散的(命令环 0x0100_0000 / 常量与镜像 0x0200_0000 / 信号量
//  0x0300_0000 / 帧缓冲 0x1000_0000 / DDR 0x8000_0000), 而 `axi_ram_model`
//  只有一块连续存储。这里按区搬进局部空间, 只影响仿真(真实硬件没有这一步)。
// ----------------------------------------------------------------------------
function automatic logic [31:0] loc_addr(input logic [31:0] a);
    if (a >= `DDR_BASE)                       loc_addr = 32'h0100_0000 + (a - `DDR_BASE);
    else if (a >= 32'h1000_0000)              loc_addr = 32'h0010_0000 + (a - 32'h1000_0000);
    else if (a >= 32'h0300_0000)              loc_addr = 32'h0002_0000 + (a - 32'h0300_0000);
    else if (a >= `CONST_BASE)                loc_addr = 32'h0001_0000 + (a - `CONST_BASE);
    else if (a >= `CMD_QUEUE_BASE)            loc_addr = 32'h0000_0000 + (a - `CMD_QUEUE_BASE);
    else                                      loc_addr = 32'h01F0_0000 + a[19:0];
endfunction

module gpu_system #(
    parameter int DDR_WORDS = 1 << 23      // 8M 字 = 32MB, 折叠后最大的窗口(DDR 段)也要装得下
) (
    input  wire clk,
    input  wire rst,

    // ---- host: AXI4-Lite(CP 的 MMIO 窗口) ----
    input  wire        h_awvalid,
    output logic       h_awready,
    input  wire [31:0] h_awaddr,
    input  wire        h_wvalid,
    output logic       h_wready,
    input  wire [31:0] h_wdata,
    input  wire [3:0]  h_wstrb,
    output logic       h_bvalid,
    input  wire        h_bready,
    output logic [1:0]  h_bresp,
    input  wire        h_arvalid,
    output logic       h_arready,
    input  wire [31:0] h_araddr,
    output logic       h_rvalid,
    input  wire        h_rready,
    output logic [31:0] h_rdata,
    output logic [1:0]  h_rresp,

    // ---- host: DDR 旁路 ----
    input  wire        ram_we,
    input  wire [31:0] ram_addr,
    input  wire [31:0] ram_wdata,
    output logic [31:0] ram_rdata,

    // ---- 观察 ----
    output logic        irq,
    output logic        sm0_done,
    output logic        sm1_done,
    output logic [3:0]  sm0_error,
    output logic [3:0]  sm1_error,
    // ---- 调试探针(仿真专用) ----
    output logic [31:0] dbg_crc,
    output logic [31:0] dbg_hdr4,
    output logic [31:0] dbg_hdr2,
    output logic [31:0] dbg_hdr0,
    output logic [31:0] dbg_hdr1,
    output logic [31:0] dbg_hdr5,
    output logic [31:0] dbg_w3,
    output logic [31:0] dbg_words,
    output logic [31:0] dbg_idx,
    output logic [4:0]  dbg_state,
    output logic [1:0]  dbg_ovf,
    output logic [1:0]  dbg_lddone,
    output logic [31:0] dbg_head,
    output logic [31:0] dbg_w0,
    output logic [15:0] dbg_len,
    output logic [31:0] dbg_cw3,
    output logic [31:0] dbg_cw2,
    output logic [31:0] dbg_cw4,
    output logic [3:0]  dbg_wst0,
    output logic [3:0]  dbg_smerr0,
    output logic [31:0] dbg_pc0,
    output logic [7:0]  dbg_wrun0,
    output logic [2:0]  dbg_exu_st,
    output logic [2:0]  dbg_lsu_st,
    output logic [2:0]  dbg_gm_wst,
    output logic [3:0]  dbg_wst_all,
    output logic [7:0]  dbg_sched_busy,

    output logic [31:0] ctr_lines,
    output logic [31:0] ctr_mmio,
    output logic [31:0] ctr_unmapped
);

    // ------------------------------------------------------------------
    //  CP
    // ------------------------------------------------------------------
    logic        cp_mem_rd_req, cp_mem_rd_line, cp_mem_rd_ack, cp_mem_rd_rsp, cp_mem_rd_err;
    logic [31:0] cp_mem_rd_addr;
    logic [16*32-1:0] cp_mem_rd_data;
    logic        cp_mem_wr_req, cp_mem_wr_ack, cp_mem_wr_rsp, cp_mem_wr_err;
    logic [31:0] cp_mem_wr_addr;
    logic [31:0] cp_mem_wr_data;
    logic [3:0]  cp_mem_wr_strb;

    logic [1:0]  cp_launch_valid;
    logic [31:0] cp_launch_pc;
    logic [3:0]  cp_launch_warps, cp_launch_flags;
    logic [1:0]  cp_sm_done;
    logic [3:0]  cp_sm0_error, cp_sm1_error;
    logic [31:0] cp_sm0_fault_pc, cp_sm1_fault_pc;
    logic [1:0]  cp_sm_err_clr;

    logic [1:0]  cp_sram_load_en, cp_sram_load_last, cp_sram_load_done, cp_sram_err_ovf;
    logic [12:0] cp_sram_load_addr;
    logic [31:0] cp_sram_load_data;

    logic        cp_eng_valid, cp_eng_wvalid, cp_eng_wlast;
    logic [1:0]  cp_eng_engine, cp_eng_err_clr;
    logic [7:0]  cp_eng_opcode;
    logic [15:0] cp_eng_len;
    logic [31:0] cp_eng_wdata;

    cp u_cp (
        .clk(clk), .rst(rst),
        .s_awvalid(h_awvalid), .s_awready(h_awready), .s_awaddr(h_awaddr),
        .s_wvalid(h_wvalid), .s_wready(h_wready), .s_wdata(h_wdata), .s_wstrb(h_wstrb),
        .s_bvalid(h_bvalid), .s_bready(h_bready), .s_bresp(h_bresp),
        .s_arvalid(h_arvalid), .s_arready(h_arready), .s_araddr(h_araddr),
        .s_rvalid(h_rvalid), .s_rready(h_rready), .s_rdata(h_rdata), .s_rresp(h_rresp),
        .irq(irq),
        .mem_rd_req(cp_mem_rd_req), .mem_rd_addr(cp_mem_rd_addr), .mem_rd_line(cp_mem_rd_line),
        .mem_rd_ack(cp_mem_rd_ack), .mem_rd_rsp(cp_mem_rd_rsp),
        .mem_rd_data(cp_mem_rd_data), .mem_rd_err(cp_mem_rd_err),
        .mem_wr_req(cp_mem_wr_req), .mem_wr_addr(cp_mem_wr_addr),
        .mem_wr_data(cp_mem_wr_data), .mem_wr_strb(cp_mem_wr_strb),
        .mem_wr_ack(cp_mem_wr_ack), .mem_wr_rsp(cp_mem_wr_rsp), .mem_wr_err(cp_mem_wr_err),
        .sm_launch_valid(cp_launch_valid), .sm_launch_pc(cp_launch_pc),
        .sm_launch_warps(cp_launch_warps), .sm_launch_flags(cp_launch_flags),
        .sm_done(cp_sm_done), .sm0_error(cp_sm0_error), .sm1_error(cp_sm1_error),
        .sm0_fault_pc(cp_sm0_fault_pc), .sm1_fault_pc(cp_sm1_fault_pc),
        .sm_err_clr(cp_sm_err_clr),
        .sram_load_en(cp_sram_load_en), .sram_load_addr(cp_sram_load_addr),
        .sram_load_data(cp_sram_load_data), .sram_load_last(cp_sram_load_last),
        .sram_load_done(cp_sram_load_done), .sram_err_ovf(cp_sram_err_ovf),
        .eng_valid(cp_eng_valid), .eng_engine(cp_eng_engine), .eng_opcode(cp_eng_opcode),
        .eng_len(cp_eng_len), .eng_wvalid(cp_eng_wvalid), .eng_wdata(cp_eng_wdata),
        .eng_wlast(cp_eng_wlast), .eng_ready(1'b0),
        .gemm_status(3'b000), .gemm_error(8'h0),
        .twod_status(3'b000), .twod_error(8'h0),
        .eng_err_clr(cp_eng_err_clr)
    );

    assign dbg_crc   = u_cp.crc_val;
    assign dbg_hdr4  = u_cp.img_hdr[4];
    assign dbg_hdr2  = u_cp.img_hdr[2];
    assign dbg_hdr0  = u_cp.img_hdr[0];
    assign dbg_hdr1  = u_cp.img_hdr[1];
    assign dbg_hdr5  = u_cp.img_hdr[5];
    assign dbg_w3     = u_cp.w3_q;
    assign dbg_words = {20'h0, u_cp.load_words};
    assign dbg_idx   = {20'h0, u_cp.idx};
    assign dbg_state = u_cp.st;
    assign dbg_ovf   = cp_sram_err_ovf;
    assign dbg_lddone= cp_sram_load_done;
    assign dbg_head  = u_cp.head;
    assign dbg_w0    = u_cp.cmd_w[0];
    assign dbg_len   = u_cp.len_q;
    assign dbg_cw2   = u_cp.cmd_w[2];
    assign dbg_cw3   = u_cp.cmd_w[3];
    assign dbg_cw4   = u_cp.cmd_w[4];
    assign dbg_wst0  = u_core0.g_warp[0].wst;
    assign dbg_smerr0= u_core0.warp_err_code[0];
    assign dbg_pc0   = u_core0.g_warp[0].u_fetch.pc;
    assign dbg_wrun0 = u_core0.warp_run;
    assign dbg_exu_st = u_core0.g_warp[0].u_exu.st;
    assign dbg_lsu_st = u_core0.g_warp[0].u_lsu.st;
    assign dbg_gm_wst = u_gm0.wr_st;
    assign dbg_wst_all = {u_core0.g_warp[3].wst, u_core0.g_warp[2].wst,
                          u_core0.g_warp[1].wst, u_core0.g_warp[0].wst};
    assign dbg_sched_busy = u_core0.mem_busy;

    /* verilator lint_off UNUSEDSIGNAL */
    logic unused_eng = cp_eng_valid | cp_eng_wvalid | cp_eng_wlast | (|cp_eng_engine) |
                       (|cp_eng_opcode) | (|cp_eng_len) | (|cp_eng_wdata) | (|cp_eng_err_clr);
    /* verilator lint_on UNUSEDSIGNAL */

    // ------------------------------------------------------------------
    //  两个 SM
    // ------------------------------------------------------------------
    logic [1:0]  sm_grd_valid, sm_grd_ready, sm_grsp_valid, sm_grsp_err;
    logic [1:0]  sm_gwr_valid, sm_gwr_ready, sm_gwr_err, sm_flush_req, sm_flush_done, sm_busy;
    logic [26:0] sm_grd_line [2], sm_gwr_line [2], sm_grsp_line [2];
    logic [7:0]  sm_grd_wen [2], sm_gwr_wen [2];
    logic [7:0][31:0] sm_grsp_data [2], sm_gwr_data [2];

    // 两个 SM 显式实例化(只有 2 个, 比 generate 更好读、避免 genvar 展开问题)
    core u_core0 (
        .clk(clk), .rst(rst),
        .launch(cp_launch_valid[0]), .launch_pc(cp_launch_pc),
        .launch_warps(cp_launch_warps), .launch_flags(cp_launch_flags),
        .sram_load_en(cp_sram_load_en[0]), .sram_load_addr(cp_sram_load_addr),
        .sram_load_data(cp_sram_load_data), .sram_load_last(cp_sram_load_last[0]),
        .sram_load_done(cp_sram_load_done[0]), .sram_load_ovf(cp_sram_err_ovf[0]),
        .sm_done(cp_sm_done[0]), .sm_error(cp_sm0_error), .sm_fault_pc(cp_sm0_fault_pc),
        .gmem_rd_valid(sm_grd_valid[0]), .gmem_rd_ready(sm_grd_ready[0]),
        .gmem_rd_line(sm_grd_line[0]), .gmem_rd_wen(sm_grd_wen[0]),
        .gmem_rsp_valid(sm_grsp_valid[0]), .gmem_rsp_line(sm_grsp_line[0]),
        .gmem_rsp_data(sm_grsp_data[0]), .gmem_rsp_err(sm_grsp_err[0]),
        .gmem_wr_valid(sm_gwr_valid[0]), .gmem_wr_ready(sm_gwr_ready[0]),
        .gmem_wr_line(sm_gwr_line[0]), .gmem_wr_wen(sm_gwr_wen[0]),
        .gmem_wr_wdata(sm_gwr_data[0]), .gmem_wr_err(sm_gwr_err[0]),
        .flush_req(sm_flush_req[0]), .flush_done(sm_flush_done[0]),
        .mem_busy(sm_busy[0])
    );

    core u_core1 (
        .clk(clk), .rst(rst),
        .launch(cp_launch_valid[1]), .launch_pc(cp_launch_pc),
        .launch_warps(cp_launch_warps), .launch_flags(cp_launch_flags),
        .sram_load_en(cp_sram_load_en[1]), .sram_load_addr(cp_sram_load_addr),
        .sram_load_data(cp_sram_load_data), .sram_load_last(cp_sram_load_last[1]),
        .sram_load_done(cp_sram_load_done[1]), .sram_load_ovf(cp_sram_err_ovf[1]),
        .sm_done(cp_sm_done[1]), .sm_error(cp_sm1_error), .sm_fault_pc(cp_sm1_fault_pc),
        .gmem_rd_valid(sm_grd_valid[1]), .gmem_rd_ready(sm_grd_ready[1]),
        .gmem_rd_line(sm_grd_line[1]), .gmem_rd_wen(sm_grd_wen[1]),
        .gmem_rsp_valid(sm_grsp_valid[1]), .gmem_rsp_line(sm_grsp_line[1]),
        .gmem_rsp_data(sm_grsp_data[1]), .gmem_rsp_err(sm_grsp_err[1]),
        .gmem_wr_valid(sm_gwr_valid[1]), .gmem_wr_ready(sm_gwr_ready[1]),
        .gmem_wr_line(sm_gwr_line[1]), .gmem_wr_wen(sm_gwr_wen[1]),
        .gmem_wr_wdata(sm_gwr_data[1]), .gmem_wr_err(sm_gwr_err[1]),
        .flush_req(sm_flush_req[1]), .flush_done(sm_flush_done[1]),
        .mem_busy(sm_busy[1])
    );

    assign sm0_done  = cp_sm_done[0];
    assign sm1_done  = cp_sm_done[1];
    assign sm0_error = cp_sm0_error;
    assign sm1_error = cp_sm1_error;

    // ------------------------------------------------------------------
    //  仲裁器 + DDR
    // ------------------------------------------------------------------
    logic       m_arvalid, m_arready, m_rvalid, m_rready, m_rlast;
    logic [31:0] m_araddr, m_rdata;
    logic [7:0]  m_arlen;
    logic [2:0]  m_arsize;
    logic [1:0]  m_arburst, m_rresp;
    logic       m_awvalid, m_awready, m_wvalid, m_wready, m_wlast, m_bvalid, m_bready;
    logic [31:0] m_awaddr, m_wdata;
    logic [7:0]  m_awlen;
    logic [2:0]  m_awsize;
    logic [1:0]  m_awburst, m_bresp;
    logic [3:0]  m_wstrb;

    // 三个主: 0 = CP, 1 = SM0, 2 = SM1
    logic [2:0]        a_arvalid, a_arready, a_rvalid, a_rready, a_rlast;
    logic [2:0]        a_awvalid, a_awready, a_wvalid, a_wready, a_wlast;
    logic [2:0]        a_bvalid, a_bready;
    logic [2:0][31:0]  a_araddr, a_awaddr, a_rdata, a_wdata;
    logic [2:0][7:0]   a_arlen, a_awlen;
    logic [2:0][2:0]   a_arsize, a_awsize;
    logic [2:0][1:0]   a_arburst, a_awburst, a_rresp, a_bresp;
    logic [2:0][3:0]   a_wstrb;

    // CP 适配器 → 主 0
    cp_mem_adapter u_cp_ad (
        .clk(clk), .rst(rst),
        .rd_req(cp_mem_rd_req), .rd_addr(cp_mem_rd_addr), .rd_line(cp_mem_rd_line),
        .rd_ack(cp_mem_rd_ack), .rd_rsp(cp_mem_rd_rsp),
        .rd_data(cp_mem_rd_data), .rd_err(cp_mem_rd_err),
        .wr_req(cp_mem_wr_req), .wr_addr(cp_mem_wr_addr),
        .wr_data(cp_mem_wr_data), .wr_strb(cp_mem_wr_strb),
        .wr_ack(cp_mem_wr_ack), .wr_rsp(cp_mem_wr_rsp), .wr_err(cp_mem_wr_err),
        .m_arvalid(a_arvalid[0]), .m_arready(a_arready[0]), .m_araddr(a_araddr[0]),
        .m_arlen(a_arlen[0]), .m_arsize(a_arsize[0]), .m_arburst(a_arburst[0]),
        .m_rvalid(a_rvalid[0]), .m_rready(a_rready[0]), .m_rdata(a_rdata[0]),
        .m_rresp(a_rresp[0]), .m_rlast(a_rlast[0]),
        .m_awvalid(a_awvalid[0]), .m_awready(a_awready[0]), .m_awaddr(a_awaddr[0]),
        .m_awlen(a_awlen[0]), .m_awsize(a_awsize[0]), .m_awburst(a_awburst[0]),
        .m_wvalid(a_wvalid[0]), .m_wready(a_wready[0]), .m_wdata(a_wdata[0]),
        .m_wstrb(a_wstrb[0]), .m_wlast(a_wlast[0]),
        .m_bvalid(a_bvalid[0]), .m_bready(a_bready[0]), .m_bresp(a_bresp[0])
    );

    // SM 全局通路 → 主 1/2
    global_mem u_gm0 (
        .clk(clk), .rst(rst),
        .rd_valid(sm_grd_valid[0]), .rd_ready(sm_grd_ready[0]),
        .rd_line(sm_grd_line[0]), .rd_wen(sm_grd_wen[0]),
        .rsp_valid(sm_grsp_valid[0]), .rsp_line(sm_grsp_line[0]),
        .rsp_data(sm_grsp_data[0]), .rsp_err(sm_grsp_err[0]),
        .wr_valid(sm_gwr_valid[0]), .wr_ready(sm_gwr_ready[0]),
        .wr_line(sm_gwr_line[0]), .wr_wen(sm_gwr_wen[0]), .wr_wdata(sm_gwr_data[0]),
        .flush_req(sm_flush_req[0]), .flush_done(sm_flush_done[0]),
        .busy(sm_busy[0]), .wr_err(sm_gwr_err[0]),
        .m_arvalid(a_arvalid[1]), .m_arready(a_arready[1]),
        .m_araddr(a_araddr[1]), .m_arlen(a_arlen[1]),
        .m_arsize(a_arsize[1]), .m_arburst(a_arburst[1]),
        .m_arid(),
        .m_rvalid(a_rvalid[1]), .m_rready(a_rready[1]),
        .m_rdata(a_rdata[1]), .m_rresp(a_rresp[1]), .m_rlast(a_rlast[1]),
        .m_rid(4'd0),
        .m_awvalid(a_awvalid[1]), .m_awready(a_awready[1]),
        .m_awaddr(a_awaddr[1]), .m_awlen(a_awlen[1]),
        .m_awsize(a_awsize[1]), .m_awburst(a_awburst[1]),
        .m_awid(),
        .m_wvalid(a_wvalid[1]), .m_wready(a_wready[1]),
        .m_wdata(a_wdata[1]), .m_wstrb(a_wstrb[1]), .m_wlast(a_wlast[1]),
        .m_bvalid(a_bvalid[1]), .m_bready(a_bready[1]), .m_bresp(a_bresp[1]),
        .m_bid()
    );

    global_mem u_gm1 (
        .clk(clk), .rst(rst),
        .rd_valid(sm_grd_valid[1]), .rd_ready(sm_grd_ready[1]),
        .rd_line(sm_grd_line[1]), .rd_wen(sm_grd_wen[1]),
        .rsp_valid(sm_grsp_valid[1]), .rsp_line(sm_grsp_line[1]),
        .rsp_data(sm_grsp_data[1]), .rsp_err(sm_grsp_err[1]),
        .wr_valid(sm_gwr_valid[1]), .wr_ready(sm_gwr_ready[1]),
        .wr_line(sm_gwr_line[1]), .wr_wen(sm_gwr_wen[1]), .wr_wdata(sm_gwr_data[1]),
        .flush_req(sm_flush_req[1]), .flush_done(sm_flush_done[1]),
        .busy(sm_busy[1]), .wr_err(sm_gwr_err[1]),
        .m_arvalid(a_arvalid[2]), .m_arready(a_arready[2]),
        .m_araddr(a_araddr[2]), .m_arlen(a_arlen[2]),
        .m_arsize(a_arsize[2]), .m_arburst(a_arburst[2]),
        .m_arid(),
        .m_rvalid(a_rvalid[2]), .m_rready(a_rready[2]),
        .m_rdata(a_rdata[2]), .m_rresp(a_rresp[2]), .m_rlast(a_rlast[2]),
        .m_rid(4'd0),
        .m_awvalid(a_awvalid[2]), .m_awready(a_awready[2]),
        .m_awaddr(a_awaddr[2]), .m_awlen(a_awlen[2]),
        .m_awsize(a_awsize[2]), .m_awburst(a_awburst[2]),
        .m_awid(),
        .m_wvalid(a_wvalid[2]), .m_wready(a_wready[2]),
        .m_wdata(a_wdata[2]), .m_wstrb(a_wstrb[2]), .m_wlast(a_wlast[2]),
        .m_bvalid(a_bvalid[2]), .m_bready(a_bready[2]), .m_bresp(a_bresp[2]),
        .m_bid()
    );

    logic a_refresh;
    axi_arb #(.NUM_M(3), .ADDR_W(32), .DATA_W(32), .ID_W(4), .LEN_W(8)) u_arb (
        .clk(clk), .rst(rst),
        .s_arvalid(a_arvalid), .s_arready(a_arready), .s_araddr(a_araddr),
        .s_arlen(a_arlen), .s_arsize(a_arsize), .s_arburst(a_arburst),
        .s_rvalid(a_rvalid), .s_rready(a_rready), .s_rdata(a_rdata),
        .s_rresp(a_rresp), .s_rlast(a_rlast),
        .s_awvalid(a_awvalid), .s_awready(a_awready), .s_awaddr(a_awaddr),
        .s_awlen(a_awlen), .s_awsize(a_awsize), .s_awburst(a_awburst),
        .s_wvalid(a_wvalid), .s_wready(a_wready), .s_wdata(a_wdata),
        .s_wstrb(a_wstrb), .s_wlast(a_wlast),
        .s_bvalid(a_bvalid), .s_bready(a_bready), .s_bresp(a_bresp),
        .m_arvalid(m_arvalid), .m_arready(m_arready), .m_araddr(m_araddr),
        .m_arlen(m_arlen), .m_arsize(m_arsize), .m_arburst(m_arburst), .m_arid(),
        .m_rvalid(m_rvalid), .m_rready(m_rready), .m_rdata(m_rdata),
        .m_rresp(m_rresp), .m_rlast(m_rlast),
        .m_awvalid(m_awvalid), .m_awready(m_awready), .m_awaddr(m_awaddr),
        .m_awlen(m_awlen), .m_awsize(m_awsize), .m_awburst(m_awburst), .m_awid(),
        .m_wvalid(m_wvalid), .m_wready(m_wready), .m_wdata(m_wdata),
        .m_wstrb(m_wstrb), .m_wlast(m_wlast),
        .m_bvalid(m_bvalid), .m_bready(m_bready), .m_bresp(m_bresp)
    );

    wire [31:0] ddr_araddr = loc_addr(m_araddr);
    wire [31:0] ddr_awaddr = loc_addr(m_awaddr);
    wire [31:0] ddr_haaddr = loc_addr(ram_addr);

    axi_ram_model #(.DEPTH_WORDS(DDR_WORDS)) u_ddr (
        .clk(clk), .rst(rst),
        .m_arvalid(m_arvalid), .m_arready(m_arready), .m_araddr(ddr_araddr),
        .m_arlen(m_arlen),
        .m_rvalid(m_rvalid), .m_rready(m_rready), .m_rdata(m_rdata),
        .m_rresp(m_rresp), .m_rlast(m_rlast),
        .m_awvalid(m_awvalid), .m_awready(m_awready), .m_awaddr(ddr_awaddr),
        .m_awlen(m_awlen),
        .m_wvalid(m_wvalid), .m_wready(m_wready), .m_wdata(m_wdata),
        .m_wstrb(m_wstrb), .m_wlast(m_wlast),
        .m_bvalid(m_bvalid), .m_bready(m_bready), .m_bresp(m_bresp),
        .host_we(ram_we), .host_addr(ddr_haaddr),
        .host_wdata(ram_wdata), .host_rdata(ram_rdata),
        .stat_ar_cnt(), .stat_aw_cnt()
    );

    /* verilator lint_off UNUSEDSIGNAL */
    logic [2:0] u_sz = m_arsize | m_awsize;
    /* verilator lint_on UNUSEDSIGNAL */

    // ------------------------------------------------------------------
    //  性能计数器: 行请求数 = 仲裁器主口上的 AR/AW 握手
    // ------------------------------------------------------------------
    wire ev_line = (m_arvalid && m_arready) || (m_awvalid && m_awready);
    mem_perf u_perf (
        .clk(clk), .rst(rst), .clear(1'b0),
        .ev_line(ev_line), .ev_shared_extra(4'd0),
        .ev_mmio(1'b0), .ev_unmapped(1'b0),
        .ctr_lines(ctr_lines), .ctr_shared_extra(),
        .ctr_mmio(ctr_mmio), .ctr_unmapped(ctr_unmapped)
    );

    /* verilator lint_off UNUSEDSIGNAL */
    logic [31:0] u_perf2 = ctr_mmio | ctr_unmapped;
    /* verilator lint_on UNUSEDSIGNAL */

endmodule
