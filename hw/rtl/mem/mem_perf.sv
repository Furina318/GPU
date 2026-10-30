`include "defines.vh"

//  mem_perf —— 内存子系统计数器

module mem_perf (
    input  wire         clk,
    input  wire         rst,
    input  wire         clear,             // 单拍脉冲: 全部计数器清零

    // 事件输入
    input  wire         ev_line,           // 一次 32B 行请求(读或写)
    input  wire  [ 3:0] ev_shared_extra,   // 共享内存冲突额外拍
    input  wire         ev_mmio,           // MMIO_ACCESS
    input  wire         ev_unmapped,       // NO_MEM_REGION

    // 计数器
    output logic [31:0] ctr_lines,
    output logic [31:0] ctr_shared_extra,
    output logic [31:0] ctr_mmio,
    output logic [31:0] ctr_unmapped
);

    logic [31:0] lines_q, sh_extra_q, mmio_q, unmapped_q;

    always_ff @(posedge clk) begin
        if (rst || clear) begin
            lines_q    <= '0;
            sh_extra_q <= '0;
            mmio_q     <= '0;
            unmapped_q <= '0;
        end else begin
            lines_q    <= lines_q    + 32'(ev_line);
            sh_extra_q <= sh_extra_q + 32'(ev_shared_extra);
            mmio_q     <= mmio_q     + 32'(ev_mmio);
            unmapped_q <= unmapped_q + 32'(ev_unmapped);
        end
    end

    assign ctr_lines        = lines_q;
    assign ctr_shared_extra = sh_extra_q;
    assign ctr_mmio         = mmio_q;
    assign ctr_unmapped     = unmapped_q;

endmodule
