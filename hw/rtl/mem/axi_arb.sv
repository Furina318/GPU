`include "defines.vh"

// ============================================================================
//  axi_arb —— 多主 → 1 个 AXI4 主口的仲裁器(SPEC.md §3.4)
//
//  主:CP 的 DMA、2 个 SM 的访存、2D 引擎、矩阵引擎。
//  读(AR/R)与写(AW/W/B)两个通道各自独立仲裁, 互不阻塞。
//
//  口径(SPEC §3.4、MEM_LSU.md §4):
//   · 每个通道同一时刻只有一个主在飞(burst 级仲裁, 不是 beat 级);
//   · 未授予的主 s_*ready 恒 0 —— 仲裁只决定"让谁走", 从不丢弃请求;
//   · 授予与下游握手同时发生(`s_arready = sel && m_arready`), 所以主侧看到
//     ready 时 DDR 一定已经收下了这一笔;
//   · 响应按"当前在飞的主"路由(burst 级仲裁下不需要 id 查表);
//   · CP(索引 0)最高优先:命令级流量(取指 / LOAD_KERNEL)不能被 SM 的流式
//     读写饿死;其余主之间从上次被服务者之后轮转。
//
//  端口是扁平向量而不是 modport 数组:各主在自己的顶层把 mem/axi4_bus.sv 的
//  modport 接到这些信号上即可, 仿真/综合都少一层层次化名字。
// ============================================================================

module axi_arb #(
    parameter int NUM_M  = 3,               // CP / SM0 / SM1(2D、GEMM 暂无 RTL)
    parameter int ADDR_W = 32,
    parameter int DATA_W = 32,
    parameter int ID_W   = 4,
    parameter int LEN_W  = 8
) (
    input  wire                 clk,
    input  wire                 rst,
    // ---------------- 从端口(各主) ----------------
    input  wire [NUM_M-1:0]              s_arvalid,
    output logic [NUM_M-1:0]             s_arready,
    input  wire [NUM_M-1:0][ADDR_W-1:0]  s_araddr,
    input  wire [NUM_M-1:0][LEN_W-1:0]   s_arlen,
    input  wire [NUM_M-1:0][2:0]         s_arsize,
    input  wire [NUM_M-1:0][1:0]         s_arburst,
    output logic [NUM_M-1:0]             s_rvalid,
    input  wire [NUM_M-1:0]              s_rready,
    output logic [NUM_M-1:0][DATA_W-1:0] s_rdata,
    output logic [NUM_M-1:0][1:0]        s_rresp,
    output logic [NUM_M-1:0]             s_rlast,

    input  wire [NUM_M-1:0]              s_awvalid,
    output logic [NUM_M-1:0]             s_awready,
    input  wire [NUM_M-1:0][ADDR_W-1:0]  s_awaddr,
    input  wire [NUM_M-1:0][LEN_W-1:0]   s_awlen,
    input  wire [NUM_M-1:0][2:0]         s_awsize,
    input  wire [NUM_M-1:0][1:0]         s_awburst,
    input  wire [NUM_M-1:0]              s_wvalid,
    output logic [NUM_M-1:0]             s_wready,
    input  wire [NUM_M-1:0][DATA_W-1:0]  s_wdata,
    input  wire [NUM_M-1:0][DATA_W/8-1:0] s_wstrb,
    input  wire [NUM_M-1:0]              s_wlast,
    output logic [NUM_M-1:0]             s_bvalid,
    input  wire [NUM_M-1:0]              s_bready,
    output logic [NUM_M-1:0][1:0]        s_bresp,

    // ---------------- 主端口(到 DDR) ----------------
    output logic                m_arvalid,
    input  wire                 m_arready,
    output logic [ADDR_W-1:0]   m_araddr,
    output logic [LEN_W-1:0]    m_arlen,
    output logic [2:0]          m_arsize,
    output logic [1:0]          m_arburst,
    output logic [ID_W-1:0]     m_arid,
    input  wire                 m_rvalid,
    output logic                m_rready,
    input  wire [DATA_W-1:0]    m_rdata,
    input  wire [1:0]           m_rresp,
    input  wire                 m_rlast,

    output logic                m_awvalid,
    input  wire                 m_awready,
    output logic [ADDR_W-1:0]   m_awaddr,
    output logic [LEN_W-1:0]    m_awlen,
    output logic [2:0]          m_awsize,
    output logic [1:0]          m_awburst,
    output logic [ID_W-1:0]     m_awid,
    output logic                m_wvalid,
    input  wire                 m_wready,
    output logic [DATA_W-1:0]   m_wdata,
    output logic [DATA_W/8-1:0] m_wstrb,
    output logic                m_wlast,
    input  wire                 m_bvalid,
    output logic                m_bready,
    input  wire [1:0]           m_bresp
);

    localparam int MW = (NUM_M > 1) ? $clog2(NUM_M) : 1;

    // ------------------------------------------------------------------
    //  读通道
    // ------------------------------------------------------------------
    logic          r_busy;      // AR 已发出, 正在收 R
    logic [MW-1:0] r_sel;       // 在飞/将授予的主
    logic [MW-1:0] r_nxt;       // 组合选出的候选主

    // CP 优先, 其余轮转(从上次被服务的 r_sel 之后开始)
    always_comb begin : r_pick
        integer k;
        logic [MW-1:0] idx;
        logic   found;
        r_nxt = '0;
        found = 1'b0;
        if (s_arvalid[0]) begin
            r_nxt = '0;
            found = 1'b1;
        end
        for (k = 0; k < NUM_M; k = k + 1) begin
            idx = MW'((int'(r_sel) + 1 + k) % NUM_M);
            if (!found && s_arvalid[idx]) begin
                r_nxt = idx;
                found = 1'b1;
            end
        end
    end

    assign m_arvalid = !r_busy && (|s_arvalid);
    assign m_araddr  = s_araddr[r_nxt];
    assign m_arlen   = s_arlen[r_nxt];
    assign m_arsize  = s_arsize[r_nxt];
    assign m_arburst = s_arburst[r_nxt];
    assign m_arid    = ID_W'(r_nxt);

    always_comb begin : r_ready
        integer i;
        for (i = 0; i < NUM_M; i = i + 1)
            s_arready[i] = (!r_busy) && (i[MW-1:0] == r_nxt) && m_arready;
    end

    always_comb begin : r_out
        integer i;
        for (i = 0; i < NUM_M; i = i + 1) begin
            s_rvalid[i] = r_busy && (i[MW-1:0] == r_sel) && m_rvalid;
            s_rdata[i]  = m_rdata;
            s_rresp[i]  = m_rresp;
            s_rlast[i]  = m_rlast;
        end
    end
    assign m_rready = r_busy && s_rready[r_sel];

    // ------------------------------------------------------------------
    //  写通道
    // ------------------------------------------------------------------
    logic          w_busy;      // AW 已发出
    logic          w_aw_done;   // AW 已握手, 正在发 W
    logic          w_b_pend;    // 最后一拍 W 已发, 等 B
    logic [MW-1:0] w_sel;
    logic [MW-1:0] w_nxt;

    always_comb begin : w_pick
        integer k;
        logic [MW-1:0] idx;
        logic   found;
        w_nxt = '0;
        found = 1'b0;
        if (s_awvalid[0]) begin
            w_nxt = '0;
            found = 1'b1;
        end
        for (k = 0; k < NUM_M; k = k + 1) begin
            idx = MW'((int'(w_sel) + 1 + k) % NUM_M);
            if (!found && s_awvalid[idx]) begin
                w_nxt = idx;
                found = 1'b1;
            end
        end
    end

    assign m_awvalid = !w_busy && (|s_awvalid);
    assign m_awaddr  = s_awaddr[w_nxt];
    assign m_awlen   = s_awlen[w_nxt];
    assign m_awsize  = s_awsize[w_nxt];
    assign m_awburst = s_awburst[w_nxt];
    assign m_awid    = ID_W'(w_nxt);

    assign m_wvalid = w_busy && w_aw_done && !w_b_pend && s_wvalid[w_sel];
    assign m_wdata  = s_wdata[w_sel];
    assign m_wstrb  = s_wstrb[w_sel];
    assign m_wlast  = s_wlast[w_sel];
    assign m_bready = w_busy && w_b_pend && s_bready[w_sel];

    always_comb begin : w_ready
        integer i;
        for (i = 0; i < NUM_M; i = i + 1) begin
            s_awready[i] = (!w_busy) && (i[MW-1:0] == w_nxt) && m_awready;
            s_wready[i]  = w_busy && w_aw_done && !w_b_pend &&
                           (i[MW-1:0] == w_sel) && m_wready;
        end
    end

    always_comb begin : w_out
        integer i;
        for (i = 0; i < NUM_M; i = i + 1) begin
            s_bvalid[i] = w_busy && w_b_pend && (i[MW-1:0] == w_sel) && m_bvalid;
            s_bresp[i]  = m_bresp;
        end
    end

    // ------------------------------------------------------------------
    //  时序
    // ------------------------------------------------------------------
    always_ff @(posedge clk) begin
        if (rst) begin
            r_busy    <= 1'b0;
            r_sel     <= '0;
            w_busy    <= 1'b0;
            w_aw_done <= 1'b0;
            w_b_pend  <= 1'b0;
            w_sel     <= '0;
        end else begin
            // ---- 读 ----
            if (!r_busy) begin
                if (m_arvalid && m_arready) begin
                    r_busy <= 1'b1;
                    r_sel  <= r_nxt;
                end
            end else if (m_rvalid && m_rready && m_rlast) begin
                r_busy <= 1'b0;
            end

            // ---- 写 ----
            if (!w_busy) begin
                if (m_awvalid && m_awready) begin
                    w_busy    <= 1'b1;
                    w_sel     <= w_nxt;
                    w_aw_done <= 1'b1;
                    w_b_pend  <= 1'b0;
                end
            end else begin
                if (w_aw_done && !w_b_pend && m_wvalid && m_wready && m_wlast) begin
                    w_aw_done <= 1'b0;
                    w_b_pend  <= 1'b1;
                end
                if (w_b_pend && m_bvalid && m_bready) begin
                    w_b_pend <= 1'b0;
                    w_busy   <= 1'b0;
                    w_aw_done <= 1'b0;
                end
            end
        end
    end

endmodule
