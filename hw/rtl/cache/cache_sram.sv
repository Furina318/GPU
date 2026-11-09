/* verilator lint_off EOFNEWLINE */
/* verilator lint_off DECLFILENAME */
/* verilator lint_off WIDTHEXPAND */
/* verilator lint_off WIDTHTRUNC */
/* verilator lint_off UNUSEDPARAM */
module data_bank_sram #(
    parameter WIDTH = 32,
    parameter DEPTH = 16
)(
    input  [$clog2(DEPTH)-1:0] addra   ,
    input                      clka    ,
    input  [WIDTH-1:0]         dina    ,
    output [WIDTH-1:0]         douta   ,
    input                      ena     ,
    input  [ 3:0]              wea      
);
    localparam V_STYLE = "block";
    localparam P_STYLE =    (V_STYLE == "ultra")        ? "uram" :
                            (V_STYLE == "distributed")  ? "select_ram" :
                            "block_ram";

    (*ram_style = V_STYLE*) reg [31:0] mem_reg [DEPTH-1:0]/*synthesis syn_ramstyle=P_STYLE*/;
    reg [31:0] output_buffer;

    always @(posedge clka) begin
        if (ena) begin
            if (wea) begin
                if (wea[0]) begin
                    mem_reg[addra][ 7: 0] <= dina[ 7: 0]; 
                end 

                if (wea[1]) begin
                    mem_reg[addra][15: 8] <= dina[15: 8];
                end

                if (wea[2]) begin
                    mem_reg[addra][23:16] <= dina[23:16];
                end

                if (wea[3]) begin
                    mem_reg[addra][31:24] <= dina[31:24];
                end
            end
            else begin
                output_buffer <= mem_reg[addra];
            end
        end
    end

    assign douta = output_buffer;

endmodule 

module tagv_sram #(
    parameter WIDTH = 21,
    parameter DEPTH = 16
)( 
    input  [$clog2(DEPTH)-1:0] addra   ,
    input                      clka    ,
    input  [WIDTH-1:0]         dina    ,
    output [WIDTH-1:0]         douta   ,
    input                      ena     ,
    input                      wea 
);

    localparam V_STYLE = "block";
    localparam P_STYLE =    (V_STYLE == "ultra")        ? "uram" :
                            (V_STYLE == "distributed")  ? "select_ram" :
                            "block_ram";

    (*ram_style = V_STYLE*) reg [WIDTH-1:0] mem_reg [DEPTH-1:0]/*synthesis syn_ramstyle=P_STYLE*/;
    reg [WIDTH-1:0] output_buffer;

    always @(posedge clka) begin
        if (ena) begin
            if (wea) begin
                mem_reg[addra] <= dina;
            end
            else begin
                output_buffer <= mem_reg[addra];
            end
        end
    end

    assign douta = output_buffer;

endmodule
/* verilator lint_on UNUSEDPARAM */
/* verilator lint_on WIDTHTRUNC */
/* verilator lint_on WIDTHEXPAND */
/* verilator lint_on DECLFILENAME */
/* verilator lint_on EOFNEWLINE */
