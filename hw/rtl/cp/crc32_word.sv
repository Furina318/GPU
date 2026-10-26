`include "defines.vh"

module crc32_word (
    input  wire        clk   ,
    input  wire        rst   ,
    input  wire        init  ,          // 起新序列(crc ← 全 1)
    input  wire        en    ,          // 本拍喂一个字
    input  wire [31:0] data  ,
    output logic [31:0] crc
);
    // 一个字节的迭代:8 次 crc = (crc>>1) ^ (poly & -(crc^b)&1)
    function automatic logic [31:0] byte_step(input logic [31:0] c, input logic [7:0] b);
        logic [31:0] v;
        v = c ^ {24'h0, b};
        for (int i = 0; i < 8; i++)
            v = v[0] ? ((v >> 1) ^ 32'hEDB8_8320) : (v >> 1);
        return v;
    endfunction

    logic [31:0] c;
    wire [31:0] c1 = byte_step(c, data[7:0]);        // 低字节先(小端)
    wire [31:0] c2 = byte_step(c1, data[15:8]);
    wire [31:0] c3 = byte_step(c2, data[23:16]);
    wire [31:0] c4 = byte_step(c3, data[31:24]);

    always_ff @(posedge clk) begin
        if (rst)          c <= 32'hFFFF_FFFF;
        else if (init)    c <= 32'hFFFF_FFFF;
        else if (en)      c <= c4;
    end

    assign crc = ~c;                                  // 最终取反
endmodule
