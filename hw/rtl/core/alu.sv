`include "defines.vh"
import core_pkg::*;

module alu(
  input  alu_op_t     alu_op    ,
  input  logic [31:0] alu_src1  ,
  input  logic [31:0] alu_src2  ,
  output logic [31:0] alu_result

);

    logic use_sub;
    assign use_sub = alu_op inside {ALU_SUB, ALU_SLT, ALU_SLTU};

    logic [32:0] adder_sum;
    logic [31:0] adder_result;
    logic        adder_cout;
    assign adder_sum = 33'(alu_src1)
                     + 33'(use_sub ? ~alu_src2 : alu_src2)
                     + 33'(use_sub);
    assign {adder_cout, adder_result} = adder_sum;

    logic slt_bit, sltu_bit;
    assign slt_bit = (alu_src1[31] & ~alu_src2[31]) | ((alu_src1[31] ~^ alu_src2[31]) & adder_result[31]);
    assign sltu_bit = ~adder_cout;

    logic [31:0] and_result, or_result, xor_result, lui_result;
    assign and_result = alu_src1 & alu_src2;
    assign or_result  = alu_src1 | alu_src2;
    assign xor_result = alu_src1 ^ alu_src2;
    assign lui_result = alu_src2;

    logic [31:0] sll_result, srl_result, sra_result;
    assign sll_result = alu_src1 << alu_src2[4:0];
    assign srl_result = alu_src1 >> alu_src2[4:0];
    assign sra_result = $signed(alu_src1) >>> alu_src2[4:0];

    always_comb begin
        case (alu_op)
            ALU_ADD  : alu_result = adder_result;
            ALU_SUB  : alu_result = adder_result;
            ALU_SLT  : alu_result = 32'(slt_bit);
            ALU_SLTU : alu_result = 32'(sltu_bit);
            ALU_AND  : alu_result = and_result;
            ALU_OR   : alu_result = or_result;
            ALU_XOR  : alu_result = xor_result;
            ALU_SLL  : alu_result = sll_result;
            ALU_SRL  : alu_result = srl_result;
            ALU_SRA  : alu_result = sra_result;
            ALU_LUI  : alu_result = lui_result;
            default  : alu_result = '0;
        endcase
    end

endmodule
