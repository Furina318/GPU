`include "defines.vh"

module wbu (
    input  wire                     valid          ,

    input  wire [`WARP_SIZE-1:0]    effective_mask ,

    input  wire                     gpr_wr_en_in   ,
    input  wire [`CLOG_GPR-1:0]     gpr_wr_addr_in ,
    input  wire [ `NUM_GPR-1:0]     gpr_wr_data_in [0:`WARP_SIZE-1],

    input  wire                     pred_wr_en_in  ,
    input  wire [`CLOG_PRED-1:0]    pred_wr_addr_in,
    input  wire [`WARP_SIZE-1:0]    pred_wr_data_in, 

    output wire [`WARP_SIZE-1:0]                 gpr_wr_en  ,
    output wire [`WARP_SIZE-1:0][`CLOG_GPR-1:0]  gpr_wr_addr,
    output wire [`WARP_SIZE-1:0][`NUM_GPR-1:0]   gpr_wr_data,

    output wire [`WARP_SIZE-1:0]                 pred_wr_en  ,
    output wire [`WARP_SIZE-1:0][`CLOG_PRED-1:0] pred_wr_addr,
    output wire [`WARP_SIZE-1:0]                 pred_wr_data
);

    genvar i;
    generate
        for (i = 0; i < `WARP_SIZE; i = i + 1) begin : gen_lane_wr
            assign gpr_wr_en[i]   = valid
                                  & gpr_wr_en_in
                                  & effective_mask[i];
            assign gpr_wr_addr[i] = gpr_wr_addr_in;
            assign gpr_wr_data[i] = gpr_wr_data_in[i];

            assign pred_wr_en[i]   = valid
                                   & pred_wr_en_in
                                   & effective_mask[i];
            assign pred_wr_addr[i] = pred_wr_addr_in;
            assign pred_wr_data[i] = pred_wr_data_in[i]; 

        end
    endgenerate

endmodule
