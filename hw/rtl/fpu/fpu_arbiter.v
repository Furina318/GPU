`include "fpu_config.vh"

module fpu_arbiter #(
    parameter NUM_PORTS  = 4,
    parameter ID_WIDTH   = 5,
    parameter DATA_WIDTH = 32
)(
    input  wire [ NUM_PORTS-1:0] req_valid_i,
    input  wire [NUM_PORTS*ID_WIDTH-1:0]   req_id_i,
    input  wire [NUM_PORTS*DATA_WIDTH-1:0] req_data_i,
    input  wire [NUM_PORTS*5-1:0]           req_fflags_i,

    output reg  [ NUM_PORTS-1:0] grant_o    , // 1-hot独热码
    output reg                   wb_valid_o ,
    output reg  [  ID_WIDTH-1:0] wb_id_o    ,
    output reg  [DATA_WIDTH-1:0] wb_data_o  ,
    output reg  [           4:0] wb_fflags_o,

    input  wire                  flush      ,
    input  wire [  ID_WIDTH-1:0] flush_id
);

    wire [NUM_PORTS-1:0] flush_match;
    wire [NUM_PORTS-1:0] req;

    genvar i;
    generate
        for (i = 0; i < NUM_PORTS; i = i + 1) begin : gen_flush
            assign flush_match[i] = flush & (req_id_i[ID_WIDTH*i +: ID_WIDTH] == flush_id);
        end
    endgenerate

    assign req = req_valid_i & ~flush_match;

    // 循环年龄比较：a 是否比 b 老
    function automatic is_older;
        input [ID_WIDTH-1:0] a;
        input [ID_WIDTH-1:0] b;
        reg [ID_WIDTH-1:0] diff;
        begin
            diff = b - a;
            is_older = diff[ID_WIDTH-1] == 1'b0 && (diff != {ID_WIDTH{1'b0}});
        end
    endfunction

    integer j;
    reg         best_valid;
    reg [ID_WIDTH-1:0] best_id;
    reg [2:0]   best_idx;

    always @(*) begin
        best_valid = 1'b0;
        best_id    = {ID_WIDTH{1'b0}};
        best_idx   = 3'b0;

        for (j = 0; j < NUM_PORTS; j = j + 1) begin
            if (req[j]) begin
                if (!best_valid || is_older(req_id_i[ID_WIDTH*j +: ID_WIDTH], best_id)) begin
                    best_valid = 1'b1;
                    best_id    = req_id_i[ID_WIDTH*j +: ID_WIDTH];
                    best_idx   = j[2:0];
                end
            end
        end

        grant_o    = {NUM_PORTS{1'b0}};
        wb_valid_o = best_valid;
        wb_id_o    = best_id;
        wb_data_o  = {DATA_WIDTH{1'b0}};
        wb_fflags_o = 5'd0;

        if (best_valid) begin
            grant_o[best_idx] = 1'b1;
            wb_data_o  = req_data_i[DATA_WIDTH*best_idx +: DATA_WIDTH];
            wb_fflags_o = req_fflags_i[5*best_idx +: 5];
        end
    end

endmodule
