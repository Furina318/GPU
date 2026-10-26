`include "defines.vh"

// ============================================================================
//  mem_perf —— 内存子系统计数器(SPEC §3.4、ISA §8.1)
//
//  计数器与参考模型(sim/simulator 的 RunStats)同名同义, 只做"数事件",
//  不参与控制:
//    gmem_lines    一次 32B 行请求(读/写各算一次)
//    shared_extra  共享内存冲突额外拍(`shared_mem.conflict_extra` 累加)
//    mmio_access   内核访问 MMIO 窗口被拒的次数(ISA §2.6 MMIO_ACCESS)
//    unmapped      内核访问未映射地址被拒的次数(NO_MEM_REGION)
//
//  计数器在 `clear` 有效时清零(与 CP 的"写任意值清零"风格一致), 便于主机
//  按区间算增量: 读一次、清一次, 不必等到 32 bit 回绕。
// ============================================================================

module mem_perf (
    input  wire        clk,
    input  wire        rst,
    input  wire        clear,             // 单拍脉冲: 全部计数器清零

    // 事件输入
    input  wire        ev_line,           // 一次 32B 行请求(读或写)
    input  wire [3:0]  ev_shared_extra,   // 共享内存冲突额外拍
    input  wire        ev_mmio,           // MMIO_ACCESS
    input  wire        ev_unmapped,       // NO_MEM_REGION

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
