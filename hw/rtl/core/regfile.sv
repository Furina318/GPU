`include "defines.vh"

module regfile (
    input  wire                 clk          ,
    input  wire                 rst          ,
    input  wire                 valid        ,

    input  wire [`CLOG_GPR-1:0] gpr_wr_addr  ,
    input  wire                 gpr_wr_en    ,
    input  wire [ `NUM_GPR-1:0] gpr_wr_data  ,
    output wire [`CLOG_GPR-1:0] gpr_rs1_addr ,
    output wire [`CLOG_GPR-1:0] gpr_rs2_addr ,
    output wire [ `NUM_GPR-1:0] gpr_rs1_data ,
    output wire [ `NUM_GPR-1:0] gpr_rs2_data ,

    input  wire [`CLOG_PRED-1:0] pred_wr_addr,
    input  wire                  pred_wr_en  ,
    input  wire [ `NUM_PRED-1:0] pred_wr_data,
    input  wire [`CLOG_PRED-1:0] pred_rd_addr,
    output wire [ `NUM_PRED-1:0] pred_rd_data
);

    reg [31:0] gpr[`NUM_GPR-1:0];
    reg [ 3:0] pred[`NUM_PRED-1:0];

    always_ff @(posedge clk) begin
        // 使用之前一定会先写入
        if (valid) begin
            if (gpr_wr_en && |gpr_wr_addr) begin
                gpr[gpr_wr_addr] <= gpr_wr_data;
            end
            if (pred_wr_en && |pred_wr_addr) begin
                pred[pred_wr_addr] <= pred_wr_data;
            end
        end
    end

    assign gpr_rs1_data = (|gpr_rs1_addr) ? gpr[gpr_rs1_addr] : `NUM_GPR'(0);
    assign gpr_rs2_data = (|gpr_rs2_addr) ? gpr[gpr_rs2_addr] : `NUM_GPR'(0);
    assign pred_rd_data = (|pred_rd_addr) ? pred[pred_rd_addr] : `NUM_PRED'(0);

endmodule
