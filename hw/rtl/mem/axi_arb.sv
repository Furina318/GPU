`include "defines.vh"

// ============================================================================
//  axi_arb —— 多主 → 1 个 AXI4 主口的仲裁器(SPEC.md §3.4)
//
//  主:CP 的 DMA、2 个 SM 的访存、2D 引擎、矩阵引擎。
//  端口用 mem/axi4_bus.sv 的 `master` modport 数组 + 一个 `slave` modport,
//  读/写通道各自独立轮转(AR/R 与 AW/W/B 互不阻塞)。
//
//  ⚠ 本文件目前只是**占位**:真正的实现在 Phase E(mem/ 与 LSU 开发规划
//  docs/MEM_LSU.md)。Phase A 只把语法瑕疵(端口列表多余逗号)修掉, 不接线,
//  所以它不在 tb/harness/duts.py 里注册。
// ============================================================================

module axi_arb #(
    parameter int NUM_MASTERS = 5,          // CP / SM0 / SM1 / 2D / GEMM
    parameter int ADDR_W      = 32,
    parameter int DATA_W      = 32,
    parameter int ID_W        = 8
) (
    input  wire clk,
    input  wire rst
);

    // TODO(Phase E):
    //   · axi4_bus.master 数组(NUM_MASTERS)进、axi4_bus.slave 出;
    //   · AR/R 与 AW/W/B 各自 round-robin, CP 置最高优先级(命令级流量不能被
    //     SM 的流式读写饿死);
    //   · ID 复用与响应路由:每主的请求带主号, 响应按 ID 送回;
    //   · 未选择的主保持 ready=0(而不是丢弃请求) —— 仲裁只做"让谁走"。
    `UNUSED_VAR(clk)
    `UNUSED_VAR(rst)
    `UNUSED_VAR(NUM_MASTERS)
    `UNUSED_VAR(ADDR_W)
    `UNUSED_VAR(DATA_W)
    `UNUSED_VAR(ID_W)

endmodule
