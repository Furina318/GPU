`include "defines.vh"

// 把 host 的 AXI4-Lite 总线协议(5 个通道)转换成 CP 内部寄存器文件(cp_regs)需要的单周期脉冲式读写接口
module axil_slave (
    input  wire         clk        ,
    input  wire         rst        ,

    input  wire         s_awvalid  ,
    output logic        s_awready  ,
    input  wire  [31:0] s_awaddr   ,
    input  wire         s_wvalid   ,
    output logic        s_wready   ,
    input  wire  [31:0] s_wdata    ,
    input  wire  [ 3:0] s_wstrb    ,
    output logic        s_bvalid   ,
    input  wire         s_bready   ,
    output logic [ 1:0] s_bresp    ,
    input  wire         s_arvalid  ,
    output logic        s_arready  ,
    input  wire  [31:0] s_araddr   ,
    output logic        s_rvalid   ,
    input  wire         s_rready   ,
    output logic [31:0] s_rdata    ,
    output logic [ 1:0] s_rresp    ,

    // to reg
    output logic        reg_wr    ,
    output logic [11:0] reg_addr  ,
    output logic [31:0] reg_wdata ,
    output logic        reg_rd    ,
    input  wire  [31:0] reg_rdata
);
    localparam logic [1:0] RESP_OKAY = 2'b00;

    reg        aw_got, w_got, b_pending;
    reg [11:0] aw_addr;
    reg [31:0] w_data;

    reg        ar_got, r_pending;
    reg [11:0] ar_addr;
    reg [31:0] r_data;

    // 写到别的命名空间中
    wire aw_bad = |s_awaddr[31:12];
    wire ar_bad = |s_araddr[31:12];

    assign reg_wr    = aw_got & w_got & ~b_pending & ~aw_bad & (|s_wstrb);
    assign reg_addr  = reg_wr ? aw_addr : ar_addr;
    assign reg_wdata = w_data;
    // 写优先
    assign reg_rd    = ar_got & ~r_pending & ~reg_wr & ~ar_bad;

    assign s_awready = ~aw_got & ~b_pending;
    assign s_wready  = ~w_got  & ~b_pending;
    assign s_bvalid  = b_pending;
    assign s_bresp   = RESP_OKAY;

    assign s_arready = ~ar_got & ~r_pending;
    assign s_rvalid  = r_pending;
    assign s_rdata   = r_data;
    assign s_rresp   = RESP_OKAY;

    // 每通道单事务在途
    always_ff @(posedge clk) begin
        if (rst) begin
            aw_got  <= 1'b0;  w_got     <= 1'b0; b_pending <= 1'b0;
            aw_addr <= 12'h0; w_data    <= 32'h0;
            ar_got  <= 1'b0;  r_pending <= 1'b0;
            ar_addr <= 12'h0; r_data    <= 32'h0;
        end
        else begin
            if (s_awvalid && s_awready) begin aw_got <= 1'b1; aw_addr <= s_awaddr[11:0]; end
            if (s_wvalid  && s_wready ) begin w_got  <= 1'b1; w_data  <= s_wdata;        end

            if (reg_wr) begin
                aw_got    <= 1'b0;
                w_got     <= 1'b0;
                b_pending <= 1'b1;
            end
            else if (s_bvalid && s_bready) begin
                b_pending <= 1'b0;
            end

            if (s_arvalid && s_arready) begin ar_got <= 1'b1; ar_addr <= s_araddr[11:0]; end

            if (reg_rd) begin
                ar_got    <= 1'b0;
                r_pending <= 1'b1;
                r_data    <= reg_rdata;
            end
            else if (s_rvalid && s_rready) begin
                r_pending <= 1'b0;
            end
        end
    end
endmodule
