`include "defines.vh"

//  cp_mem_adapter —— CP 的行/字访存协议 → AXI4 主口
//  CP 一次只有一个内存操作在飞, 所以这里不做 pipelining

module cp_mem_adapter #(
    parameter int ADDR_W = 32,
    parameter int DATA_W = 32
) (
    input  wire clk,
    input  wire rst,

    // ---------------- CP 侧 ----------------
    input  wire                     rd_req,
    input  wire [ADDR_W-1:0]        rd_addr,
    input  wire                     rd_line,
    output logic                    rd_ack,      // 请求收下了(地址已采样, 还没有数据)
    // 读回来的数据按 AXI R 的节奏一拍一个字吐出(与 m_rvalid 同拍), 不再攒整行:
    // 512 位的行缓冲和"攒满再回"的两个状态都不需要了
    output logic                    rd_vld,      // 本拍有一个字
    output logic [DATA_W-1:0]       rd_word,
    output logic                    rd_last,     // 本次读的最后一个字
    output logic                    rd_err,      // 与 rd_word 同拍的响应错误

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

    // 读: A_RD_AR 发 AR, A_RD_BEAT 逐拍把 R 通道的数据转给上层。
    // 行读 = 两段 8 拍 burst, 中间回 A_RD_AR 重发一次;字读 = 1 拍。
    typedef enum logic [2:0] {
        A_IDLE, A_RD_AR, A_RD_BEAT, A_WR_AW, A_WR_B
    } astate_t;

    astate_t state;
    logic [  ADDR_W-1:0] addr_q;
    logic                is_line_q;
    logic [DATA_W/8-1:0] strb_q;
    logic [  DATA_W-1:0] wdata_q;
    logic              seg2_q;        // 1 = 正在取 64B 行的后 32B

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

    // 数据直通: 不再攒整行, 每拍一个 32 位字直接交给上层
    assign rd_vld  = (state == A_RD_BEAT) && m_rvalid;
    assign rd_word = m_rdata;
    assign rd_last = rd_vld && m_rlast && (!is_line_q || seg2_q);
    assign rd_err  = rd_vld && (m_rresp != 2'b00);

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

    always_ff @(posedge clk) begin
        if (rst) begin
            state      <= A_IDLE;
            addr_q     <= '0;
            is_line_q  <= 1'b0;
            seg2_q     <= 1'b0;
            rd_ack     <= 1'b0;
            wr_ack     <= 1'b0;
            wr_rsp     <= 1'b0;
            wr_err     <= 1'b0;
            wdata_q    <= '0;
            strb_q     <= '0;
        end else begin
            rd_ack <= 1'b0;
            wr_ack <= 1'b0;
            wr_rsp <= 1'b0;
            wr_err <= 1'b0;

            case (state)
                A_IDLE: begin
                    seg2_q <= 1'b0;
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
                    if (r_hs && m_rlast) begin
                        if (is_line_q && !seg2_q) begin
                            // 第一段 8 拍结束, 接着取后 32B
                            seg2_q <= 1'b1;
                            state  <= A_RD_AR;
                        end else begin
                            state <= A_IDLE;      // 最后一拍已经直接交给上层了
                        end
                    end
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
