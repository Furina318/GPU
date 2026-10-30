`include "defines.vh"

// ============================================================================
//  cp_mem_adapter —— CP 的行/字访存协议 → AXI4 主口
//
//  CP 的内存口是"请求 → ack → rsp"的两段式:
//    读: mem_rd_req + mem_rd_addr + mem_rd_line(1 = 64B 行, 0 = 4B 字)
//        接受后回 mem_rd_ack(单拍), 数据回来时回 mem_rd_rsp + mem_rd_data
//    写: mem_wr_req + mem_wr_addr + mem_wr_data + mem_wr_strb
//        接受后回 mem_wr_ack, 落盘完成回 mem_wr_rsp
//  本模块把它翻成 AXI4: 64B 行 = 两段 8 拍 burst(32B), 4B 字 = 1 拍 burst。
//  CP 一次只有一个内存操作在飞, 所以这里不做 pipelining(outstanding = 1)。
// ============================================================================

module cp_mem_adapter #(
    parameter int LINE_WORDS = 16,
    parameter int ADDR_W     = 32,
    parameter int DATA_W     = 32
) (
    input  wire clk,
    input  wire rst,

    // ---------------- CP 侧 ----------------
    input  wire                     rd_req,
    input  wire [ADDR_W-1:0]        rd_addr,
    input  wire                     rd_line,
    output logic                    rd_ack,
    output logic                    rd_rsp,
    output logic [LINE_WORDS*32-1:0] rd_data,
    output logic                    rd_err,

    input  wire                     wr_req,
    input  wire [ADDR_W-1:0]        wr_addr,
    input  wire [DATA_W-1:0]        wr_data,
    input  wire [DATA_W/8-1:0]      wr_strb,
    output logic                    wr_ack,
    output logic                    wr_rsp,
    output logic                    wr_err,

    // ---------------- AXI4 主口 ----------------
    output logic                m_arvalid,
    input  wire                 m_arready,
    output logic [ADDR_W-1:0]   m_araddr,
    output logic [7:0]          m_arlen,
    output logic [2:0]          m_arsize,
    output logic [1:0]          m_arburst,
    input  wire                 m_rvalid,
    output logic                m_rready,
    input  wire [DATA_W-1:0]    m_rdata,
    input  wire [1:0]           m_rresp,
    input  wire                 m_rlast,

    output logic                m_awvalid,
    input  wire                 m_awready,
    output logic [ADDR_W-1:0]   m_awaddr,
    output logic [7:0]          m_awlen,
    output logic [2:0]          m_awsize,
    output logic [1:0]          m_awburst,
    output logic                m_wvalid,
    input  wire                 m_wready,
    output logic [DATA_W-1:0]   m_wdata,
    output logic [DATA_W/8-1:0] m_wstrb,
    output logic                m_wlast,
    input  wire                 m_bvalid,
    output logic                m_bready,
    input  wire [1:0]           m_bresp
);

    typedef enum logic [2:0] {
        A_IDLE, A_RD_AR, A_RD_BEAT, A_RD_LAT, A_RD_RSP, A_WR_AW, A_WR_B
    } astate_t;

    astate_t state;
    logic [  ADDR_W-1:0] addr_q;
    logic                is_line_q;
    logic [DATA_W/8-1:0] strb_q;
    logic [  DATA_W-1:0] wdata_q;
    logic [4:0]        beat_q;        // 0..15 行内拍号(全局计数)
    logic              seg2_q;        // 1 = 正在取 64B 行的后 32B
    logic [ 3:0]       err_q;
    logic [31:0]       rbuf_q [0:LINE_WORDS-1];

    // handshake
    wire rd_hs = m_arvalid && m_arready;
    wire r_hs  = m_rvalid && m_rready;
    wire aw_hs = m_awvalid && m_awready;
    wire w_hs  = m_wvalid && m_wready;
    wire b_hs  = m_bvalid && m_bready;

    // 行的前 32B 起点 = {addr[31:6], 6'b0}, 第二段 +32B;字访问只发 1 拍
    logic [ADDR_W-1:0] seg_addr;
    assign seg_addr = is_line_q ? ({addr_q[ADDR_W-1:6], 6'b0} + (seg2_q ? ADDR_W'(32) : ADDR_W'(0)))
                                : addr_q;

    assign m_arvalid = (state == A_RD_AR);
    assign m_araddr  = seg_addr;
    assign m_arlen   = is_line_q ? 8'd7 : 8'd0;
    assign m_arsize  = 3'b010;          // 4 字节
    assign m_arburst = 2'b01;           // INCR
    assign m_rready  = (state == A_RD_BEAT);

    assign m_awvalid = (state == A_WR_AW);
    assign m_awaddr  = addr_q & ~ADDR_W'(3);   // 字对齐
    assign m_awlen   = 8'd0;
    assign m_awsize  = 3'b010;
    assign m_awburst = 2'b01;
    assign m_wvalid  = (state == A_WR_B);
    assign m_wdata   = wdata_q;
    assign m_wstrb   = strb_q;
    assign m_wlast   = 1'b1;
    assign m_bready  = (state == A_WR_B);

    // 读数据锁存: 行访问按拍号落到 rbuf
    always_ff @(posedge clk) begin
        if (rst) begin
            state      <= A_IDLE;
            addr_q     <= '0;
            is_line_q  <= 1'b0;
            beat_q     <= '0;
            seg2_q     <= 1'b0;
            err_q      <= '0;
            rd_ack     <= 1'b0;
            rd_rsp     <= 1'b0;
            rd_data    <= '0;
            rd_err     <= 1'b0;
            wr_ack     <= 1'b0;
            wr_rsp     <= 1'b0;
            wr_err     <= 1'b0;
            wdata_q    <= '0;
            strb_q     <= '0;
        end else begin
            rd_ack <= 1'b0;
            rd_rsp <= 1'b0;
            rd_err <= 1'b0;
            wr_ack <= 1'b0;
            wr_rsp <= 1'b0;
            wr_err <= 1'b0;

            case (state)
                A_IDLE: begin
                    beat_q <= '0;
                    seg2_q <= 1'b0;
                    err_q  <= '0;
                    if (rd_req) begin
                        addr_q    <= rd_addr;
                        is_line_q <= rd_line;
                        rd_ack    <= 1'b1;       // 先 ack, 再去总线取数
                        state     <= A_RD_AR;
                    end else if (wr_req) begin
                        addr_q  <= wr_addr;
                        wdata_q <= wr_data;
                        strb_q  <= wr_strb;
                        wr_ack  <= 1'b1;
                        state   <= A_WR_AW;
                    end
                end

                A_RD_AR: begin
                    if (rd_hs) state <= A_RD_BEAT;
                end

                A_RD_BEAT: begin
                    if (r_hs) begin
                        rbuf_q[beat_q[3:0]] <= m_rdata;
                        if (m_rresp != 2'b00) err_q <= 4'd1;
                        if (m_rlast) begin
                            if (is_line_q && !seg2_q) begin
                                // 第一段 8 拍结束, 接着取后 32B
                                beat_q <= 5'd8;
                                seg2_q <= 1'b1;
                                state  <= A_RD_AR;
                            end else begin
                                beat_q <= beat_q + 5'd1;
                                state  <= A_RD_LAT;
                            end
                        end else begin
                            beat_q <= beat_q + 5'd1;
                        end
                    end
                end

                A_RD_LAT: begin
                    for (int i = 0; i < LINE_WORDS; i = i + 1) rd_data[i*32 +: 32] <= rbuf_q[i];
                    state <= A_RD_RSP;
                end

                A_RD_RSP: begin
                    rd_err <= |err_q;
                    rd_rsp <= 1'b1;          // 数据已在上一拍锁进 rd_data
                    state  <= A_IDLE;
                end

                A_WR_AW: begin
                    if (aw_hs) state <= A_WR_B;
                end

                A_WR_B: begin
                    if (w_hs && b_hs) begin
                        wr_rsp <= 1'b1;
                        wr_err <= (m_bresp != 2'b00);
                        state  <= A_IDLE;
                    end else if (b_hs) begin
                        wr_rsp <= 1'b1;
                        wr_err <= (m_bresp != 2'b00);
                        state  <= A_IDLE;
                    end
                end

                default: state <= A_IDLE;
            endcase
        end
    end

endmodule
